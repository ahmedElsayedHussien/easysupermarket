"""
تصحيح طريقة سداد فاتورة في وردية مقفولة — في الاتجاهين.

The invoice and the shift are never edited; one entry dated today moves the money to where it really went.

1. Recorded CASH, really paid by card / e-wallet / transfer: the drawer came out SHORT by that amount and the
   shortage was booked at close (shift_services.post_shift_variance: Dr 5400 — or 1135 when charged to the
   cashier — / Cr the drawer):
       Dr  the card machine's bank / the e-wallet / the bank       amount
       Cr  the shortage account read FROM the shift-close entry   amount

2. Recorded card / e-wallet / transfer, really paid CASH: the drawer came out OVER by that amount (booked at
   close: Dr the drawer / Cr 5400), while the sale entry debited a bank / wallet that never got the money:
       Dr  the overage account read FROM the shift-close entry    amount
       Cr  the bank / e-wallet read FROM the posted sale entry    amount

A SPLIT invoice is corrected for the wrong leg only (its cash part, or one card / wallet / transfer part), and
the amount may be lowered when only some of a leg was wrong.

Not covered: two opposite mistakes in ONE shift (a cash sale really paid by card and a card sale really paid in
cash) — they cancel in the drawer, so the variance never shows them and neither fits within it. Those are
fixed by a manual entry (the screen says so).

Either way the variance nets to zero, the closed shift's report stays as it was (the correction shows there as
a note), the corrections of a shift never exceed its variance, and an invoice is corrected once at most.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

ZERO = Decimal('0')
CENT = Decimal('0.01')
NON_CASH = ('CARD', 'EWALLET', 'BANK_TRANSFER')


def _cash_collected(invoice):
    if invoice.payment_type == 'CASH':
        return (invoice.total_amount or ZERO) + (invoice.rounding_amount or ZERO)
    if invoice.payment_type == 'SPLIT':
        return invoice.paid_cash or ZERO
    return ZERO


def _variance_account(session, shortage):
    """The account the shift's variance went to at close — read from that very entry: the debit side of a
    shortage, the credit side of an overage (the drawer is the other side)."""
    entry = session.variance_entry
    diff = session.difference or ZERO
    if entry is None or (diff >= 0 if shortage else diff <= 0):
        kind = 'عجز' if shortage else 'زيادة'
        raise ValidationError(f'الوردية #{session.pk} مفيهاش {kind} متسجّلة — مفيش حاجة تتصحح.')
    drawer = session.treasury.account_id
    side = [i for i in entry.items.select_related('account')
            if (i.debit if shortage else i.credit) > 0 and i.account_id != drawer]
    if len(side) != 1:
        raise ValidationError(f'قيد قفل الوردية #{session.pk} مش بالشكل المتوقع — صحّحه يدوي من القيود.')
    return side[0].account


LEG_FIELD = {'CASH': 'paid_cash', 'CARD': 'paid_card', 'EWALLET': 'paid_ewallet', 'BANK_TRANSFER': 'paid_bank'}


def recorded_legs(invoice):
    """{method: amount} of what the invoice recorded per payment method (a single method carries the total)."""
    if invoice.payment_type == 'CASH':
        return {'CASH': _cash_collected(invoice)}
    if invoice.payment_type in NON_CASH:
        return {invoice.payment_type: invoice.total_amount or ZERO}
    if invoice.payment_type == 'SPLIT':
        return {m: getattr(invoice, f) or ZERO for m, f in LEG_FIELD.items() if (getattr(invoice, f) or ZERO) > 0}
    return {}


def _booked_money_line(invoice, leg, leg_amount):
    """The debit line of the POSTED sale entry that booked this card / wallet / transfer leg — read from the
    entry itself, never re-resolved from today's defaults."""
    from apps.tenant.accounting.models import BankAccount, EWallet
    entry = invoice.journal_entry
    if entry is None or entry.status != 'POSTED' or entry.is_reversed:
        raise ValidationError(f'قيد بيع الفاتورة {invoice.invoice_number} مش مرحّل — صحّح يدوي من القيود.')
    holders = EWallet if leg == 'EWALLET' else BankAccount
    money = set(holders.objects.exclude(account=None).values_list('account_id', flat=True))
    lines = [i for i in entry.items.select_related('account') if i.debit > 0 and i.account_id in money]
    if len(lines) > 1:
        lines = [i for i in lines if i.debit == leg_amount]
    if len(lines) != 1:
        raise ValidationError(f'مش واضح من قيد بيع الفاتورة {invoice.invoice_number} الحساب اللي اتقيد عليه '
                              'المبلغ — صحّح يدوي من القيود.')
    return lines[0]


