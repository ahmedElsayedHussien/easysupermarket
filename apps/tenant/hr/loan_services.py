"""
سلف الموظفين: صرفها، خصم أقساطها من المسير، وتحويل عجز الكاشير لسلفة.

Accounting (account 1135 ذمم الموظفين والعهد):
  cash loan      Dr 1135 / Cr treasury                     (when it is paid out)
  shift shortage Dr 1135 / Cr drawer                       (already booked by the shift close)
  payroll        Dr 5210 (salary before instalments) / Cr 1135 (instalments) / Cr 2160 (net)
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import EmployeeLoan, Payroll, PayrollLoanDeduction

ZERO = Decimal('0')
CENT = Decimal('0.01')
EMPLOYEES_ACCOUNT_CODE = '1135'   # ذمم الموظفين والعهد


def with_balances(queryset):
    """Loans with the deducted total computed by the database (remaining = amount - it)."""
    # instalments of CONFIRMED payrolls only: a draft payroll can still be recalculated or dropped
    return queryset.annotate(_deducted=Coalesce(Sum('deductions__amount', filter=Q(deductions__payroll__is_paid=True)),
                                                Value(ZERO),
                                                output_field=DecimalField(max_digits=12, decimal_places=2)))


def _split(amount, count):
    count = max(int(count or 1), 1)
    return (Decimal(amount) / count).quantize(CENT)


def create_cash_loan(employee, amount, installments, start_year, start_month, treasury, reason='', user=None):
    """Pay a loan out of ``treasury`` and book it (Dr 1135 / Cr treasury)."""
    from apps.tenant.invoicing.shift_services import open_session_on_treasury, treasury_book_balance
    from apps.tenant.invoicing.shift_services import _post
    from apps.tenant.services.journal_service import _get_system_account

    amount = Decimal(amount).quantize(CENT)
    if amount <= 0:
        raise ValidationError('مبلغ السلفة لازم يكون أكبر من صفر.')
    if int(installments or 0) < 1:
        raise ValidationError('عدد الأقساط لازم يكون 1 على الأقل.')
    if not (1 <= int(start_month) <= 12):
        raise ValidationError('شهر أول قسط غير صالح.')
    if treasury is None:
        raise ValidationError('اختار الخزينة اللي هتتصرف منها السلفة.')
    if open_session_on_treasury(treasury) is not None:
        raise ValidationError(f'الخزينة "{treasury}" عليها وردية مفتوحة — اصرف السلفة من خزينة تانية '
                              'أو بعد تقفيل الوردية (عشان حساب الدرج يفضل مظبوط).')
    with transaction.atomic():
        if treasury_book_balance(treasury) < amount:
            raise ValidationError(f'رصيد الخزينة "{treasury}" مش مكفي لصرف {amount}.')
        loan = EmployeeLoan.objects.create(
            employee=employee, source=EmployeeLoan.CASH, amount=amount, installments_count=int(installments),
            monthly_installment=_split(amount, installments), start_year=int(start_year),
            start_month=int(start_month), reason=(reason or '')[:255], treasury=treasury, created_by=user,
        )
        treasury_acc = treasury.ensure_account()
        if treasury_acc is None:
            raise ValidationError('الخزينة غير مرتبطة بحساب في شجرة الحسابات.')
        label = f'سلفة للموظف {_name(employee)}' + (f' - {reason}' if reason else '')
        loan.journal_entry = _post(f'LOAN-{loan.pk}', label, [
            (_get_system_account(EMPLOYEES_ACCOUNT_CODE), amount, ZERO, label),
            (treasury_acc, ZERO, amount, label),
        ], user, loan)
        loan.save(update_fields=['journal_entry'])
    return loan


def shortage_loan_for_session(session, user=None):
    """A shift shortage charged to the cashier becomes a loan deducted from his next payroll
    (the money is already on 1135 from the shift close). Once per shift; None when the
    cashier has no employee profile."""
    employee = getattr(session.user, 'employee_profile', None) if session.user_id else None
    amount = abs(session.difference or ZERO).quantize(CENT)
    if employee is None or amount < CENT:
        return None
    day = timezone.localdate(session.end_time) if session.end_time else timezone.localdate()
    loan, _ = EmployeeLoan.objects.get_or_create(pos_session=session, defaults={
        'employee': employee, 'source': EmployeeLoan.SHORTAGE, 'amount': amount, 'installments_count': 1,
        'monthly_installment': amount, 'start_year': day.year, 'start_month': day.month, 'date': day,
        'reason': f'عجز درج الوردية #{session.pk}', 'created_by': user,
    })
    return loan


def cancel_loan(loan, user=None):
    """Cancel a loan nothing was deducted from yet (a cash loan's entry is reversed)."""
    from apps.tenant.services.journal_service import reverse_entry_if_active
    with transaction.atomic():
        loan = EmployeeLoan.objects.select_for_update(of=('self',)).get(pk=loan.pk)
        if loan.status != EmployeeLoan.ACTIVE:
            raise ValidationError('السلفة مش سارية.')
        if loan.deductions.exists():
            raise ValidationError('اتخصم منها أقساط في مسير — مينفعش تتلغي.')
        if loan.source == EmployeeLoan.SHORTAGE:
            raise ValidationError('سلفة عجز الوردية بتتسدد من المرتب ومينفعش تتلغي من هنا.')
        if loan.journal_entry_id:
            reverse_entry_if_active(loan.journal_entry, reason=f'إلغاء سلفة #{loan.pk}', user=user)
        loan.status = EmployeeLoan.CANCELLED
        loan.save(update_fields=['status'])
    return loan


def due_installments(employees, year, month, exclude_payroll_ids=()):
    """{employee_id: [(loan, instalment due this month)]} for active loans that started on or
    before the month; an instalment never exceeds what is left of the loan. 2 queries."""
    period = year * 12 + month
    loans = EmployeeLoan.objects.filter(employee__in=employees, status=EmployeeLoan.ACTIVE).order_by('date', 'id')
    # deductions already made in OTHER payrolls (the month's own unconfirmed payroll is rebuilt)
    other = dict(PayrollLoanDeduction.objects.filter(loan__employee__in=employees, loan__status=EmployeeLoan.ACTIVE)
                 .exclude(payroll_id__in=list(exclude_payroll_ids)).values('loan_id')
                 .annotate(s=Sum('amount')).values_list('loan_id', 's'))
    out = {}
    for loan in loans:
        if loan.start_year * 12 + loan.start_month > period:
            continue
        left = loan.amount - (other.get(loan.id) or ZERO)
        due = min(loan.monthly_installment, left)
        if due >= CENT:
            out.setdefault(loan.employee_id, []).append((loan, due.quantize(CENT)))
    return out


def apply_to_payroll(payroll, dues, net_before_loans):
    """Rebuild the payroll's loan deductions from ``dues`` [(loan, amount)], never taking the
    net below zero (what does not fit stays on the loan for the next months).
    Returns the total deducted."""
    if Payroll.objects.filter(pk=payroll.pk, is_paid=True).exists():
        raise ValidationError(Payroll.LOCKED_MSG)
    payroll.loan_deductions.all().delete()
    room = max(net_before_loans, ZERO)
    total = ZERO
    rows = []
    for loan, due in dues:
        take = min(due, room - total)
        if take < CENT:
            break
        rows.append(PayrollLoanDeduction(payroll=payroll, loan=loan, amount=take))
        total += take
    PayrollLoanDeduction.objects.bulk_create(rows)
    return total


def settle_paid_loans(payroll):
    """After a payroll is confirmed: loans of its deductions that are fully repaid are closed."""
    ids = list(payroll.loan_deductions.values_list('loan_id', flat=True))
    for loan in with_balances(EmployeeLoan.objects.filter(id__in=ids, status=EmployeeLoan.ACTIVE)):
        if loan.remaining < CENT:
            loan.status = EmployeeLoan.SETTLED
            loan.save(update_fields=['status'])


def _name(employee):
    from apps.tenant.core.usernames import display_username
    return employee.user.get_full_name() or display_username(employee.user)
