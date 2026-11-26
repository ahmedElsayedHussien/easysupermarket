"""
Accounting service for maintenance tickets.
Posts a compound journal entry on delivery:

  Dr. Treasury / Bank / Wallet / Customer  ← total_revenue
  Dr. COGS-Maintenance (تكلفة قطع الغيار)  ← parts_cost_total

  Cr. Revenue-Maintenance (إيرادات الصيانة)← labor_cost
  Cr. Revenue-Sales (إيرادات مبيعات)        ← parts_selling_total
  Cr. Inventory (المخزون)                  ← parts_cost_total
"""
from decimal import Decimal
from apps.tenant.services.journal_service import _get_system_account
from apps.tenant.accounting.models import JournalEntry, JournalItem


def _get_customer_account(customer):
    if customer:
        if getattr(customer, 'receivable_account', None):
            return customer.receivable_account
        if getattr(customer, 'account', None):
            return customer.account
    return _get_system_account('1210')  # الذمم المدينة العامة


def post_maintenance_journal(ticket, user) -> JournalEntry:
    """
    Create and post the accounting journal entry for a delivered maintenance ticket.
    Called inside ticket.deliver() which is already atomic.
    """
    labor   = ticket.labor_cost
    parts_s = ticket.parts_selling_total   # parts at selling price
    parts_c = ticket.parts_cost_total      # parts at actual FIFO cost
    total   = ticket.total_revenue         # labor + parts_selling

    if total == Decimal('0') and parts_c == Decimal('0'):
        return None  # Nothing to record (free warranty ticket)

    # Fetch accounts (codes defined in setup_chart_of_accounts)
    acc_revenue_m  = _get_system_account('4300')   # إيرادات الصيانة والمصنعيات
    acc_revenue_s  = _get_system_account('4100')   # المبيعات (قطع الغيار)
    acc_cogs_m     = _get_system_account('5105')   # تكلفة قطع الغيار المستهلكة
    acc_inventory  = _get_system_account('1140')   # المخزون

    je = None   # lines are collected below and posted at the end (accounting.posting)

    lines = []

    # --- DEBIT SIDE (الجانب المدين) ---
    pm = ticket.payment_method
    customer_acc = _get_customer_account(ticket.customer)

    if pm == ticket.PAYMENT_CASH:
        paid_acc = ticket.treasury.account if (ticket.treasury and ticket.treasury.account) else customer_acc
        if total > 0:
            lines.append(JournalItem(
                account=paid_acc, debit=total, credit=Decimal('0'),
                description=f'تحصيل نقدي صيانة #{ticket.id}'
            ))
    elif pm == ticket.PAYMENT_BANK_TRANSFER:
        paid_acc = ticket.bank_account.account if (ticket.bank_account and ticket.bank_account.account) else customer_acc
        if total > 0:
            lines.append(JournalItem(
                account=paid_acc, debit=total, credit=Decimal('0'),
                description=f'تحصيل بنكي/انستا باي صيانة #{ticket.id}'
            ))
    elif pm == ticket.PAYMENT_EWALLET:
        paid_acc = ticket.ewallet.account if (ticket.ewallet and ticket.ewallet.account) else customer_acc
        if total > 0:
            lines.append(JournalItem(
                account=paid_acc, debit=total, credit=Decimal('0'),
                description=f'تحصيل محفظة إلكترونية صيانة #{ticket.id}'
            ))
    elif pm == ticket.PAYMENT_CREDIT:
        if total > 0:
            lines.append(JournalItem(
                account=customer_acc, debit=total, credit=Decimal('0'),
                description=f'إثبات مستحق آجل صيانة #{ticket.id} على العميل {ticket.customer.name}'
            ))
    elif pm == ticket.PAYMENT_SPLIT:
        paid = ticket.paid_amount or Decimal('0')
        rem = ticket.remaining_amount or Decimal('0')

        fin_acc = None
        if ticket.treasury and ticket.treasury.account:
            fin_acc = ticket.treasury.account
        elif ticket.bank_account and ticket.bank_account.account:
            fin_acc = ticket.bank_account.account
        elif ticket.ewallet and ticket.ewallet.account:
            fin_acc = ticket.ewallet.account
        else:
            fin_acc = customer_acc

        if paid > 0:
            lines.append(JournalItem(
                account=fin_acc, debit=paid, credit=Decimal('0'),
                description=f'تحصيل فوري سداد جزئي صيانة #{ticket.id}'
            ))
        if rem > 0:
            lines.append(JournalItem(
                account=customer_acc, debit=rem, credit=Decimal('0'),
                description=f'متبقي آجل صيانة #{ticket.id} على العميل {ticket.customer.name}'
            ))
    else:
        paid_acc = ticket.treasury.account if (ticket.treasury and ticket.treasury.account) else customer_acc
        if total > 0:
            lines.append(JournalItem(
                account=paid_acc, debit=total, credit=Decimal('0'),
                description=f'تحصيل صيانة #{ticket.id}'
            ))

    # Debit for consumed inventory cost (COGS)
    if parts_c > 0:
        lines.append(JournalItem(
            account=acc_cogs_m,
            debit=parts_c,
            credit=Decimal('0'),
            description='تكلفة قطع الغيار المستهلكة'
        ))

    # --- CREDIT SIDE (الجانب الدائن) ---
    if labor > 0:
        lines.append(JournalItem(
            account=acc_revenue_m,
            debit=Decimal('0'),
            credit=labor,
            description='إيرادات المصنعية'
        ))
    if parts_s > 0:
        lines.append(JournalItem(
            account=acc_revenue_s,
            debit=Decimal('0'),
            credit=parts_s,
            description='إيرادات بيع قطع الغيار'
        ))
    if parts_c > 0:
        lines.append(JournalItem(
            account=acc_inventory,
            debit=Decimal('0'),
            credit=parts_c,
            description='خصم تكلفة قطع الغيار من المخزون'
        ))

    from apps.tenant.accounting.posting import post_entry
    return post_entry(
        date=ticket.delivered_at.date(), reference=f'صيانة-#{ticket.id}',
        description=f'تسليم تذكرة صيانة #{ticket.id} — {ticket.device_model} ({ticket.get_payment_method_display()})',
        lines=[(i.account, i.debit, i.credit, i.description) for i in lines], user=user, source=ticket)
