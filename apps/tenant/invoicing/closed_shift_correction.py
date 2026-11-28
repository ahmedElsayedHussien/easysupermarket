"""
تصحيح طريقة سداد فاتورة في وردية مقفولة.

The cashier rang up a CASH sale that was really paid by card / e-wallet / transfer: the drawer came out
short by that amount and the shortage was booked when the shift closed (shift_services.post_shift_variance:
Dr 5400 — or 1135 when charged to the cashier — / Cr the drawer). The invoice and the shift are never
edited. Instead one entry dated today moves the money to where it really went:

    Dr  the card machine's bank / the e-wallet / the bank       amount
    Cr  the shortage account read FROM the shift-close entry   amount

So the shortage nets to zero, the closed shift's report stays as it was (the correction shows there as a
note), and an invoice is corrected once at most (one row per invoice, unique).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

ZERO = Decimal('0')
CENT = Decimal('0.01')


def _cash_collected(invoice):
    if invoice.payment_type == 'CASH':
        return (invoice.total_amount or ZERO) + (invoice.rounding_amount or ZERO)
    if invoice.payment_type == 'SPLIT':
        return invoice.paid_cash or ZERO
    return ZERO


def _shortage_account(session):
    """The account the shortage was debited to when the shift closed — read from that very entry."""
    entry = session.variance_entry
    if entry is None or (session.difference or ZERO) >= 0:
        raise ValidationError(f'الوردية #{session.pk} مفيهاش عجز متسجّل — مفيش حاجة تتصحح.')
    drawer = session.treasury.account_id
    debits = [i for i in entry.items.select_related('account') if i.debit > 0 and i.account_id != drawer]
    if len(debits) != 1:
        raise ValidationError(f'قيد قفل الوردية #{session.pk} مش بالشكل المتوقع — صحّحه يدوي من القيود.')
    return debits[0].account


def correct_payment(invoice, method, user, reason, bank_account=None, ewallet=None):
    from apps.tenant.accounting.posting import post_entry
    from apps.tenant.invoicing.models import ClosedShiftPaymentCorrection, Invoice, POSSession
    reason = (reason or '').strip()
    if len(reason) < 3:
        raise ValidationError('اكتب سبب التصحيح.')
    if method not in dict(ClosedShiftPaymentCorrection.METHOD_CHOICES):
        raise ValidationError('اختار الطريقة الصح: فيزا أو محفظة أو تحويل.')
    holder = ewallet if method == ClosedShiftPaymentCorrection.EWALLET else bank_account
    if holder is None:
        raise ValidationError('اختار ' + ('المحفظة' if method == ClosedShiftPaymentCorrection.EWALLET
                                          else 'حساب الفيزا / البنك') + ' اللي الفلوس راحت عليه فعلاً.')
    target = holder.ensure_account()
    with transaction.atomic():
        inv = Invoice.objects.select_for_update(of=('self',)).select_related('pos_session__treasury').get(pk=invoice.pk)
        if inv.status != Invoice.POSTED or inv.invoice_type != Invoice.SALE:
            raise ValidationError('التصحيح لفواتير البيع المرحّلة بس.')
        session = inv.pos_session
        if session is None or session.status != POSSession.CLOSED:
            raise ValidationError('الفاتورة مش تبع وردية مقفولة — غيّر طريقة السداد من الفاتورة نفسها.')
        if ClosedShiftPaymentCorrection.objects.filter(invoice=inv).exists():
            raise ValidationError(f'الفاتورة {inv.invoice_number} اتصححت قبل كده — التصحيح مرة واحدة بس.')
        session = POSSession.objects.select_for_update(of=('self',)).select_related(
            'treasury', 'variance_entry').get(pk=session.pk)
        amount = _cash_collected(inv).quantize(CENT)
        if amount < CENT:
            raise ValidationError('الفاتورة مفيهاش مبلغ نقدي يتصحح.')
        shortage_acc = _shortage_account(session)
        already = sum((c.amount for c in session.payment_corrections.all()), ZERO)
        left = abs(session.difference or ZERO).quantize(CENT) - already
        if amount > left:
            raise ValidationError(f'العجز المتبقي في الوردية #{session.pk} هو {left:.2f} بس — أقل من مبلغ الفاتورة '
                                  f'({amount:.2f}).')
        # a shortage charged to the cashier is also a salary loan: lower it by what is now explained
        loan = getattr(session, 'shortage_loan', None)
        if loan is not None and loan.status == loan.ACTIVE:
            if loan.remaining < amount:
                raise ValidationError('جزء من العجز اتخصم من مرتب الكاشير بالفعل — رجّعله الفرق يدوي الأول.')
            loan.amount -= amount
            loan.monthly_installment = min(loan.monthly_installment, loan.amount)
            if loan.amount < CENT or loan.remaining < CENT:
                loan.status = loan.SETTLED
            loan.save(update_fields=['amount', 'monthly_installment', 'status'])
        label = (f'تصحيح طريقة سداد الفاتورة {inv.invoice_number} (وردية مقفولة #{session.pk}): '
                 f'{dict(ClosedShiftPaymentCorrection.METHOD_CHOICES)[method]} بدل نقدي — {reason}')
        entry = post_entry(reference=f'CSP-{inv.invoice_number}', description=label[:500],
                           lines=[(target, amount, ZERO, label), (shortage_acc, ZERO, amount, label)],
                           user=user, source=inv)
        try:
            with transaction.atomic():
                correction = ClosedShiftPaymentCorrection.objects.create(
                    invoice=inv, session=session, method=method, bank_account=bank_account, ewallet=ewallet,
                    amount=amount, shortage_account=shortage_acc, reason=reason[:255], journal_entry=entry,
                    created_by=user if getattr(user, 'pk', None) else None)
        except IntegrityError:
            raise ValidationError(f'الفاتورة {inv.invoice_number} اتصححت قبل كده — التصحيح مرة واحدة بس.')
        from apps.tenant.core.models import AuditLog
        from django.contrib.contenttypes.models import ContentType
        AuditLog.objects.create(
            user=user if getattr(user, 'pk', None) else None, action=AuditLog.UPDATE,
            content_type=ContentType.objects.get_for_model(inv), object_id=str(inv.pk),
            object_repr=f'{inv.invoice_number}'[:255],
            changes={'تصحيح طريقة السداد (وردية مقفولة)': ['نقدي', correction.get_method_display()],
                     'المبلغ': str(amount), 'السبب': reason[:255], 'القيد': entry.reference})
    return correction