def correct_payment(invoice, method, user, reason, bank_account=None, ewallet=None, amount=None,
                    wrong_method=None):
    """``method``: what the customer really paid with. ``wrong_method``: for a SPLIT invoice corrected to cash,
    which of its card / wallet / transfer legs was wrong. ``amount``: defaults to the whole wrong leg."""
    from apps.tenant.accounting.posting import post_entry
    from apps.tenant.invoicing.models import ClosedShiftPaymentCorrection as C, Invoice, POSSession
    reason = (reason or '').strip()
    if len(reason) < 3:
        raise ValidationError('اكتب سبب التصحيح.')
    if method not in dict(C.METHOD_CHOICES):
        raise ValidationError('اختار الطريقة الصح.')
    to_cash = method == C.CASH
    target = None
    if to_cash:
        bank_account = ewallet = None
    else:
        holder = ewallet if method == C.EWALLET else bank_account
        if holder is None:
            raise ValidationError('اختار ' + ('المحفظة' if method == C.EWALLET else 'حساب الفيزا / البنك')
                                  + ' اللي الفلوس راحت عليه فعلاً.')
        target = holder.ensure_account()
        if method == C.EWALLET:
            bank_account = None
        else:
            ewallet = None
    names = dict(C.METHOD_CHOICES)
    with transaction.atomic():
        inv = Invoice.objects.select_for_update(of=('self',)).select_related(
            'pos_session__treasury', 'journal_entry').get(pk=invoice.pk)
        if inv.status != Invoice.POSTED or inv.invoice_type != Invoice.SALE:
            raise ValidationError('التصحيح لفواتير البيع المرحّلة بس.')
        session = inv.pos_session
        if session is None or session.status != POSSession.CLOSED:
            raise ValidationError('الفاتورة مش تبع وردية مقفولة — غيّر طريقة السداد من الفاتورة نفسها.')
        if C.objects.filter(invoice=inv).exists():
            raise ValidationError(f'الفاتورة {inv.invoice_number} اتصححت قبل كده — التصحيح مرة واحدة بس.')
        session = POSSession.objects.select_for_update(of=('self',)).select_related(
            'treasury', 'variance_entry').get(pk=session.pk)
        legs = recorded_legs(inv)
        if to_cash:
            recorded = wrong_method if inv.payment_type == 'SPLIT' else inv.payment_type
            if recorded not in NON_CASH or legs.get(recorded, ZERO) <= 0:
                raise ValidationError('«نقدي» بيصحح جزء اتسجل فيزا أو محفظة أو تحويل — '
                                      + ('اختار الجزء اللي اتسجل غلط.' if inv.payment_type == 'SPLIT'
                                         else f'الفاتورة دي اتسجلت {inv.get_payment_type_display()}.'))
        else:
            recorded = C.CASH
            if legs.get(C.CASH, ZERO) <= 0:
                raise ValidationError('الفاتورة مفيهاش مبلغ نقدي يتصحح.')
        leg_amount = legs[recorded].quantize(CENT)
        booked = _booked_money_line(inv, recorded, leg_amount) if to_cash else None
        if booked is not None:
            leg_amount = min(leg_amount, booked.debit)
        if amount in (None, ''):
            amount = leg_amount
        try:
            amount = Decimal(str(amount)).quantize(CENT)
        except Exception:
            raise ValidationError('المبلغ مش رقم صحيح.')
        if amount < CENT:
            raise ValidationError('الفاتورة مفيهاش مبلغ يتصحح.')
        if amount > leg_amount:
            raise ValidationError(f'المبلغ أكبر من اللي اتسجل {names[recorded]} على الفاتورة ({leg_amount:.2f}).')
        variance_acc = _variance_account(session, shortage=not to_cash)
        already = sum((c.amount for c in session.payment_corrections.all()), ZERO)
        left = abs(session.difference or ZERO).quantize(CENT) - already
        if amount > left:
            kind = 'الزيادة المتبقية' if to_cash else 'العجز المتبقي'
            raise ValidationError(f'{kind} في الوردية #{session.pk} {left:.2f} بس — أقل من مبلغ الفاتورة '
                                  f'({amount:.2f}).')
        if to_cash:
            lines = [(variance_acc, amount, ZERO), (booked.account, ZERO, amount)]
        else:
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
            lines = [(target, amount, ZERO), (variance_acc, ZERO, amount)]
        label = (f'تصحيح طريقة سداد الفاتورة {inv.invoice_number} (وردية مقفولة #{session.pk}): '
                 f'{names[method]} بدل {names[recorded]} — {reason}')
        entry = post_entry(reference=f'CSP-{inv.invoice_number}', description=label[:500],
                           lines=[(acc, d, c, label) for acc, d, c in lines], user=user, source=inv)
        try:
            with transaction.atomic():
                correction = C.objects.create(
                    invoice=inv, session=session, recorded_method=recorded, method=method,
                    bank_account=bank_account, ewallet=ewallet, amount=amount, variance_account=variance_acc,
                    reason=reason[:255], journal_entry=entry,
                    created_by=user if getattr(user, 'pk', None) else None)
        except IntegrityError:
            raise ValidationError(f'الفاتورة {inv.invoice_number} اتصححت قبل كده — التصحيح مرة واحدة بس.')
        from apps.tenant.core.models import AuditLog
        from django.contrib.contenttypes.models import ContentType
        AuditLog.objects.create(
            user=user if getattr(user, 'pk', None) else None, action=AuditLog.UPDATE,
            content_type=ContentType.objects.get_for_model(inv), object_id=str(inv.pk),
            object_repr=f'{inv.invoice_number}'[:255],
            changes={'تصحيح طريقة السداد (وردية مقفولة)': [names[recorded], names[method]],
                     'المبلغ': str(amount), 'السبب': reason[:255], 'القيد': entry.reference})
    return correction


