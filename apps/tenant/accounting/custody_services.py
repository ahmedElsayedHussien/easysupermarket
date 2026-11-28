"""
العهد النقدية: صرف، تسوية بالمصروفات + مرتجع الخزينة، وتحويل الباقي لسلفة على المرتب.
Account 1135 (ذمم الموظفين والعهد) holds the open custody until it is settled.
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Account, Custody, CustodyExpense
from apps.tenant.core.money import money

ZERO = Decimal('0')
CENT = Decimal('0.01')
EMPLOYEES_ACCOUNT_CODE = '1135'


def _employee_name(employee):
    from apps.tenant.core.usernames import display_username
    return employee.user.get_full_name() or display_username(employee.user)


def _guard_drawer(treasury):
    from apps.tenant.invoicing.shift_services import open_session_on_treasury
    if open_session_on_treasury(treasury) is not None:
        raise ValidationError(f'الخزينة "{treasury}" عليها وردية مفتوحة — استخدم خزينة تانية أو بعد تقفيل الوردية '
                              '(عشان حساب الدرج يفضل مظبوط).')


def _post(custody, reference, label, lines, user):
    from apps.tenant.invoicing.shift_services import _post as post_entry
    return post_entry(reference, label, lines, user, custody)


def expense_accounts():
    """Leaf expense accounts a custody can be spent on — the same list as the expense
    screen: no cost of sales, automated or salary accounts."""
    return (Account.objects.filter(account_type=Account.EXPENSE, is_active=True, parent__isnull=False)
            .exclude(is_cogs=True)
            .exclude(code__in=['5100', '5105', '5110', '5115', '5210', '5212', '5215', '5220', '5400'])
            .exclude(children__isnull=False).order_by('code'))


def create_custody(employee, amount, treasury, purpose, date=None, user=None):
    from apps.tenant.invoicing.shift_services import treasury_book_balance
    from apps.tenant.services.journal_service import _get_system_account
    amount = money(Decimal(amount))
    if amount <= 0:
        raise ValidationError('مبلغ العهدة لازم يكون أكبر من صفر.')
    if treasury is None:
        raise ValidationError('اختار الخزينة اللي هتتصرف منها العهدة.')
    if not (purpose or '').strip():
        raise ValidationError('اكتب الغرض من العهدة.')
    _guard_drawer(treasury)
    with transaction.atomic():
        if treasury_book_balance(treasury) < amount:
            raise ValidationError(f'رصيد الخزينة "{treasury}" مش مكفي لصرف {amount}.')
        custody = Custody.objects.create(employee=employee, amount=amount, treasury=treasury,
                                         purpose=purpose.strip()[:255], date=date or datetime.date.today(),
                                         created_by=user)
        treasury_acc = treasury.ensure_account()
        label = f'عهدة {custody.number} للموظف {_employee_name(employee)} - {custody.purpose}'
        custody.payout_entry = _post(custody, f'CUS-{custody.pk}', label, [
            (_get_system_account(EMPLOYEES_ACCOUNT_CODE), amount, ZERO, label),
            (treasury_acc, ZERO, amount, label),
        ], user)
        custody.save(update_fields=['payout_entry'])
    return custody


def settle_custody(custody, expenses, returned=ZERO, return_treasury=None, leftover_to_loan=False, user=None):
    """``expenses`` = [(account, amount, description)].

    spent + returned == amount   -> closed
    spent + returned <  amount   -> the rest must become a salary loan (leftover_to_loan)
    spent > amount (returned 0)  -> the extra is paid back to the employee from return_treasury"""
    from apps.tenant.services.journal_service import _get_system_account
    returned = money(Decimal(returned or 0))
    with transaction.atomic():
        c = Custody.objects.select_for_update(of=('self',)).select_related('employee__user', 'treasury').get(pk=custody.pk)
        if c.status != Custody.OPEN:
            raise ValidationError('العهدة مش مفتوحة.')
        allowed = {a.id: a for a in expense_accounts()}
        rows = []
        for account, amount, desc in expenses:
            amount = money(Decimal(amount or 0))
            if amount <= 0:
                continue
            if account is None or account.id not in allowed:
                raise ValidationError('اختار حساب مصروف صحيح لكل بند.')
            rows.append((account, amount, (desc or '').strip()[:255]))
        spent = sum((r[1] for r in rows), ZERO)
        if returned < 0:
            raise ValidationError('المرتجع مينفعش يكون بالسالب.')
        if spent + returned > c.amount and returned > 0:
            raise ValidationError(f'المصروفات ({spent}) + المرتجع ({returned}) أكبر من العهدة ({c.amount}).')
        leftover = c.amount - spent - returned
        extra = max(spent - c.amount, ZERO)
        if leftover > 0 and not leftover_to_loan:
            raise ValidationError(f'لسه فيه {leftover} من العهدة مش متسوّي — رجّعه للخزينة أو حوّله سلفة على المرتب.')
        money_treasury = return_treasury or c.treasury
        if (returned > 0 or extra > 0):
            _guard_drawer(money_treasury)

        label = f'تسوية عهدة {c.number} - {_employee_name(c.employee)}'
        lines = [(acc, amt, ZERO, f'{label} - {d or acc.name}') for acc, amt, d in rows]
        if returned > 0:
            lines.append((money_treasury.ensure_account(), returned, ZERO, f'{label} - مرتجع نقدي'))
        cleared = spent + returned - extra            # what leaves 1135 (never more than the custody)
        if cleared > 0:
            lines.append((_get_system_account(EMPLOYEES_ACCOUNT_CODE), ZERO, cleared, label))
        if extra > 0:
            lines.append((money_treasury.ensure_account(), ZERO, extra, f'{label} - فرق مصروف زيادة للموظف'))
        if lines and (spent or returned):
            c.settlement_entry = _post(c, f'CUS-{c.pk}-SET', label, lines, user)
        CustodyExpense.objects.bulk_create([CustodyExpense(custody=c, account=a, amount=m, description=d)
                                            for a, m, d in rows])
        if leftover > 0:
            from apps.tenant.hr.models import EmployeeLoan
            today = datetime.date.today()
            EmployeeLoan.objects.create(
                employee=c.employee, source=EmployeeLoan.CUSTODY, amount=leftover, installments_count=1,
                monthly_installment=leftover, start_year=today.year, start_month=today.month,
                reason=f'باقي عهدة {c.number} لم يُسوَّ', created_by=user,
            )
        c.status, c.settled_at = Custody.SETTLED, datetime.date.today()
        c.returned_amount, c.return_treasury = returned, money_treasury if (returned or extra) else None
        c.save(update_fields=['status', 'settled_at', 'returned_amount', 'return_treasury', 'settlement_entry'])
    return c


def cancel_custody(custody, user=None):
    """Cancel an open custody (the money came back untouched): its payout is reversed."""
    from apps.tenant.services.journal_service import reverse_entry_if_active
    with transaction.atomic():
        c = Custody.objects.select_for_update(of=('self',)).get(pk=custody.pk)
        if c.status != Custody.OPEN:
            raise ValidationError('العهدة مش مفتوحة.')
        _guard_drawer(c.treasury)
        if c.payout_entry_id:
            reverse_entry_if_active(c.payout_entry, reason=f'إلغاء عهدة {c.number}', user=user)
        c.status = Custody.CANCELLED
        c.save(update_fields=['status'])
    return c
