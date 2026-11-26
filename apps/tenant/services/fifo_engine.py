"""
FIFO Inventory Engine
=====================
Implements First-In-First-Out (FIFO) inventory consumption and stock management.

Key functions:
  - consume_fifo_batches(): Deducts stock from oldest batches first
  - add_inventory_batch(): Creates a new FIFO batch on purchase / stock-in
  - get_stock_value(): Calculates total inventory value
  - transfer_stock(): Moves stock between warehouses via FIFO

Concurrency: batch rows are locked with SELECT ... FOR UPDATE *before* the
available quantity is computed (the sum is done in Python over the locked rows),
so two concurrent consumers can never both see the same stock.
Every stock change creates an immutable StockMovement audit record.
"""
from django.db import transaction
from decimal import Decimal
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


class InsufficientStockError(ValueError):
    """Not enough (sellable) stock for a consumption or a transfer."""


def global_negative_stock_allowed() -> bool:
    """The SystemSetting flag — read it ONCE per document and pass it down (allow_negative=…)."""
    from apps.tenant.core.models import SystemSetting
    return bool(getattr(SystemSetting.get_settings(), 'allow_negative_stock', False))


def _negative_stock_allowed(product, allow_global=None) -> bool:
    """Global SystemSetting OR the product's own allow_negative_stock flag."""
    if allow_global is None:
        allow_global = global_negative_stock_allowed()
    return bool(allow_global or getattr(product, 'allow_negative_stock', False))


def lock_order(items, key=lambda x: x.product_id):
    """Documents touching several products lock their batches in product-id order: two documents
    posted at the same moment then wait for each other instead of dead-locking (PostgreSQL)."""
    return sorted(items, key=lambda x: (key(x) or 0))


def _offset_deficits(product, warehouse, batch):
    """
    Uses the quantity of a freshly created positive `batch` to fill existing negative
    (deficit) batches of the same product & warehouse, oldest first.
    Returns the batch with its quantity_remaining reduced accordingly.
    """
    from apps.tenant.inventory.models import InventoryBatch

    negative_batches = list(
        InventoryBatch.objects.select_for_update(of=('self',)).filter(
            product=product,
            warehouse=warehouse,
            quantity_remaining__lt=Decimal('0')
        ).exclude(id=batch.id).order_by('created_at', 'id')
    )

    remaining_to_offset = batch.quantity_remaining
    for neg_batch in negative_batches:
        if remaining_to_offset <= Decimal('0'):
            break
        deficit = abs(neg_batch.quantity_remaining)
        offset = min(deficit, remaining_to_offset)
        neg_batch.quantity_remaining += offset
        neg_batch.save(update_fields=['quantity_remaining', 'updated_at'])
        remaining_to_offset -= offset

    if remaining_to_offset != batch.quantity_remaining:
        batch.quantity_remaining = remaining_to_offset
        batch.save(update_fields=['quantity_remaining', 'updated_at'])
    return batch


def _fefo():
    """Consumption order: nearest expiry first (FEFO), no expiry last; same expiry → oldest first (FIFO)."""
    from django.db.models import F
    return (F('expiry_date').asc(nulls_last=True), 'created_at', 'id')


