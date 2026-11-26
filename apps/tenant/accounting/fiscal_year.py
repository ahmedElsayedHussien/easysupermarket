"""
إقفال السنة المالية: قيد بتاريخ 31/12 يصفّر حسابات الإيرادات والمصروفات للسنة في
3200 أرباح محتجزة، ثم تُقفل الدفاتر حتى آخر السنة (SystemSetting.accounting_lock_date).
Re-opening is allowed for the latest closed year only (the closing entry is reversed on the
same date and the previous lock date is restored).
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, ExtractYear

from .models import Account, FiscalYearClose, JournalEntry, JournalItem

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=20, decimal_places=2)
RETAINED_EARNINGS = '3200'


def _year_items(year):
    return JournalItem.objects.filter(entry__status=JournalEntry.POSTED, entry__date__year=year,
                                      account__account_type__in=[Account.REVENUE, Account.EXPENSE])


def year_summary(year):
    """Revenue / expenses / net profit of the year (before its closing entry), one query."""
    closing_ids = list(FiscalYearClose.objects.filter(year=year).exclude(closing_entry=None)
                       .values_list('closing_entry_id', flat=True))
    qs = _year_items(year).exclude(entry_id__in=closing_ids).exclude(entry__reversal_of_id__in=closing_ids)
    agg = qs.aggregate(
        rev=Coalesce(Sum(F('credit') - F('debit'), filter=Q(account__account_type=Account.REVENUE)), Value(ZERO),
                     output_field=MONEY),
        exp=Coalesce(Sum(F('debit') - F('credit'), filter=Q(account__account_type=Account.EXPENSE)), Value(ZERO),
                     output_field=MONEY),
    )
    return {'year': year, 'revenue': agg['rev'], 'expenses': agg['exp'], 'net_profit': agg['rev'] - agg['exp']}


def years_overview():
    """Every year that has revenue / expense movements, newest first, with its close status."""
    years = sorted(set(JournalItem.objects.filter(entry__status=JournalEntry.POSTED)
                       .annotate(y=ExtractYear('entry__date')).values_list('y', flat=True)), reverse=True)
    closes = {c.year: c for c in FiscalYearClose.objects.select_related('closing_entry', 'closed_by')}
    latest_closed = max(closes) if closes else None
    today = datetime.date.today()
    rows = []
    for y in years:
        row = year_summary(y)
        earlier_open = [p for p in years if p < y and p not in closes]
        row.update(close=closes.get(y), can_reopen=y == latest_closed,
                   can_close=y not in closes and y < today.year and not earlier_open)
        rows.append(row)
    return rows


def close_year(year, user=None):
    from apps.tenant.core.models import SystemSetting
    from apps.tenant.services.journal_service import _get_system_account, unique_reference
    end = datetime.date(year, 12, 31)
    if end >= datetime.date.today():
        raise ValidationError('ينفع تقفل السنة بعد ما تخلص بس.')
    if FiscalYearClose.objects.filter(year=year).exists():
        raise ValidationError(f'سنة {year} مقفولة بالفعل.')
    if FiscalYearClose.objects.filter(year__gt=year).exists():
        raise ValidationError('فيه سنة بعدها مقفولة — مينفعش تقفل سنة قديمة بعدها.')
    closed = set(FiscalYearClose.objects.values_list('year', flat=True))
    earlier = set(JournalItem.objects.filter(entry__status=JournalEntry.POSTED, entry__date__year__lt=year)
                  .annotate(y=ExtractYear('entry__date')).values_list('y', flat=True).distinct())
    if earlier - closed:
        raise ValidationError(f'اقفل سنة {min(earlier - closed)} الأول — السنين بتتقفل بالترتيب.')
    settings = SystemSetting.get_settings()
    lock = settings.accounting_lock_date
    if lock and lock >= end:
        raise ValidationError(f'الدفاتر مقفولة لحد {lock} — افتح القفل الأول من الإعدادات أو اختار سنة بعده.')
    with transaction.atomic():
        rows = (_year_items(year).values('account_id').annotate(
            net=Coalesce(Sum(F('debit') - F('credit')), Value(ZERO), output_field=MONEY)).exclude(net=0))
        accounts = {a.id: a for a in Account.objects.filter(id__in=[r['account_id'] for r in rows])}
        summary = year_summary(year)
        entry = JournalEntry.objects.create(date=end, reference=unique_reference(f'FY-{year}-CLOSE'),
                                            description=f'قيد إقفال السنة المالية {year}', status=JournalEntry.DRAFT,
                                            created_by=user if getattr(user, 'pk', None) else None)
        for r in rows:
            net = r['net']   # debit balance (expense) is credited, credit balance (revenue) is debited
            JournalItem.objects.create(entry=entry, account=accounts[r['account_id']],
                                       debit=-net if net < 0 else ZERO, credit=net if net > 0 else ZERO,
                                       description=f'إقفال {year}')
        profit = summary['net_profit']
        if profit:
            JournalItem.objects.create(entry=entry, account=_get_system_account(RETAINED_EARNINGS),
                                       debit=-profit if profit < 0 else ZERO, credit=profit if profit > 0 else ZERO,
                                       description=f'صافي {"ربح" if profit > 0 else "خسارة"} {year}')
        if not entry.items.exists():
            raise ValidationError(f'مفيش إيرادات أو مصروفات في سنة {year}.')
        entry.post()
        close = FiscalYearClose.objects.create(year=year, revenue=summary['revenue'], expenses=summary['expenses'],
                                               net_profit=profit, closing_entry=entry, previous_lock_date=lock,
                                               closed_by=user)
        settings.accounting_lock_date = end
        settings.save(update_fields=['accounting_lock_date'])
    return close


def reopen_year(close, user=None):
    from apps.tenant.core.models import SystemSetting
    from apps.tenant.services.journal_service import reverse_journal_entry
    latest = FiscalYearClose.objects.order_by('-year').first()
    if latest is None or latest.pk != close.pk:
        raise ValidationError('ينفع تفتح آخر سنة مقفولة بس.')
    with transaction.atomic():
        settings = SystemSetting.get_settings()
        settings.accounting_lock_date = close.previous_lock_date
        settings.save(update_fields=['accounting_lock_date'])
        if close.closing_entry_id and not close.closing_entry.is_reversed:
            reverse_journal_entry(close.closing_entry, reason=f'إعادة فتح سنة {close.year}', user=user,
                                  date=datetime.date(close.year, 12, 31))
        close.delete()
