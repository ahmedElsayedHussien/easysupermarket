from django.db import transaction
from django.utils import timezone
from apps.tenant.accounting.models import JournalEntry, JournalItem, Account
from apps.tenant.services.journal_service import _get_system_account, _next_reference

@transaction.atomic
def post_payroll(payroll):
    """
    Creates a journal entry for a confirmed payroll.
    Debits '5210' (Salaries Expense).
    Credits '2160' (Accrued Salaries Liability).
    """
    if payroll.journal_entry:
        return payroll.journal_entry

    if payroll.net_salary <= 0:
        return None

    salaries_exp_acc = _get_system_account('5210')
    accrued_salaries_acc = _get_system_account('2160')

    entry = JournalEntry.objects.create(
        date=timezone.now().date(),
        reference=_next_reference('PR-'),
        description=f"استحقاق راتب {payroll.employee.user.username} لشهر {payroll.month}/{payroll.year}",
        status=JournalEntry.POSTED,
    )

    # Debit Expense
    JournalItem.objects.create(
        entry=entry,
        account=salaries_exp_acc,
        debit=payroll.net_salary,
        credit=0,
        description=f"مصروف راتب لشهر {payroll.month}/{payroll.year}"
    )

    # Credit Liability
    JournalItem.objects.create(
        entry=entry,
        account=accrued_salaries_acc,
        debit=0,
        credit=payroll.net_salary,
        description=f"استحقاق راتب لشهر {payroll.month}/{payroll.year}"
    )

    payroll.journal_entry = entry
    payroll.save()

    return entry
