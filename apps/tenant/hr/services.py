from django.db import transaction
from django.utils import timezone
from apps.tenant.accounting.models import JournalEntry, JournalItem, Account
from apps.tenant.services.journal_service import _get_system_account, _next_reference
from apps.tenant.core.usernames import display_username

@transaction.atomic
def post_payroll(payroll):
    """
    Creates a journal entry for a confirmed payroll.
    Debits '5210' (Salaries Expense).
    Credits '2160' (Accrued Salaries Liability).
    """
    from apps.tenant.hr.models import Payroll

    # Lock the payroll row and re-check so concurrent confirms cannot post twice
    locked = Payroll.objects.select_for_update(of=('self',)).select_related('employee__user').get(pk=payroll.pk)
    if locked.journal_entry_id:
        payroll.journal_entry = locked.journal_entry
        return locked.journal_entry
    payroll_obj = payroll
    payroll = locked

    loans = payroll.loan_deduction or 0
    insurance = payroll.social_insurance or 0
    employer = payroll.employer_insurance or 0
    tax = payroll.income_tax or 0
    gross = payroll.net_salary + loans + insurance + tax   # salary cost before deductions
    if gross <= 0:
        return None

    salaries_exp_acc = _get_system_account('5210')
    accrued_salaries_acc = _get_system_account('2160')

    month = f"{payroll.month}/{payroll.year}"
    commission = payroll.commission_pay or 0
    lines = [(salaries_exp_acc, gross - commission, 0, f"مصروف راتب لشهر {month}")]
    if commission > 0:
        lines.append((_get_system_account('5215'), commission, 0, f"عمولة مبيعات {month}"))
    # Company share of social insurance: an extra cost, owed to the insurance authority
    if employer > 0:
        lines.append((_get_system_account('5212'), employer, 0, f"حصة المنشأة في التأمينات {month}"))
    if insurance + employer > 0:
        lines.append((_get_system_account('2165'), 0, insurance + employer,
                      f"تأمينات اجتماعية مستحقة (الموظف + المنشأة) {month}"))
    if tax > 0:
        lines.append((_get_system_account('2166'), 0, tax, f"ضريبة كسب العمل المستحقة {month}"))
    # the employee's loan instalments deducted this month (1135)
    if loans > 0:
        lines.append((_get_system_account('1135'), 0, loans, f"أقساط سلف مخصومة من راتب {month}"))
    if payroll.net_salary > 0:
        lines.append((accrued_salaries_acc, 0, payroll.net_salary, f"استحقاق راتب لشهر {month}"))
    from apps.tenant.accounting.posting import post_entry
    entry = post_entry(date=timezone.now().date(), reference=_next_reference('PR-'),
                       description=f"استحقاق راتب {display_username(payroll.employee.user)} لشهر {month}",
                       lines=lines, source=payroll)

    payroll.journal_entry = entry
    payroll.save(update_fields=['journal_entry'])
    payroll_obj.journal_entry = entry  # keep the caller's instance in sync

    return entry
