"""
Accounting service for maintenance tickets.
Posts a compound journal entry on delivery:

  Dr. Treasury (الخزينة)                   ← total_revenue
  Dr. COGS-Maintenance (تكلفة قطع الغيار)  ← parts_cost_total

  Cr. Revenue-Maintenance (إيرادات الصيانة)← labor_cost
  Cr. Revenue-Sales (إيرادات مبيعات)        ← parts_selling_total
  Cr. Inventory (المخزون)                  ← parts_cost_total
"""
from decimal import Decimal
from apps.tenant.services.journal_service import _get_system_account
from apps.tenant.accounting.models import JournalEntry, JournalLine


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
    acc_treasury   = ticket.treasury.account  # Linked account on Treasury model
    acc_revenue_m  = _get_system_account('4300')   # إيرادات الصيانة والمصنعيات
    acc_revenue_s  = _get_system_account('4100')   # المبيعات (قطع الغيار)
    acc_cogs_m     = _get_system_account('5105')   # تكلفة قطع الغيار المستهلكة
    acc_inventory  = _get_system_account('1140')   # المخزون

    je = JournalEntry.objects.create(
        date=ticket.delivered_at.date(),
        reference=f'صيانة-#{ticket.id}',
        description=f'تسليم تذكرة صيانة #{ticket.id} — {ticket.device_model}',
        created_by=user,
        status=JournalEntry.POSTED,
    )

    lines = []

    # --- DEBIT ---
    if total > 0:
        lines.append(JournalLine(
            journal_entry=je,
            account=acc_treasury,
            debit=total,
            credit=Decimal('0'),
            description='تحصيل إيرادات الصيانة'
        ))
    if parts_c > 0:
        lines.append(JournalLine(
            journal_entry=je,
            account=acc_cogs_m,
            debit=parts_c,
            credit=Decimal('0'),
            description='تكلفة قطع الغيار المستهلكة'
        ))

    # --- CREDIT ---
    if labor > 0:
        lines.append(JournalLine(
            journal_entry=je,
            account=acc_revenue_m,
            debit=Decimal('0'),
            credit=labor,
            description='إيرادات المصنعية'
        ))
    if parts_s > 0:
        lines.append(JournalLine(
            journal_entry=je,
            account=acc_revenue_s,
            debit=Decimal('0'),
            credit=parts_s,
            description='إيرادات بيع قطع الغيار'
        ))
    if parts_c > 0:
        lines.append(JournalLine(
            journal_entry=je,
            account=acc_inventory,
            debit=Decimal('0'),
            credit=parts_c,
            description='خصم تكلفة قطع الغيار من المخزون'
        ))

    JournalLine.objects.bulk_create(lines)
    return je