@transaction.atomic
def consume_fifo_batches(
    product,
    warehouse,
    quantity_needed: Decimal,
    reference: str = '',
    notes: str = '',
    movement_type: str = 'OUT',
    allow_negative: Optional[bool] = None,
    skip_expired: bool = False,
) -> List[dict]:
    """
    Consume inventory from the oldest available batches (FIFO order).

    Batches are locked (select_for_update) before the availability check so that
    concurrent consumers are serialized. Creates a StockMovement per batch consumed.

    Args:
        product: Product instance to consume
        warehouse: Warehouse instance to consume from
        quantity_needed: Decimal quantity to consume (base units)
        reference: Optional reference string (e.g., invoice number)
        notes: Optional notes for audit trail
        movement_type: StockMovement type to record
        allow_negative: Optional override. None (default) = global setting OR
            product.allow_negative_stock.

    Returns:
        List of dicts, one per batch consumed:
            [{'batch', 'quantity_consumed', 'unit_cost', 'total_cost'}, ...]

    Raises:
        ValueError: If insufficient stock is available and negative stock is not allowed
    """
    from apps.tenant.inventory.models import InventoryBatch, StockMovement

    if getattr(product, 'product_type', None) == 'SERVICE':
        logger.info(f'Skipping FIFO consumption for SERVICE product: {product.name}')
        return []

    quantity_needed = Decimal(str(quantity_needed))
    if quantity_needed == Decimal('0'):
        return []
    if quantity_needed < Decimal('0'):
        raise ValueError(f'الكمية المطلوب صرفها لا يمكن أن تكون سالبة. المُدخل: {quantity_needed}')

    # Lock the batches FIRST (FEFO order), then compute availability from the locked rows.
    batches = list(
        InventoryBatch.objects.select_for_update(of=('self',)).filter(
            product=product,
            warehouse=warehouse,
            quantity_remaining__gt=Decimal('0')
        ).order_by(*_fefo())
    )
    expired_qty = Decimal('0')
    if skip_expired:
        import datetime
        today = datetime.date.today()
        expired_qty = sum((b.quantity_remaining for b in batches if b.expiry_date and b.expiry_date < today),
                          Decimal('0'))
        batches = [b for b in batches if not (b.expiry_date and b.expiry_date < today)]
    total_available = sum((b.quantity_remaining for b in batches), Decimal('0'))

    if allow_negative is None:
        allow_negative = _negative_stock_allowed(product)

    if total_available < quantity_needed and (not allow_negative or expired_qty > 0):
        extra = (f' — ومنه {expired_qty.normalize():f} منتهي الصلاحية مينفعش يتباع '
                 '(اعمله تسوية هالك، أو محتاج صلاحية «بيع دفعات منتهية»)') if expired_qty else ''
        raise InsufficientStockError(
            f'المخزون غير كافٍ للمنتج "{product.name}" في مستودع "{warehouse.name}". '
            f'المتاح: {total_available}, المطلوب: {quantity_needed}{extra}'
        )

    consumptions = []
    remaining_to_consume = quantity_needed

    for batch in batches:
        if remaining_to_consume <= Decimal('0'):
            break

        consume_from_this = min(batch.quantity_remaining, remaining_to_consume)

        batch.quantity_remaining -= consume_from_this
        batch.save(update_fields=['quantity_remaining', 'updated_at'])

        total_cost = (consume_from_this * batch.unit_cost).quantize(Decimal('0.0001'))

        StockMovement.objects.create(
            product=product,
            warehouse=warehouse,
            batch=batch,
            movement_type=movement_type,
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

    if remaining_to_consume > Decimal('0'):
        estimated_unit_cost = (
            product.get_average_cost(warehouse=warehouse)
            or product.last_purchase_price
            or Decimal('0')
        )

        deficit_batch = InventoryBatch.objects.create(
            product=product,
            warehouse=warehouse,
            quantity_original=Decimal('0'),
            quantity_remaining=-remaining_to_consume,
            unit_cost=estimated_unit_cost,
            batch_number='DEFICIT',
        )

        StockMovement.objects.create(
            product=product,
            warehouse=warehouse,
            batch=deficit_batch,
            movement_type=movement_type,
            quantity=remaining_to_consume,
            unit_cost=estimated_unit_cost,
            reference=reference,
            notes=notes or f'استهلاك رصيد سالب - عجز {remaining_to_consume}'
        )

        total_cost = (remaining_to_consume * estimated_unit_cost).quantize(Decimal('0.0001'))
        consumptions.append({
            'batch': deficit_batch,
            'quantity_consumed': remaining_to_consume,
            'unit_cost': estimated_unit_cost,
            'total_cost': total_cost,
        })

        logger.info(
            f'FIFO consume (negative stock): product={product.id}, '
            f'qty={remaining_to_consume}, estimated_cost={estimated_unit_cost}'
        )
        remaining_to_consume = Decimal('0')

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
    movement_type: str = 'IN',
    update_last_purchase_price: Optional[bool] = None,
    notes: str = '',
) -> 'InventoryBatch':
    """
    Creates a new FIFO inventory batch (on purchase/stock-in) and offsets any
    existing negative (deficit) batches of the same product & warehouse.

    Args:
        product: Product instance
        warehouse: Warehouse instance
        quantity: Decimal quantity received (base units)
        unit_cost: Decimal cost per unit
        expiry_date: Optional date for perishable items
        batch_number: Optional supplier batch/lot number
        invoice_line: Optional InvoiceLine that created this batch
        reference: Optional reference string
        movement_type: StockMovement type to record
        update_last_purchase_price: Update product.last_purchase_price with unit_cost.
            None (default) = only for real purchase receipts (movement_type == 'IN').
            Pass False for restorations / cancellations / returns / adjustments.
        notes: Optional StockMovement notes

    Returns:
        The newly created InventoryBatch instance (None for SERVICE products).

    Raises:
        ValueError: If quantity or cost is invalid
    """
    from apps.tenant.inventory.models import InventoryBatch, StockMovement

    if getattr(product, 'product_type', None) == 'SERVICE':
        logger.info(f'Skipping FIFO batch creation for SERVICE product: {product.name}')
        return None

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

    _offset_deficits(product, warehouse, batch)

    if update_last_purchase_price is None:
        update_last_purchase_price = (movement_type == 'IN')
    if update_last_purchase_price:
        product.last_purchase_price = unit_cost
        product.save(update_fields=['last_purchase_price', 'updated_at'])

    StockMovement.objects.create(
        product=product,
        warehouse=warehouse,
        batch=batch,
        movement_type=movement_type,
        quantity=quantity,
        unit_cost=unit_cost,
        reference=reference,
        notes=notes or f'استلام مخزون - دفعة #{batch.id}'
    )

    logger.info(
        f'FIFO add_batch: product={product.id}, warehouse={warehouse.id}, '
        f'qty={quantity}, cost={unit_cost}, batch={batch.id}'
    )
    return batch


