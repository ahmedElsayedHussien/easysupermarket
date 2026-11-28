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
from apps.tenant.core.money import money

ZERO = Decimal('0')
CENT = Decimal('0.01')
VARIANCE_ACCOUNT_CODE = '5400'   # عجز وزيادة الخزينة (الورديات)
CASHIER_ACCOUNT_CODE = '1135'    # ذمم الموظفين والعهد

CHARGE_EXPENSE = 'EXPENSE'
CHARGE_CASHIER = 'CASHIER'



def invoice_figure_expressions():
    """Aggregate expressions of a shift's invoice figures (sales, returns, cash / card /
    wallet / bank / credit legs). Used with .aggregate() for one shift and with
    .values('pos_session').annotate() for many shifts at once — same arithmetic."""
    from django.db import models as m
    from django.db.models import Case, Count, F, Q, Sum, When
    from django.db.models.functions import Coalesce
    from apps.tenant.invoicing.models import Invoice

    DEC = m.DecimalField(max_digits=18, decimal_places=4)
    S, R = Invoice.SALE, Invoice.RETURN_SALE

    def signed(paid_field, ptype):
        """Sale adds, return subtracts (non-cash methods)."""
        return Coalesce(Sum(Case(
            When(Q(invoice_type=S, payment_type=Invoice.SPLIT), then=F(paid_field)),
            When(Q(invoice_type=S, payment_type=ptype), then=F('total_amount')),
            When(Q(invoice_type=R, payment_type=Invoice.SPLIT), then=-F(paid_field)),
            When(Q(invoice_type=R, payment_type=ptype), then=-F('total_amount')),
            default=Decimal('0'), output_field=DEC)), Decimal('0'))

    return dict(
        total_gross_sales=Coalesce(Sum('total_amount', filter=Q(invoice_type=S)), Decimal('0')),
        total_returns_amount=Coalesce(Sum('total_amount', filter=Q(invoice_type=R)), Decimal('0')),
        sales_count=Count('id', filter=Q(invoice_type=S)),
        returns_count=Count('id', filter=Q(invoice_type=R)),
        total_cash_sales=Coalesce(Sum(Case(
            When(Q(invoice_type=S, payment_type=Invoice.SPLIT), then=F('paid_cash')),
            When(Q(invoice_type=S, payment_type=Invoice.CASH), then=F('total_amount') + F('rounding_amount')),
            default=Decimal('0'), output_field=DEC)), Decimal('0')),
        total_cash_refunds=Coalesce(Sum(Case(
            When(Q(invoice_type=R, payment_type=Invoice.SPLIT), then=F('paid_cash')),
            When(Q(invoice_type=R, payment_type=Invoice.CASH), then=F('total_amount')),
            default=Decimal('0'), output_field=DEC)), Decimal('0')),
        total_card_sales=signed('paid_card', Invoice.CARD),
        total_ewallet_sales=signed('paid_ewallet', Invoice.EWALLET),
        total_bank_sales=signed('paid_bank', Invoice.BANK_TRANSFER),
        total_credit_sales=signed('paid_credit', Invoice.CREDIT),
    )


def shifts_list_rows(sessions):
    """Rows of the shifts report for a page of sessions without per-shift queries:
    invoice figures in ONE grouped query; a closed shift shows the figures stored at
    close, an open one (few) is computed live by shift_cash_summary()."""
    from apps.tenant.invoicing.models import Invoice
    sessions = list(sessions)
    zero = {k: (0 if k.endswith('_count') else Decimal('0')) for k in invoice_figure_expressions()}
    grouped = {r['pos_session']: r for r in Invoice.objects.filter(
        pos_session__in=[s.id for s in sessions], status=Invoice.POSTED).order_by()
        .values('pos_session').annotate(**invoice_figure_expressions())}
    rows = []
    for session in sessions:
        figures = {**zero, **{k: v for k, v in grouped.get(session.id, {}).items() if k != 'pos_session'}}
        row = {'session': session, **figures,
               'net_sales_amount': figures['total_gross_sales'] - figures['total_returns_amount']}
        if session.status == 'CLOSED':
            row.update(expected_balance=session.closing_balance_expected,
                       actual_balance=session.closing_balance_actual, difference=session.difference)
        else:
            live = shift_cash_summary(session)['expected_balance']
            row.update(expected_balance=live, actual_balance=live, difference=Decimal('0'))
        rows.append(row)
    return rows