ADJUST_KEYS = {'CASH': 'cash', 'CARD': 'card', 'EWALLET': 'ewallet', 'BANK_TRANSFER': 'bank'}


def correction_adjustments(corrections):
    """Net per payment method of a set of corrections: + to the method really paid with, - from the method
    recorded. Added to a shift's (or a range's) sales by method, the totals match the books. ``digital`` =
    card + wallet + transfer; ``any`` says whether there was a correction at all."""
    adj = {k: ZERO for k in ADJUST_KEYS.values()}
    count = 0
    for c in corrections:
        adj[ADJUST_KEYS[c.method]] += c.amount
        adj[ADJUST_KEYS[c.recorded_method]] -= c.amount
        count += 1
    adj['digital'] = adj['card'] + adj['ewallet'] + adj['bank']
    adj['any'] = count > 0
    return adj


def corrections_by_session(session_ids):
    """{session_id: correction_adjustments(...)} for the shifts that have corrections — one query."""
    from apps.tenant.invoicing.models import ClosedShiftPaymentCorrection
    grouped = {}
    for c in ClosedShiftPaymentCorrection.objects.filter(session_id__in=list(session_ids)).only(
            'session_id', 'method', 'recorded_method', 'amount'):
        grouped.setdefault(c.session_id, []).append(c)
    return {sid: correction_adjustments(cs) for sid, cs in grouped.items()}
