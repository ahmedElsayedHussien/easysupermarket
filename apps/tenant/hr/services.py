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

    entry = JournalEntry.objects.create(
        date=timezone.now().date(),
        reference=_next_reference('PR-'),
        description=f"استحقاق راتب {display_username(payroll.employee.user)} لشهر {payroll.month}/{payroll.year}",
        status=JournalEntry.POSTED,
    )

    # Debit Expense (the salary before loan instalments); a sales commission goes to 5215
    commission = payroll.commission_pay or 0
    JournalItem.objects.create(
        entry=entry,
        account=salaries_exp_acc,
        debit=gross - commission,
        credit=0,
        description=f"مصروف راتب لشهر {payroll.month}/{payroll.year}"
    )
    if commission > 0:
        JournalItem.objects.create(entry=entry, account=_get_system_account('5215'), debit=commission, credit=0,
                                   description=f"عمولة مبيعات {payroll.month}/{payroll.year}")

    # Company share of social insurance: an extra cost, owed to the insurance authority
    if employer > 0:
        JournalItem.objects.create(entry=entry, account=_get_system_account('5212'), debit=employer, credit=0,
                                   description=f"حصة المنشأة في التأمينات {payroll.month}/{payroll.year}")
    if insurance + employer > 0:
        JournalItem.objects.create(entry=entry, account=_get_system_account('2165'), debit=0,
                                   credit=insurance + employer,
                                   description=f"تأمينات اجتماعية مستحقة (الموظف + المنشأة) {payroll.month}/{payroll.year}")
    if tax > 0:
        JournalItem.objects.create(entry=entry, account=_get_system_account('2166'), debit=0, credit=tax,
                                   description=f"ضريبة كسب العمل المستحقة {payroll.month}/{payroll.year}")

    # Credit the employee's loans (1135) with the instalments deducted this month
    if loans > 0:
        JournalItem.objects.create(
            entry=entry,
            account=_get_system_account('1135'),
            debit=0,
            credit=loans,
            description=f"أقساط سلف مخصومة من راتب {payroll.month}/{payroll.year}"
        )

    # Credit Liability (net to pay)
    if payroll.net_salary > 0:
        JournalItem.objects.create(
            entry=entry,
            account=accrued_salaries_acc,
            debit=0,
            credit=payroll.net_salary,
            description=f"استحقاق راتب لشهر {payroll.month}/{payroll.year}"
        )

    payroll.journal_entry = entry
    payroll.save(update_fields=['journal_entry'])
    payroll_obj.journal_entry = entry  # keep the caller's instance in sync

    return entry