def shift_cash_summary(session):
    """
    THE drawer arithmetic of a shift — used by the close screen, the shift report and
    the live POS stats, so the three always show the same figures.

    What moves the drawer (= the shift's treasury) during the shift:
      invoices of the shift (cash part; returns subtract) · cash vouchers on the drawer
      (or linked to the shift) · cash rentals on the drawer (or linked to the shift) ·
      e-payment services of the shift · maintenance tickets paid in cash into the drawer ·
      fund transfers out of / into the drawer.
    """
    from django.db import models as m
    from django.db.models import Case, Count, F, Q, Sum, When
    from django.db.models.functions import Coalesce
    from apps.tenant.accounting.eservice_dashboard_views import eservice_cash_for_session
    from apps.tenant.accounting.models import InternalTransfer, Voucher
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.rentals.models import Rental

    DEC = m.DecimalField(max_digits=18, decimal_places=4)

    start, end = session.start_time, session.end_time
    in_window = Q(created_at__gte=start) & (Q(created_at__lte=end) if end else Q())

    # --- invoices ---
    invoices = Invoice.objects.filter(pos_session=session, status=Invoice.POSTED)
    agg = invoices.aggregate(**invoice_figure_expressions())

    # --- cash vouchers on the drawer ---
    vouchers = (Voucher.objects.filter(status=Voucher.POSTED)
                .filter(Q(payment_method=Voucher.CASH) | Q(payment_method=Voucher.SPLIT, paid_cash__gt=0))
                .filter(Q(pos_session=session) | (Q(treasury=session.treasury) & in_window))
                .distinct())
    v_cash = Case(When(payment_method=Voucher.SPLIT, then=F('paid_cash')), default=F('amount'), output_field=DEC)
    v_agg = vouchers.aggregate(
        total_cash_receipts=Coalesce(Sum(v_cash, filter=Q(voucher_type=Voucher.RECEIPT)), Decimal('0')),
        total_cash_payments=Coalesce(Sum(v_cash, filter=Q(voucher_type=Voucher.PAYMENT)), Decimal('0')),
        receipts_count=Count('id', filter=Q(voucher_type=Voucher.RECEIPT)),
        payments_count=Count('id', filter=Q(voucher_type=Voucher.PAYMENT)),
    )

    # --- rentals of the shift / on the drawer ---
    rentals = Rental.objects.filter(Q(pos_session=session) | (Q(treasury=session.treasury) & in_window)).distinct()
    r_agg = rentals.aggregate(
        total_rentals_amount=Coalesce(Sum('total_amount'), Decimal('0')),
        # cash INTO THIS drawer only (a rental paid by e-wallet in the shift is not drawer cash)
        total_cash_rentals=Coalesce(Sum('total_amount', filter=Q(payment_method=Rental.PAYMENT_CASH,
                                                                 treasury=session.treasury)), Decimal('0')),
        total_credit_rentals=Coalesce(Sum('total_amount', filter=~Q(payment_method=Rental.PAYMENT_CASH)), Decimal('0')),
        rentals_count=Count('id'),
    )

    # --- fund transfers out of / into the drawer during the shift ---
    # (the cash drop done AT close is created after end_time and not counted: it
    #  happens after the count)
    out_qs = InternalTransfer.objects.filter(
        in_window, from_account_type=InternalTransfer.TREASURY, from_treasury=session.treasury,
        status__in=[InternalTransfer.POSTED, InternalTransfer.IN_TRANSIT])
    received_window = Q(received_at__gte=start) & (Q(received_at__lte=end) if end else Q())
    in_qs = InternalTransfer.objects.filter(
        to_account_type=InternalTransfer.TREASURY, to_treasury=session.treasury, status=InternalTransfer.POSTED,
    ).filter(received_window | (Q(received_at__isnull=True) & in_window))
    transfers_out = out_qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    transfers_in = in_qs.aggregate(s=Sum(Coalesce('received_amount', 'amount')))['s'] or Decimal('0')

    eservice_cash = eservice_cash_for_session(session)

    # --- maintenance tickets delivered with cash into the drawer (cash / the paid part of a split) ---
    from apps.tenant.maintenance.models import MaintenanceTicket
    delivered_window = Q(delivered_at__gte=start) & (Q(delivered_at__lte=end) if end else Q())
    maintenance_cash = (MaintenanceTicket.objects.filter(
        status=MaintenanceTicket.STATUS_DELIVERED, treasury=session.treasury,
        payment_method__in=[MaintenanceTicket.PAYMENT_CASH, MaintenanceTicket.PAYMENT_SPLIT])
        .filter(Q(pos_session=session) | (Q(pos_session__isnull=True) & delivered_window))
        .aggregate(s=Sum('paid_amount'))['s'] or Decimal('0'))

    # --- rental returns settled in cash from / into the drawer during the shift (refund − penalty) ---
    from apps.tenant.accounting.models import JournalItem
    drawer_acc = session.treasury.account_id if session.treasury_id else None
    rs = (JournalItem.objects.filter(account_id=drawer_acc, entry__status='POSTED',
                                     entry__rental_settlement__isnull=False)
          .filter(Q(entry__created_at__gte=start) & (Q(entry__created_at__lte=end) if end else Q()))
          .aggregate(d=Sum('debit'), c=Sum('credit'))) if drawer_acc else {'d': None, 'c': None}
    rental_settlements = (rs['d'] or Decimal('0')) - (rs['c'] or Decimal('0'))

    expected = (session.opening_balance + agg['total_cash_sales'] - agg['total_cash_refunds']
                + v_agg['total_cash_receipts'] - v_agg['total_cash_payments']
                + r_agg['total_cash_rentals'] + rental_settlements + eservice_cash + maintenance_cash
                + transfers_in - transfers_out)

    return {
        'posted_invoices': invoices,
        **agg,
        'net_sales_amount': agg['total_gross_sales'] - agg['total_returns_amount'],
        'shift_vouchers': vouchers,
        **v_agg,
        'shift_rentals': rentals,
        **r_agg,
        'total_eservice_cash': eservice_cash,
        'total_cash_maintenance': maintenance_cash,
        'total_rental_settlements': rental_settlements,
        'total_transfers_in': transfers_in,
        'total_transfers_out': transfers_out,
        'total_transfers_net': transfers_in - transfers_out,
        'expected_balance': expected,
    }


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
    """A posted entry dated today, linked to ``session`` (any document) — see accounting.posting."""
    from apps.tenant.accounting.posting import post_entry
    return post_entry(date=timezone.localdate(), reference=reference, description=description, lines=lines,
                      user=user, source=session)


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
    diff = money(session.difference or ZERO)
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
    if diff < 0 and session.variance_charged_to == CHARGE_CASHIER:
        # the shortage is deducted from the cashier's salary as a one-instalment loan
        from apps.tenant.hr.loan_services import shortage_loan_for_session
        shortage_loan_for_session(session, user)
    return entry


def charge_shortage_to_cashier(session, user=None):
    """Re-books a closed shift's shortage from the expense account onto the cashier (1135)."""
    from apps.tenant.invoicing.models import POSSession
    from apps.tenant.services.journal_service import reverse_entry_if_active
    with transaction.atomic():
        s = POSSession.objects.select_for_update(of=('self',)).select_related('treasury', 'user', 'variance_entry').get(pk=session.pk)
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
    amount = money(Decimal(str(amount)))
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