def get_stock_value(warehouse=None, product=None) -> Decimal:
    """
    Calculates total inventory value (quantity_remaining × unit_cost).
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


def _transfer_serialized(product, from_warehouse, to_warehouse, quantity, reference, serial_ids=None):
    """Moves `quantity` unsold SerialItems (or the explicit serial_ids) between warehouses."""
    from apps.tenant.inventory.models import SerialItem, StockMovement

    if quantity != quantity.to_integral_value():
        raise ValueError('كمية التحويل للمنتجات المسلسلة يجب أن تكون عدداً صحيحاً.')
    count = int(quantity)

    qs = SerialItem.objects.select_for_update(of=('self',)).filter(
        product=product, warehouse=from_warehouse, is_sold=False
    )
    if serial_ids:
        serials = list(qs.filter(id__in=list(serial_ids)).order_by('created_at', 'id'))
        if len(serials) != len(set(serial_ids)) or len(serials) != count:
            raise ValueError('بعض السيريالات المحددة غير متاحة في المستودع المصدر أو لا تطابق الكمية.')
    else:
        serials = list(qs.order_by('created_at', 'id')[:count])
        if len(serials) < count:
            raise ValueError(
                f'المخزون غير كافٍ للتحويل. المتاح في "{from_warehouse.name}": {len(serials)}, '
                f'المطلوب: {count}'
            )

    for s in serials:
        s.warehouse = to_warehouse
        s.save(update_fields=['warehouse', 'updated_at'])
        for wh, m_type, note in (
            (from_warehouse, 'TRANSFER_OUT', f'تحويل إلى {to_warehouse.name}'),
            (to_warehouse, 'TRANSFER_IN', f'تحويل من {from_warehouse.name}'),
        ):
            StockMovement.objects.create(
                product=product,
                warehouse=wh,
                batch=None,
                movement_type=m_type,
                quantity=Decimal('1'),
                unit_cost=s.actual_cost or Decimal('0'),
                reference=reference,
                notes=f'{note} - سيريال #{s.id}',
            )
    return True


@transaction.atomic
def transfer_stock(
    product,
    from_warehouse,
    to_warehouse,
    quantity: Decimal,
    reference: str = '',
    serial_ids=None,
    skip_expired: bool = False,
) -> bool:
    """
    Transfers stock between two warehouses using FIFO for the source.

    The transfer is all-or-nothing: if the source does not hold the full quantity
    (positive batches only, negative stock is never allowed for transfers), nothing
    is moved and ValueError is raised.

      1. Locks and consumes source batches (FIFO order, oldest first)
      2. Creates batches in to_warehouse preserving cost basis; these first offset
         any deficit (negative) batches in the destination
      3. Records TRANSFER_OUT and TRANSFER_IN stock movements

    SERIALIZED products move their unsold SerialItems (optionally the given serial_ids).

    Returns:
        True on success

    Raises:
        ValueError: If from_warehouse == to_warehouse or insufficient stock
    """
    from apps.tenant.inventory.models import InventoryBatch, StockMovement

    if from_warehouse.id == to_warehouse.id:
        raise ValueError('لا يمكن التحويل من مستودع إلى نفس المستودع.')

    quantity = Decimal(str(quantity))
    if quantity <= Decimal('0'):
        raise ValueError('كمية التحويل يجب أن تكون أكبر من صفر.')

    product_type = getattr(product, 'product_type', None)
    if product_type == 'SERVICE':
        raise ValueError('لا يمكن تحويل منتج خدمي (لا يملك مخزوناً).')
    if product_type == 'SERIALIZED':
        return _transfer_serialized(product, from_warehouse, to_warehouse, quantity, reference, serial_ids)

    # Lock source batches first, then check availability on the locked rows.
    batches = list(
        InventoryBatch.objects.select_for_update(of=('self',)).filter(
            product=product,
            warehouse=from_warehouse,
            quantity_remaining__gt=Decimal('0')
        ).order_by(*_fefo())
    )
    if skip_expired:            # e.g. loading a sales rep's van: expired goods stay in the warehouse
        import datetime
        today = datetime.date.today()
        batches = [b for b in batches if not (b.expiry_date and b.expiry_date < today)]
    available = sum((b.quantity_remaining for b in batches), Decimal('0'))
    if available < quantity:
        raise InsufficientStockError(
            f'المخزون غير كافٍ للتحويل. المتاح في "{from_warehouse.name}": {available}, '
            f'المطلوب: {quantity}'
        )

    remaining_to_transfer = quantity

    for batch in batches:
        if remaining_to_transfer <= Decimal('0'):
            break

        transfer_from_this = min(batch.quantity_remaining, remaining_to_transfer)

        batch.quantity_remaining -= transfer_from_this
        batch.save(update_fields=['quantity_remaining', 'updated_at'])

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

        new_batch = InventoryBatch.objects.create(
            product=product,
            warehouse=to_warehouse,
            quantity_original=transfer_from_this,
            quantity_remaining=transfer_from_this,
            unit_cost=batch.unit_cost,
            expiry_date=batch.expiry_date,
            batch_number=batch.batch_number,
        )
        # Fill destination deficits (negative stock) first
        _offset_deficits(product, to_warehouse, new_batch)

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

    if remaining_to_transfer > Decimal('0'):
        # Should be unreachable after the locked availability check; fail atomically.
        raise ValueError(
            f'تعذر إكمال التحويل: تبقى {remaining_to_transfer} غير محوّلة. تم إلغاء العملية بالكامل.'
        )

    logger.info(
        f'FIFO transfer: product={product.id}, '
        f'from={from_warehouse.id}, to={to_warehouse.id}, qty={quantity}'
    )
    return True
