"""
أوامر الشراء: اقتراح الكميات من النواقص، الإنشاء، والاستلام بفاتورة شراء.

Suggestion for a warehouse: active stock items at / under their reorder level
(product.min_stock_level, else SystemSetting.low_stock_threshold). Suggested quantity =
(max_stock_level, else 2 x reorder level) - stock - quantity still open on purchase orders.
Supplier = the last supplier the item was bought from; price = last purchase price.
All quantities are summed by the database (a few grouped queries).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest

from .models import Invoice, InvoiceLine, PurchaseOrder, PurchaseOrderLine

ZERO = Decimal('0')
QTY = DecimalField(max_digits=18, decimal_places=3)


def reorder_suggestions(warehouse):
    from apps.tenant.core.settings_helpers import setting
    from apps.tenant.inventory.models import InventoryBatch, Product
    try:
        default_level = Decimal(str(setting('low_stock_threshold', 0) or 0))
    except Exception:
        default_level = ZERO
    stock = dict(InventoryBatch.objects.filter(warehouse=warehouse).values('product_id')
                 .annotate(q=Sum('quantity_remaining')).values_list('product_id', 'q'))
    on_order = dict(PurchaseOrderLine.objects.filter(order__warehouse=warehouse,
                                                     order__status__in=PurchaseOrder.OPEN_STATUSES)
                    .values('product_id').annotate(q=Sum(Greatest(F('quantity') - F('received_quantity'),
                                                                   Value(ZERO), output_field=QTY)))
                    .values_list('product_id', 'q'))
    last_supplier = (InvoiceLine.objects.filter(product=OuterRef('pk'), invoice__invoice_type=Invoice.PURCHASE,
                                                invoice__status=Invoice.POSTED)
                     .order_by('-invoice__date', '-id').values('invoice__partner')[:1])
    products = (Product.objects.filter(is_active=True).exclude(product_type='SERVICE')
                .annotate(last_supplier_id=Subquery(last_supplier)).select_related('category'))
    from apps.tenant.partners.models import Partner
    rows = []
    for p in products:
        level = p.min_stock_level if p.min_stock_level and p.min_stock_level > 0 else default_level
        if level <= 0:
            continue
        have = stock.get(p.id) or ZERO
        if have > level:
            continue
        target = p.max_stock_level if p.max_stock_level and p.max_stock_level > level else level * 2
        need = target - have - (on_order.get(p.id) or ZERO)
        if need <= 0:
            continue
        rows.append({'product': p, 'stock': have, 'level': level, 'on_order': on_order.get(p.id) or ZERO,
                     'quantity': need.quantize(Decimal('1')) if need == need.to_integral() else need,
                     'price': p.last_purchase_price or ZERO, 'supplier_id': p.last_supplier_id})
    suppliers = Partner.objects.in_bulk({r['supplier_id'] for r in rows if r['supplier_id']})
    for r in rows:
        r['supplier'] = suppliers.get(r['supplier_id'])
    rows.sort(key=lambda r: ((r['supplier'].name if r['supplier'] else 'ي'), r['product'].name))
    return rows


def create_order(supplier, warehouse, lines, user=None, expected_date=None, notes=''):
    """lines = [(product, quantity, unit_price, product_uom or None)]."""
    lines = [(p, Decimal(q), Decimal(pr or 0), u) for p, q, pr, u in lines if p is not None and Decimal(q or 0) > 0]
    if not lines:
        raise ValidationError('أضف صنف واحد على الأقل بكمية.')
    if supplier is None:
        raise ValidationError('اختار المورد.')
    with transaction.atomic():
        order = PurchaseOrder.objects.create(supplier=supplier, warehouse=warehouse, branch=warehouse.branch,
                                             expected_date=expected_date, notes=(notes or '').strip(), created_by=user)
        PurchaseOrderLine.objects.bulk_create([PurchaseOrderLine(order=order, product=p, quantity=q, unit_price=pr, uom=u)
                                               for p, q, pr, u in lines])
    return order


def receive_from_invoice(order, invoice):
    """A posted purchase invoice made from the order: add its quantities to the order lines
    (matched by product + unit) and update the order status."""
    if order is None or invoice.invoice_type != Invoice.PURCHASE:
        return
    with transaction.atomic():
        order = PurchaseOrder.objects.select_for_update(of=('self',)).get(pk=order.pk)
        lines = {(l.product_id, l.uom_id): l for l in order.lines.all()}
        by_product = {}
        for l in lines.values():
            by_product.setdefault(l.product_id, l)
        for il in invoice.lines.all():
            target = lines.get((il.product_id, il.uom_id)) or by_product.get(il.product_id)
            if target is not None:
                target.received_quantity += il.quantity
                target.save(update_fields=['received_quantity'])
        invoice.purchase_order = order
        invoice.save(update_fields=['purchase_order'])
        all_lines = list(order.lines.all())
        if all(l.received_quantity >= l.quantity for l in all_lines):
            order.status = PurchaseOrder.RECEIVED
        elif any(l.received_quantity > 0 for l in all_lines):
            order.status = PurchaseOrder.PARTIAL
        order.save(update_fields=['status'])


def invoice_prefill(order):
    """posted_data / posted_lines for the purchase invoice screen (remaining quantities)."""
    lines = [{'product_id': str(l.product_id), 'quantity': str(l.remaining), 'price': str(l.unit_price),
              'uom_id': str(l.uom_id or ''), 'expiry_date': ''} for l in order.lines.all() if l.remaining > 0]
    data = {'invoice_type': Invoice.PURCHASE, 'supplier_id': str(order.supplier_id),
            'warehouse_id': str(order.warehouse_id), 'branch_id': str(order.branch_id), 'payment_type': 'CREDIT'}
    return data, lines
