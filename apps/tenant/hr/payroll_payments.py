"""
صرف المرتبات وسداد المستحقات الحكومية عن شهر (بعد اعتماد المسير).

  salaries     Dr 2160 الرواتب والأجور المستحقة / Cr treasury or bank     (net of approved payrolls)
  government   Dr 2165 التأمينات المستحقة + Dr 2166 ضريبة كسب العمل / Cr treasury or bank
A payroll is paid once; the government dues of a month are remitted once per payroll.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Payroll, PayrollPayment

ZERO = Decimal('0')


def parse_source(value, user):
    """'t:<id>' -> treasury, 'b:<id>' -> bank account (the user's scope only)."""
    from apps.tenant.accounting.forms import scoped_treasuries
    from apps.tenant.accounting.models import BankAccount
    kind, _, pk = (value or '').partition(':')
    if kind == 't' and pk.isdigit():
        t = scoped_treasuries(user).filter(pk=int(pk)).first()
        if t:
            return t, None
    if kind == 'b' and pk.isdigit():
        b = BankAccount.objects.filter(pk=int(pk)).first()
        if b:
            return None, b
    raise ValidationError('اختار الخزينة أو الحساب البنكي اللي هيتصرف منه.')


def _source_account(treasury, bank, amount):
    from apps.tenant.invoicing.shift_services import open_session_on_treasury, treasury_book_balance
    if treasury is not None:
        if open_session_on_treasury(treasury) is not None:
            raise ValidationError(f'الخزينة "{treasury}" عليها وردية مفتوحة — اصرف من خزينة تانية أو بعد التقفيل.')
        if treasury_book_balance(treasury) < amount:
            raise ValidationError(f'رصيد الخزينة "{treasury}" مش مكفي ({amount}).')
        return treasury.ensure_account()
    acc = bank.ensure_account()
    if acc is None:
        raise ValidationError('الحساب البنكي مش مربوط بحساب في الشجرة.')
    return acc


def _post(payment, label, lines, user):
    from apps.tenant.invoicing.shift_services import _post as post_entry
    return post_entry(f'PAY-{payment.kind[:3]}-{payment.pk}', label, lines, user, payment)


def approved(payrolls):
    """Payrolls whose accrual entry is posted (اعتماد)."""
    return payrolls.filter(is_paid=True, journal_entry__isnull=False)


def unpaid_salaries(payrolls):
    return approved(payrolls).exclude(payments__kind=PayrollPayment.SALARIES).filter(net_salary__gt=0)


def government_due(payrolls):
    """(payrolls not remitted yet, insurance total, tax total) — one aggregate."""
    due = approved(payrolls).exclude(payments__kind=PayrollPayment.GOVERNMENT)
    agg = due.aggregate(ins=Sum('social_insurance'), er=Sum('employer_insurance'), tax=Sum('income_tax'))
    return due, (agg['ins'] or ZERO) + (agg['er'] or ZERO), agg['tax'] or ZERO


def pay_salaries(payrolls, year, month, treasury, bank, user=None, date=None):
    from apps.tenant.services.journal_service import _get_system_account
    with transaction.atomic():
        rows = list(unpaid_salaries(payrolls.filter(year=year, month=month)).select_for_update(of=('self',)))
        if not rows:
            raise ValidationError('مفيش مرتبات معتمدة مستنية صرف في الشهر ده.')
        amount = sum((p.net_salary for p in rows), ZERO)
        acc = _source_account(treasury, bank, amount)
        payment = PayrollPayment.objects.create(kind=PayrollPayment.SALARIES, year=year, month=month, amount=amount,
                                                treasury=treasury, bank_account=bank, created_by=user,
                                                date=date or timezone.localdate())
        payment.payrolls.set(rows)
        label = f'صرف مرتبات {month}/{year} ({len(rows)} موظف)'
        payment.journal_entry = _post(payment, label, [
            (_get_system_account('2160'), amount, ZERO, label), (acc, ZERO, amount, label)], user)
        payment.save(update_fields=['journal_entry'])
    return payment


def pay_government(payrolls, year, month, treasury, bank, user=None, date=None):
    from apps.tenant.services.journal_service import _get_system_account
    with transaction.atomic():
        due, insurance, tax = government_due(payrolls.filter(year=year, month=month))
        rows = list(due.select_for_update(of=('self',)))
        amount = insurance + tax
        if amount <= 0:
            raise ValidationError('مفيش تأمينات أو ضرايب مستحقة السداد عن الشهر ده.')
        acc = _source_account(treasury, bank, amount)
        payment = PayrollPayment.objects.create(kind=PayrollPayment.GOVERNMENT, year=year, month=month, amount=amount,
                                                insurance_amount=insurance, tax_amount=tax, treasury=treasury,
                                                bank_account=bank, created_by=user, date=date or timezone.localdate())
        payment.payrolls.set(rows)
        label = f'سداد تأمينات وضرائب مرتبات {month}/{year}'
        lines = []
        if insurance > 0:
            lines.append((_get_system_account('2165'), insurance, ZERO, f'{label} - تأمينات اجتماعية'))
        if tax > 0:
            lines.append((_get_system_account('2166'), tax, ZERO, f'{label} - ضريبة كسب العمل'))
        lines.append((acc, ZERO, amount, label))
        payment.journal_entry = _post(payment, label, lines, user)
        payment.save(update_fields=['journal_entry'])
    return payment
