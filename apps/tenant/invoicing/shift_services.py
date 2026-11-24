"""
Cash-drawer accounting for POS shifts.

The drawer of a shift IS its treasury: every cash movement of the shift is posted on
the treasury's ledger account when it happens, so the treasury's book balance is what
the drawer should contain. This module keeps the book and the physical cash equal:

* opening  -> the shift's opening balance is the treasury's book balance (no typing);
* closing  -> the counted-vs-expected difference is booked (no silent gap):
                shortage: Dr 5400 (expense) or Dr 1135 (charged to the cashier) / Cr treasury
                overage : Dr treasury / Cr 5400
* deposit  -> optional cash drop at close: an InternalTransfer from the drawer to another
              treasury / bank / e-wallet (two-step when it goes to another branch).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

ZERO = Decimal('0')
CENT = Decimal('0.01')
VARIANCE_ACCOUNT_CODE = '5400'   # عجز وزيادة الخزينة (الورديات)
CASHIER_ACCOUNT_CODE = '1135'    # ذمم الموظفين والعهد

CHARGE_EXPENSE = 'EXPENSE'
CHARGE_CASHIER = 'CASHIER'


def treasury_book_balance(treasury):
    """Ledger balance of the treasury (what its drawer should hold)."""
    if treasury is None:
        return ZERO
    acc = treasury.ensure_account()
    if acc is None:
        return ZERO
    from apps.tenant.accounting.models import Account
    return Account.objects.get(pk=acc.pk).current_balance  # fresh row: no cached balance


def open_session_on_treasury(treasury, exclude_user=None):
    """The OPEN shift already using this drawer (one drawer = one cashier), or None."""
    from apps.tenant.invoicing.models import POSSession
    qs = POSSession.objects.filter(treasury=treasury, status=POSSession.OPEN)
    if exclude_user is not None:
        qs = qs.exclude(user=exclude_user)
    return qs.select_related('user').first()


def _post(reference, description, lines, user, session):
    from django.contrib.contenttypes.models import ContentType
    from apps.tenant.accounting.models import JournalEntry, JournalItem
    from apps.tenant.services.journal_service import unique_reference
    entry = JournalEntry.objects.create(
        date=timezone.localdate(), reference=unique_reference(reference), description=description,
        status=JournalEntry.DRAFT, created_by=user if getattr(user, 'pk', None) else None,
        content_type=ContentType.objects.get_for_model(session), object_id=session.pk,
    )
    for account, debit, credit, desc in lines:
        JournalItem.objects.create(entry=entry, account=account, debit=debit, credit=credit,
                                   description=desc[:500])
    entry.post()
    return entry


def _cashier_name(session):
    from apps.tenant.core.templatetags.user_tags import display_name
    try:
        return display_name(session.user)
    except Exception:  # noqa: BLE001 - label only
        return session.user.get_username()


def post_shift_variance(session, charge_to=CHARGE_EXPENSE, user=None):
    """
    Books session.difference (actual - expected) against the drawer's treasury.
    Overage always goes to 5400 (income side); a shortage goes to 5400 or, with
    charge_to=CASHIER, to the employees' receivable 1135. Returns the entry or None.
    """
    from apps.tenant.services.journal_service import _get_system_account
    diff = (session.difference or ZERO).quantize(CENT)
    if abs(diff) < CENT:
        return None
    treasury_acc = session.treasury.ensure_account()
    if treasury_acc is None:
        raise ValidationError('خزينة الوردية غير مرتبطة بحساب في شجرة الحسابات.')
    variance_acc = _get_system_account(VARIANCE_ACCOUNT_CODE)
    cashier = _cashier_name(session)
    amount = abs(diff)
    if diff < 0:
        to_cashier = charge_to == CHARGE_CASHIER
        debit_acc = _get_system_account(CASHIER_ACCOUNT_CODE) if to_cashier else variance_acc
        label = f'عجز درج الوردية #{session.pk} ({session.treasury}) - الكاشير {cashier}'
        label += ' - محمّل على الكاشير' if to_cashier else ' - مصروف على المحل'
        lines = [(debit_acc, amount, ZERO, label), (treasury_acc, ZERO, amount, label)]
        session.variance_charged_to = CHARGE_CASHIER if to_cashier else CHARGE_EXPENSE
    else:
        label = f'زيادة درج الوردية #{session.pk} ({session.treasury}) - الكاشير {cashier}'
        lines = [(treasury_acc, amount, ZERO, label), (variance_acc, ZERO, amount, label)]
        session.variance_charged_to = CHARGE_EXPENSE
    entry = _post(f'SHIFT-{session.pk}-VAR', label, lines, user, session)
    session.variance_entry = entry
    session.save(update_fields=['variance_entry', 'variance_charged_to'])
    return entry


def charge_shortage_to_cashier(session, user=None):
    """Re-books a closed shift's shortage from the expense account onto the cashier (1135)."""
    from apps.tenant.invoicing.models import POSSession
    from apps.tenant.services.journal_service import reverse_entry_if_active
    with transaction.atomic():
        s = POSSession.objects.select_for_update().select_related('treasury', 'user', 'variance_entry').get(pk=session.pk)
        if s.status != POSSession.CLOSED:
            raise ValidationError('الوردية لم تُغلق بعد.')
        if (s.difference or ZERO) >= 0 or not s.variance_entry_id:
            raise ValidationError('لا يوجد عجز مسجل على هذه الوردية.')
        if s.variance_charged_to == CHARGE_CASHIER:
            raise ValidationError('العجز محمّل على الكاشير بالفعل.')
        reverse_entry_if_active(s.variance_entry, reason=f'تحميل عجز الوردية #{s.pk} على الكاشير', user=user)
        return post_shift_variance(s, CHARGE_CASHIER, user)


def deposit_drawer_cash(session, dest_type, dest_id, amount, user=None):
    """Cash drop at close: InternalTransfer drawer -> treasury/bank/e-wallet, confirmed now."""
    from apps.tenant.accounting.models import BankAccount, EWallet, InternalTransfer, Treasury
    amount = Decimal(str(amount)).quantize(CENT)
    if amount <= 0:
        return None
    model = {InternalTransfer.TREASURY: Treasury, InternalTransfer.BANK: BankAccount,
             InternalTransfer.EWALLET: EWallet}.get(dest_type)
    if model is None:
        raise ValidationError('نوع جهة التوريد غير صالح.')
    dest = model.objects.filter(pk=dest_id).first() if str(dest_id or '').isdigit() else None
    if dest is None:
        raise ValidationError('جهة التوريد غير موجودة.')
    if dest_type == InternalTransfer.TREASURY and dest.pk == session.treasury_id:
        raise ValidationError('لا يمكن التوريد إلى نفس خزينة الوردية.')
    field = {InternalTransfer.TREASURY: 'to_treasury', InternalTransfer.BANK: 'to_bank_account',
             InternalTransfer.EWALLET: 'to_ewallet'}[dest_type]
    tr = InternalTransfer.objects.create(
        amount=amount, from_account_type=InternalTransfer.TREASURY, from_treasury=session.treasury,
        to_account_type=dest_type, **{field: dest}, pos_session=session,
        description=f'توريد نقدية درج الوردية #{session.pk} عند الإغلاق',
        created_by=user if getattr(user, 'pk', None) else None,
    )
    tr.confirm_transfer(user=user)
    return tr
