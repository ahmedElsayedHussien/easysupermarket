"""
FIFO Inventory Engine
=====================
Implements First-In-First-Out (FIFO) inventory consumption and stock management.

Key functions:
  - consume_fifo_batches(): Deducts stock from oldest batches first
  - add_inventory_batch(): Creates a new FIFO batch on purchase
  - get_stock_value(): Calculates total inventory value
  - transfer_stock(): Moves stock between warehouses via FIFO

All functions use select_for_update() to prevent race conditions in concurrent
environments. Every stock change creates an immutable StockMovement audit record.
"""
from django.db import transaction
from django.db.models import Sum
from decimal import Decimal
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


@transaction.atomic
def consume_fifo_batches(
    product,
    warehouse,
    quantity_needed: Decimal,
    reference: str = '',
    notes: str = ''
) -> List[dict]:
    """
    Consume inventory from the oldest available batches (FIFO order).

    Locks batches with select_for_update() to prevent concurrent over-consumption.
    Creates a StockMovement record for each batch consumed.

    Args:
        product: Product instance to consume
        warehouse: Warehouse instance to consume from
        quantity_needed: Decimal quantity to consume
        reference: Optional reference string (e.g., invoice number)
        notes: Optional notes for audit trail

    Returns:
        List of dicts, one per batch consumed:
            [{
                'batch': InventoryBatch,
                'quantity_consumed': Decimal,
                'unit_cost': Decimal,
                'total_cost': Decimal,
            }, ...]

    Raises:
        ValueError: If insufficient stock is available
    """
    from apps.tenant.inventory.models import InventoryBatch, StockMovement

    # Lock batches in FIFO order (oldest first via Meta.ordering = ['created_at'])
    batches = InventoryBatch.objects.select_for_update().filter(
        product=product,
        warehouse=warehouse,
        quantity_remaining__gt=Decimal('0')
    ).order_by('created_at')  # Explicit FIFO order

    # Check total available stock
    total_available = batches.aggregate(
        t=Sum('quantity_remaining')
    )['t'] or Decimal('0')

    if total_available < quantity_needed and not product.allow_negative_stock:
        raise ValueError(
            f'المخزون غير كافٍ للمنتج "{product.name}" في مستودع "{warehouse.name}". '
            f'المتاح: {total_available}, المطلوب: {quantity_needed}'
        )

    consumptions = []
    remaining_to_consume = quantity_needed

    for batch in batches:
        if remaining_to_consume <= Decimal('0'):
            break

        # How much to take from this batch
        consume_from_this = min(batch.quantity_remaining, remaining_to_consume)

        # Update batch
        batch.quantity_remaining -= consume_from_this
        batch.save(update_fields=['quantity_remaining', 'updated_at'])

        total_cost = (consume_from_this * batch.unit_cost).quantize(Decimal('0.0001'))

        # Record the stock movement
        StockMovement.objects.create(
            product=product,
            warehouse=warehouse,
            batch=batch,
            movement_type='OUT',
            quantity=consume_from_this,
            unit_cost=batch.unit_cost,
            reference=reference,
            notes=notes or f'استهلاك FIFO من الدفعة #{batch.id}'
        )

        consumptions.append({
            'batch': batch,
            'quantity_consumed': consume_from_this,
            'unit_cost': batch.unit_cost,
            'total_cost': total_cost,
        })

        remaining_to_consume -= consume_from_this
        logger.debug(
            f'FIFO consume: product={product.id}, batch={batch.id}, '
            f'qty={consume_from_this}, cost={batch.unit_cost}'
        )

    return consumptions


@transaction.atomic
def add_inventory_batch(
    product,
    warehouse,
    quantity: Decimal,
    unit_cost: Decimal,
    expiry_date=None,
    batch_number: str = '',
    invoice_line=None,
    reference: str = '',
) -> 'InventoryBatch':
    """
    Creates a new FIFO inventory batch (on purchase/stock-in).

    Args:
        product: Product instance
        warehouse: Warehouse instance
        quantity: Decimal quantity received
        unit_cost: Decimal cost per unit
        expiry_date: Optional date for perishable items
        batch_number: Optional supplier batch/lot number
        invoice_line: Optional InvoiceLine that created this batch
        reference: Optional reference string

    Returns:
        The newly created InventoryBatch instance.

    Raises:
        ValueError: If quantity or cost is invalid
    """
    from apps.tenant.inventory.models import InventoryBatch, StockMovement

    if quantity <= Decimal('0'):
        raise ValueError(f'الكمية يجب أن تكون أكبر من صفر. المُدخل: {quantity}')
    if unit_cost < Decimal('0'):
        raise ValueError(f'التكلفة لا يمكن أن تكون سالبة. المُدخل: {unit_cost}')

    batch = InventoryBatch.objects.create(
        product=product,
        warehouse=warehouse,
        quantity_original=quantity,
        quantity_remaining=quantity,
        unit_cost=unit_cost,
        expiry_date=expiry_date,
        batch_number=batch_number,
        source_invoice_line=invoice_line,
    )

    # Update product's last purchase price
    product.last_purchase_price = unit_cost
    product.save(update_fields=['last_purchase_price', 'updated_at'])

    # Record the stock movement
    StockMovement.objects.create(
        product=product,
        warehouse=warehouse,
        batch=batch,
        movement_type='IN',
        quantity=quantity,
        unit_cost=unit_cost,
        reference=reference,
        notes=f'استلام مخزون - دفعة #{batch.id}'
    )

    logger.info(
        f'FIFO add_batch: product={product.id}, warehouse={warehouse.id}, '
        f'qty={quantity}, cost={unit_cost}, batch={batch.id}'
    )
    return batch


