"""
متبقي كل فاتورة آجلة، وربط السندات بالفواتير.

For a partner and a kind (SALE = what customers owe us, PURCHASE = what we owe suppliers):

  credit part   CREDIT invoice: its total · SPLIT invoice: its paid_credit
  remaining     credit part - credit part of its posted returns - posted voucher allocations
  settlements that are not tied to an invoice (unallocated vouchers, cheques, payments,
  opening balances...) are applied to the remaining amounts OLDEST FIRST, so the open
  invoices always add up to the partner's balance (partners.services.batch_get_partners_balances).

Everything is computed with a few grouped queries for any number of partners.
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, DecimalField, F, OuterRef, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from apps.tenant.core.money import money

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=18, decimal_places=2)
RETURN_OF = {'SALE': 'RETURN_SALE', 'PURCHASE': 'RETURN_PURCHASE'}
KIND_OF_VOUCHER = {'RECEIPT': 'SALE', 'PAYMENT': 'PURCHASE'}


def _credit_part():
    return Case(When(payment_type='CREDIT', then=F('total_amount')),
                When(payment_type='SPLIT', then=F('paid_credit')),
                default=Value(ZERO), output_field=MONEY)


def open_invoices(partners, kind, as_of=None, exclude_voucher_id=None, add_back=ZERO):
    """{partner_id: {'rows': [...], 'balance': side balance, 'open_total', 'not_on_invoices'}}.

    rows (oldest first): invoice, credit, returned, allocated, outstanding, due, days_overdue.
    ``add_back``: a posted voucher being (re)allocated — its amount is put back on the balance
    so it is not already counted as an oldest-first settlement."""
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.partners.models import Partner
    from apps.tenant.partners.services import batch_get_partners_balances
    from .models import Voucher, VoucherAllocation

    partners = list(partners)
    if not partners:
        return {}
    today = as_of or datetime.date.today()
    returned_sq = (Invoice.objects.filter(parent_invoice=OuterRef('pk'), status=Invoice.POSTED,
                                          invoice_type=RETURN_OF[kind])
                   .order_by().values('parent_invoice').annotate(s=Sum(_credit_part())).values('s'))
    allocs = VoucherAllocation.objects.filter(invoice=OuterRef('pk'), voucher__status=Voucher.POSTED)
    if exclude_voucher_id:
        allocs = allocs.exclude(voucher_id=exclude_voucher_id)
    alloc_sq = allocs.order_by().values('invoice').annotate(s=Sum('amount')).values('s')
    invoices = (Invoice.objects.filter(partner__in=partners, status=Invoice.POSTED, invoice_type=kind)
                .annotate(credit=_credit_part(),
                          returned=Coalesce(Subquery(returned_sq, output_field=MONEY), Value(ZERO), output_field=MONEY),
                          allocated=Coalesce(Subquery(alloc_sq, output_field=MONEY), Value(ZERO), output_field=MONEY))
                .filter(credit__gt=0).order_by('date', 'id'))
    by_partner = {}
    for inv in invoices:
        by_partner.setdefault(inv.partner_id, []).append(inv)

    balances = batch_get_partners_balances(partner_qs=Partner.objects.filter(pk__in=[p.pk for p in partners]))
    out = {}
    for p in partners:
        final = (balances.get(p.pk) or {}).get('final_balance') or ZERO
        side = (final if kind == 'SALE' else -final) + add_back   # what is owed on this side
        rows = []
        for inv in by_partner.get(p.pk, []):
            remaining = max(inv.credit - inv.returned - inv.allocated, ZERO)
            rows.append({'invoice': inv, 'credit': inv.credit, 'returned': inv.returned,
                         'allocated': inv.allocated, 'outstanding': remaining, 'auto_settled': ZERO})
        unexplained = sum((r['outstanding'] for r in rows), ZERO) - max(side, ZERO)
        for r in rows:                                       # unallocated settlements: oldest first
            if unexplained <= 0:
                break
            take = min(r['outstanding'], unexplained)
            r['outstanding'] -= take
            r['auto_settled'] = take
            unexplained -= take
        for r in rows:
            due = r['invoice'].due_date or r['invoice'].date
            r['due'] = due
            r['days_overdue'] = max((today - due).days, 0) if r['outstanding'] > 0 else 0
        open_rows = [r for r in rows if r['outstanding'] > 0]
        open_total = sum((r['outstanding'] for r in open_rows), ZERO)
        out[p.pk] = {'partner': p, 'rows': open_rows, 'balance': side, 'open_total': open_total,
                     'not_on_invoices': max(side - open_total, ZERO)}
    return out


AGE_BUCKETS = [(0, 30, '0-30 يوم'), (31, 60, '31-60 يوم'), (61, 90, '61-90 يوم'), (91, None, 'أكتر من 90')]


def aging(partners, kind, as_of=None):
    """Per partner: open total split into age buckets (by days past the due date)."""
    data = open_invoices(partners, kind, as_of=as_of)
    result = []
    for pid, d in data.items():
        if d['open_total'] <= 0 and d['not_on_invoices'] <= 0:
            continue
        buckets = [ZERO] * len(AGE_BUCKETS)
        for r in d['rows']:
            for i, (lo, hi, _) in enumerate(AGE_BUCKETS):
                if r['days_overdue'] >= lo and (hi is None or r['days_overdue'] <= hi):
                    buckets[i] += r['outstanding']
                    break
        result.append({**d, 'buckets': buckets,
                       'oldest': max((r['days_overdue'] for r in d['rows']), default=0)})
    result.sort(key=lambda x: (-x['open_total'], x['partner'].name))
    return result


def save_allocations(voucher, amounts):
    """Replace the voucher's invoice allocations with ``amounts`` {invoice_id: Decimal}.
    Each amount may not exceed what is open on the invoice; together not more than the voucher."""
    from apps.tenant.invoicing.models import Invoice
    from .models import VoucherAllocation
    kind = KIND_OF_VOUCHER.get(voucher.voucher_type)
    amounts = {int(k): money(Decimal(v)) for k, v in amounts.items() if Decimal(v or 0) > 0}
    with transaction.atomic():
        voucher.allocations.all().delete()
        if not amounts:
            return []
        if kind is None or voucher.partner_id is None:
            raise ValidationError('الربط بالفواتير لسندات العملاء والموردين بس.')
        if sum(amounts.values()) > voucher.amount:
            raise ValidationError(f'مجموع المبالغ المربوطة ({sum(amounts.values())}) أكبر من مبلغ السند ({voucher.amount}).')
        from .models import Voucher
        add_back = voucher.amount if voucher.status == Voucher.POSTED else ZERO
        open_map = {r['invoice'].id: r['outstanding'] for r in
                    open_invoices([voucher.partner], kind, exclude_voucher_id=voucher.pk,
                                  add_back=add_back)[voucher.partner_id]['rows']}
        rows = []
        for inv_id, amount in amounts.items():
            if inv_id not in open_map:
                raise ValidationError('فيه فاتورة مختارة مش مفتوحة على الشريك ده.')
            if amount > open_map[inv_id]:
                inv = Invoice.objects.only('invoice_number').get(pk=inv_id)
                raise ValidationError(f'المبلغ على فاتورة {inv.invoice_number} ({amount}) أكبر من المتبقي ({open_map[inv_id]}).')
            rows.append(VoucherAllocation(voucher=voucher, invoice_id=inv_id, amount=amount))
        return VoucherAllocation.objects.bulk_create(rows)
