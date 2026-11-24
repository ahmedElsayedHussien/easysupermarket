"""
How much of a return may be paid back in MONEY (cash / card / wallet / bank).

A return gives back what was really paid. For an invoice sold (or bought) on credit,
refunding cash would hand over money that never came in, while the customer still
owes the full invoice. So:

    refundable money = money paid on the original invoice
                       - money already refunded by earlier returns of it
                       + the part of its credit that has since been settled
                         (credit - credit already returned - what the partner still owes, >= 0)

Anything above that goes back as partner credit (reduces the balance / store credit),
which can then be paid out with a payment voucher if needed.
"""
from decimal import Decimal

ZERO = Decimal('0')


def money_part(inv):
    """Cash + card + wallet + bank part of an invoice."""
    if inv.payment_type == 'SPLIT':
        return sum((inv.paid_cash or ZERO, inv.paid_card or ZERO, inv.paid_ewallet or ZERO,
                    inv.paid_bank or ZERO), ZERO)
    if inv.payment_type == 'CREDIT':
        return ZERO
    return inv.total_amount or ZERO


def credit_part(inv):
    if inv.payment_type == 'SPLIT':
        return inv.paid_credit or ZERO
    if inv.payment_type == 'CREDIT':
        return inv.total_amount or ZERO
    return ZERO


def money_refund_allowance(original, exclude_return_id=None):
    """Max money that may still be refunded on returns of ``original``."""
    from apps.tenant.invoicing.models import Invoice
    previous = original.return_invoices.filter(status=Invoice.POSTED)
    if exclude_return_id:
        previous = previous.exclude(pk=exclude_return_id)
    money_back = sum((money_part(r) for r in previous), ZERO)
    credit_back = sum((credit_part(r) for r in previous), ZERO)

    credit = credit_part(original)
    settled = ZERO
    if credit > 0 and original.partner_id:
        partner = original.partner
        owed = partner.customer_balance if original.invoice_type == Invoice.SALE else partner.supplier_balance
        open_credit = max(ZERO, credit - credit_back)
        settled = min(open_credit, max(ZERO, open_credit - max(owed or ZERO, ZERO)))
    return max(ZERO, money_part(original) - money_back + settled)
