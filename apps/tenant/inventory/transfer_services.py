"""
Posting a multi-item stock transfer (one StockTransfer document, FIFO cost per line, all lines or none).
Used by the stock-transfer screen and by van loading / unloading (invoicing.van_services).
"""
import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, Sum

from .models import StockMovement, StockTransfer, StockTransferLine


def post_transfer(from_wh, to_wh, items, user=None, notes='', date=None):
    """items: [{'product', 'uom' (ProductUoM|None), 'factor', 'qty' (in uom), 'base_qty'}].
    Raises ValueError('"<product>": <reason>') and moves nothing when one line can't move."""
    from apps.tenant.services.fifo_engine import transfer_stock
    if from_wh.pk == to_wh.pk:
        raise ValueError('لا يمكن التحويل من وإلى نفس المستودع.')
    items = [i for i in items if i['base_qty'] > 0]
    if not items:
        raise ValueError('مفيش كميات تتحوّل.')
    with transaction.atomic():
        doc = StockTransfer.objects.create(date=date or datetime.date.today(), from_warehouse=from_wh,
                                           to_warehouse=to_wh, notes=notes or '', created_by=user)
        total = Decimal('0')
        for item in items:
            product = item['product']
            try:
                transfer_stock(product, from_wh, to_wh, item['base_qty'], reference=doc.number)
            except ValueError as e:
                raise ValueError(f'"{product.name}": {e}')
            cost = StockMovement.objects.filter(reference=doc.number, product=product,
                                                movement_type='TRANSFER_OUT').aggregate(
                c=Sum(F('quantity') * F('unit_cost'), output_field=DecimalField(max_digits=30, decimal_places=8))
            )['c'] or Decimal('0')
            StockTransferLine.objects.create(
                transfer=doc, product=product, uom=item.get('uom'), quantity=item['qty'],
                conversion_factor=item.get('factor') or Decimal('1'), base_quantity=item['base_qty'],
                unit_cost=(cost / item['base_qty']) if item['base_qty'] else Decimal('0'),
            )
            total += cost
        doc.total_cost = total
        doc.save(update_fields=['total_cost'])
    return doc
