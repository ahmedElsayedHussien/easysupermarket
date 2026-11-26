"""
إقفال الفترات (الشهور): the books are closed up to a date (SystemSetting.accounting_lock_date) — every
posting dated on/before it is refused by JournalEntry.post(). Closing / re-opening goes through here
only (not through the settings form), with checks and a history (PeriodClose):

  close_until(date)  refused if a DRAFT entry is still in the period, if the period's posted entries
                     don't balance, or if the date isn't after the current lock date.
  reopen_to(date)    a reason is required, and never inside a closed fiscal year (re-open the year from
                     «إقفال السنة»). Needs the reopen permission (admins).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

ZERO = Decimal('0')


def current_lock():
    from apps.tenant.core.settings_helpers import accounting_lock_date
    return accounting_lock_date()


def _set_lock(date):
    from apps.tenant.core.models import SystemSetting
    s = SystemSetting.get_settings()
    s.accounting_lock_date = date
    s.save(update_fields=['accounting_lock_date'])


def period_check(start_after, until):
    """(drafts in the period, posted debit, posted credit) for entries dated in (start_after, until]."""
    from .models import JournalEntry, JournalItem
    rng = {'date__lte': until}
    if start_after:
        rng['date__gt'] = start_after
    drafts = JournalEntry.objects.filter(status=JournalEntry.DRAFT, **rng).count()
    sums = JournalItem.objects.filter(entry__status=JournalEntry.POSTED,
                                      **{f'entry__{k}': v for k, v in rng.items()}).aggregate(
        d=Sum('debit'), c=Sum('credit'))
    return drafts, sums['d'] or ZERO, sums['c'] or ZERO


@transaction.atomic
def close_until(until, user=None, note=''):
    from apps.tenant.core.models import SystemSetting
    from .models import PeriodClose
    SystemSetting.objects.select_for_update().filter(pk=1).first()          # one close at a time
    lock = current_lock()
    if lock and until <= lock:
        raise ValidationError(f'الدفاتر مقفولة أصلاً لحد {lock:%d/%m/%Y} — اختار تاريخ بعده.')
    drafts, debit, credit = period_check(lock, until)
    if drafts:
        raise ValidationError(f'فيه {drafts} قيد مسودة في الفترة — رحّلها أو امسحها قبل الإقفال.')
    if abs(debit - credit) >= Decimal('0.0001'):
        raise ValidationError(f'قيود الفترة مش متوازنة (مدين {debit:.2f} / دائن {credit:.2f}) — راجعها قبل الإقفال.')
    _set_lock(until)
    return PeriodClose.objects.create(action=PeriodClose.CLOSE, lock_date=until, previous_lock_date=lock,
                                      user=user, note=(note or '').strip()[:255])


@transaction.atomic
def reopen_to(new_lock, user=None, reason=''):
    """Move the lock date back (None = no lock at all)."""
    from apps.tenant.core.models import SystemSetting
    from .models import FiscalYearClose, PeriodClose
    reason = (reason or '').strip()
    if len(reason) < 3:
        raise ValidationError('اكتب سبب إعادة فتح الفترة.')
    SystemSetting.objects.select_for_update().filter(pk=1).first()
    lock = current_lock()
    if not lock:
        raise ValidationError('مفيش فترة مقفولة.')
    if new_lock and new_lock >= lock:
        raise ValidationError('تاريخ إعادة الفتح لازم يكون قبل تاريخ القفل الحالي.')
    # re-opening must not unlock any day of a closed fiscal year
    import datetime
    blocking = sorted(c.year for c in FiscalYearClose.objects.only('year')
                      if new_lock is None or new_lock < datetime.date(c.year, 12, 31))
    if blocking:
        raise ValidationError(f'سنة {blocking[-1]} مقفولة — افتحها الأول من «إقفال السنة المالية».')
    _set_lock(new_lock)
    return PeriodClose.objects.create(action=PeriodClose.REOPEN, lock_date=new_lock, previous_lock_date=lock,
                                      user=user, note=reason[:255])
