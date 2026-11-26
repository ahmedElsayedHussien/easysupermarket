"""
Partner balances FROM THE LEDGER — the single source of truth.

Every partner has its own sub-accounts (customer: receivable_account 1130-x, supplier: payable_account
2110-x) and every document that touches a partner posts there: credit invoices / their returns, the
credit leg of split invoices, receipt / payment vouchers, cheques, credit e-service recharges, the
opening balance. A manual journal entry on those accounts (bad-debt write-off, allowed discount,
customer↔supplier offset…) therefore counts too, and the partner's balance always equals the trial
balance.

    customer balance = Σ debit − Σ credit on receivable_account   (+ = the customer owes us)
    supplier balance = Σ credit − Σ debit on payable_account      (+ = we owe the supplier)

Only POSTED entries count. ``ledger_balance_map`` does any number of partners in one query.
"""
from decimal import Decimal

from django.db.models import Sum

ZERO = Decimal('0')


def _accounts(partner):
    """(customer account id, supplier account id) of a partner (None when it has none)."""
    cust = partner.receivable_account_id
    supp = partner.payable_account_id
    if cust is None and partner.is_customer and partner.account_id and partner.account_id != supp:
        cust = partner.account_id
    if supp is None and partner.is_supplier and partner.account_id and partner.account_id != cust:
        supp = partner.account_id
    return cust, supp


def account_nets(account_ids, as_of=None, before=None):
    """{account_id: Σ debit − Σ credit} over POSTED entries (optionally dated ≤ as_of / < before)."""
    from apps.tenant.accounting.models import JournalItem
    ids = [a for a in set(account_ids) if a]
    if not ids:
        return {}
    qs = JournalItem.objects.filter(account_id__in=ids, entry__status='POSTED')
    if as_of is not None:
        qs = qs.filter(entry__date__lte=as_of)
    if before is not None:
        qs = qs.filter(entry__date__lt=before)
    rows = qs.order_by().values('account_id').annotate(d=Sum('debit'), c=Sum('credit'))
    return {r['account_id']: (r['d'] or ZERO) - (r['c'] or ZERO) for r in rows}


def ledger_balance_map(partners, as_of=None):
    """{partner_id: (customer_balance, supplier_balance)} for all ``partners`` in one query."""
    partners = list(partners)
    pairs = {p.pk: _accounts(p) for p in partners}
    nets = account_nets([a for pair in pairs.values() for a in pair], as_of=as_of)
    return {pid: (nets.get(cust, ZERO) if cust else ZERO, -nets.get(supp, ZERO) if supp else ZERO)
            for pid, (cust, supp) in pairs.items()}


def prime(partners, as_of=None):
    """Set the cached balances on each partner instance (list pages: no query per partner)."""
    partners = list(partners)
    balances = ledger_balance_map(partners, as_of)
    for p in partners:
        p._cached_customer_balance, p._cached_supplier_balance = balances.get(p.pk, (ZERO, ZERO))
    return partners
