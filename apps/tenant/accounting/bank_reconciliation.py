"""
التسوية البنكية: مطابقة كشف البنك مع حركات حساب البنك في الدفاتر.

  book balance      all posted movements of the bank's ledger account up to the statement date
  cleared balance   movements ticked in earlier FINISHED reconciliations + the ones ticked now
  difference        statement balance - cleared balance      (must be 0 to finish)
Movements not ticked (a cheque not cashed yet, a deposit in transit...) stay open for the
next reconciliation. Amounts are signed as the bank sees them: deposit + / withdrawal -.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import BankReconciliation, BankReconciliationItem, JournalEntry, JournalItem

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=18, decimal_places=2)


def _net(qs):
    return qs.aggregate(s=Coalesce(Sum(F('debit') - F('credit')), Value(ZERO), output_field=MONEY))['s']


def _items(bank, date):
    if not bank.account_id:
        return JournalItem.objects.none()
    return JournalItem.objects.filter(account_id=bank.account_id, entry__status=JournalEntry.POSTED,
                                      entry__date__lte=date)


def _cleared_before(bank, exclude_rec_id=None):
    qs = BankReconciliationItem.objects.filter(reconciliation__bank_account=bank,
                                               reconciliation__status=BankReconciliation.DONE)
    if exclude_rec_id:
        qs = qs.exclude(reconciliation_id=exclude_rec_id)
    return qs


def snapshot(rec):
    """Figures and the open movements for a reconciliation (a few queries)."""
    bank, date = rec.bank_account, rec.statement_date
    done = _cleared_before(bank, exclude_rec_id=rec.pk)
    done_ids = done.values('journal_item_id')
    previous_cleared = _net(JournalItem.objects.filter(id__in=done_ids))
    candidates = list(_items(bank, date).exclude(id__in=done_ids).select_related('entry')
                      .order_by('entry__date', 'id'))
    ticked = set(rec.items.values_list('journal_item_id', flat=True)) if rec.pk else set()
    ticked_net = sum(((i.debit - i.credit) for i in candidates if i.id in ticked), ZERO)
    book = _net(_items(bank, date))
    cleared = previous_cleared + ticked_net
    return {
        'book': book, 'previous_cleared': previous_cleared, 'cleared': cleared,
        'difference': rec.statement_balance - cleared,
        'rows': [{'item': i, 'amount': i.debit - i.credit, 'ticked': i.id in ticked} for i in candidates],
    }


def start(bank, statement_date, statement_balance, user=None, notes=''):
    if not bank.account_id:
        raise ValidationError('الحساب البنكي مش مربوط بحساب في الشجرة.')
    if BankReconciliation.objects.filter(bank_account=bank, status=BankReconciliation.DRAFT).exists():
        raise ValidationError('فيه تسوية مسودة مفتوحة للحساب ده — كمّلها أو احذفها الأول.')
    last = BankReconciliation.objects.filter(bank_account=bank, status=BankReconciliation.DONE).order_by(
        '-statement_date').first()
    if last and statement_date < last.statement_date:
        raise ValidationError(f'آخر تسوية منتهية كانت بتاريخ {last.statement_date} — التاريخ لازم يكون بعده.')
    return BankReconciliation.objects.create(bank_account=bank, statement_date=statement_date,
                                             statement_balance=Decimal(statement_balance), notes=(notes or '')[:255],
                                             created_by=user)


def save_ticks(rec, item_ids, finish=False, user=None):
    """Store the ticked movements; ``finish`` closes it (only with a zero difference)."""
    with transaction.atomic():
        rec = BankReconciliation.objects.select_for_update(of=('self',)).select_related('bank_account').get(pk=rec.pk)
        if rec.status != BankReconciliation.DRAFT:
            raise ValidationError('التسوية منتهية.')
        allowed = set(_items(rec.bank_account, rec.statement_date)
                      .exclude(id__in=_cleared_before(rec.bank_account).values('journal_item_id'))
                      .values_list('id', flat=True))
        ids = {int(i) for i in item_ids if str(i).isdigit() and int(i) in allowed}
        rec.items.exclude(journal_item_id__in=ids).delete()
        have = set(rec.items.values_list('journal_item_id', flat=True))
        BankReconciliationItem.objects.bulk_create([BankReconciliationItem(reconciliation=rec, journal_item_id=i)
                                                    for i in ids - have])
        snap = snapshot(rec)
        rec.book_balance, rec.cleared_balance, rec.difference = snap['book'], snap['cleared'], snap['difference']
        if finish and snap['difference'] == 0:
            rec.status, rec.done_at = BankReconciliation.DONE, timezone.now()
        rec.save()
    if finish and rec.status != BankReconciliation.DONE:
        # the ticks are kept as a draft; only finishing is refused
        raise ValidationError(f'اتحفظت كمسودة — الفرق لسه {rec.difference} ولازم يبقى صفر عشان تنهي التسوية.')
    return rec


def reopen(rec):
    """Back to draft — only the latest finished reconciliation of the account."""
    latest = BankReconciliation.objects.filter(bank_account=rec.bank_account, status=BankReconciliation.DONE).order_by(
        '-statement_date', '-id').first()
    if rec.status != BankReconciliation.DONE or latest is None or latest.pk != rec.pk:
        raise ValidationError('ينفع تفتح آخر تسوية منتهية بس.')
    if BankReconciliation.objects.filter(bank_account=rec.bank_account, status=BankReconciliation.DRAFT).exists():
        raise ValidationError('فيه تسوية مسودة مفتوحة للحساب ده.')
    rec.status, rec.done_at = BankReconciliation.DRAFT, None
    rec.save(update_fields=['status', 'done_at'])
    return rec