def get_stock_value(warehouse=None, product=None) -> Decimal:
    """
    Calculates total inventory value (quantity_remaining × unit_cost).

    Args:
        warehouse: Optional Warehouse to filter by
        product: Optional Product to filter by

    Returns:
        Decimal: Total value of matching inventory batches
    """
    from apps.tenant.inventory.models import InventoryBatch

    qs = InventoryBatch.objects.filter(quantity_remaining__gt=Decimal('0'))
    if warehouse:
        qs = qs.filter(warehouse=warehouse)
    if product:
        qs = qs.filter(product=product)

    total = Decimal('0')
    for batch in qs:
        total += batch.quantity_remaining * batch.unit_cost

    return total.quantize(Decimal('0.0001'))


@transaction.atomic
def transfer_stock(
    product,
    from_warehouse,
    to_warehouse,
    quantity: Decimal,
    reference: str = ''
) -> bool:
    """
    Transfers stock between two warehouses using FIFO for the source.

    The transfer:
      1. Consumes from from_warehouse (FIFO order, oldest first)
      2. Creates new batches in to_warehouse (preserving cost basis)
      3. Records TRANSFER_OUT and TRANSFER_IN stock movements

    Args:
        product: Product instance to transfer
        from_warehouse: Source Warehouse instance
        to_warehouse: Destination Warehouse instance
        quantity: Decimal quantity to transfer
        reference: Optional reference string

    Returns:
        True on success

    Raises:
        ValueError: If from_warehouse == to_warehouse or insufficient stock
    """
    from apps.tenant.inventory.models import InventoryBatch, StockMovement

    if from_warehouse.id == to_warehouse.id:
        raise ValueError('لا يمكن التحويل من مستودع إلى نفس المستودع.')

    if quantity <= Decimal('0'):
        raise ValueError('كمية التحويل يجب أن تكون أكبر من صفر.')

    # Check available stock
    available = product.get_stock(warehouse=from_warehouse)
    if available < quantity:
        raise ValueError(
            f'المخزون غير كافٍ للتحويل. المتاح في "{from_warehouse.name}": {available}, '
            f'المطلوب: {quantity}'
        )

    # Consume FIFO from source warehouse
    batches = InventoryBatch.objects.select_for_update().filter(
        product=product,
        warehouse=from_warehouse,
        quantity_remaining__gt=Decimal('0')
    ).order_by('created_at')

    remaining_to_transfer = quantity

    for batch in batches:
        if remaining_to_transfer <= Decimal('0'):
            break

        transfer_from_this = min(batch.quantity_remaining, remaining_to_transfer)

        # Deduct from source batch
        batch.quantity_remaining -= transfer_from_this
        batch.save(update_fields=['quantity_remaining', 'updated_at'])

        # Record TRANSFER_OUT
        StockMovement.objects.create(
            product=product,
            warehouse=from_warehouse,
            batch=batch,
            movement_type='TRANSFER_OUT',
            quantity=transfer_from_this,
            unit_cost=batch.unit_cost,
            reference=reference,
            notes=f'تحويل إلى {to_warehouse.name}'
        )

        # Create new batch in destination (preserving creation timestamp for FIFO)
        new_batch = InventoryBatch.objects.create(
            product=product,
            warehouse=to_warehouse,
            quantity_original=transfer_from_this,
            quantity_remaining=transfer_from_this,
            unit_cost=batch.unit_cost,
            expiry_date=batch.expiry_date,
            batch_number=batch.batch_number,
        )

        # Record TRANSFER_IN
        StockMovement.objects.create(
            product=product,
            warehouse=to_warehouse,
            batch=new_batch,
            movement_type='TRANSFER_IN',
            quantity=transfer_from_this,
            unit_cost=batch.unit_cost,
            reference=reference,
            notes=f'تحويل من {from_warehouse.name}'
        )

        remaining_to_transfer -= transfer_from_this

    logger.info(
        f'FIFO transfer: product={product.id}, '
        f'from={from_warehouse.id}, to={to_warehouse.id}, qty={quantity}'
    )
    return True
