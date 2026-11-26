"""
Service for managing, creating, and modifying product opening stock (رصيد أول المدة للأصناف).
Handles:
  - Reading the opening balance and history for a product in a warehouse.
  - Updating or setting opening quantity and unit cost.
  - Adjusting InventoryBatch (FIFO) and StockMovement records atomically.
  - Automatically creating or adjusting the balanced accounting JournalEntry (Debit 1140, Credit 3999).
  - Validation to prevent reducing opening stock below what has already been consumed/sold.
"""
from decimal import Decimal, InvalidOperation
from django.db import transaction, models
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
import datetime
import logging

from apps.tenant.inventory.models import Product, Warehouse, InventoryBatch, StockMovement
from apps.tenant.accounting.models import JournalEntry, JournalItem, Account
from apps.tenant.services.journal_service import _get_system_account

logger = logging.getLogger(__name__)


def get_product_opening_info(product: Product, warehouse: Warehouse = None):
    """
    Returns current opening stock information for a product in a given warehouse.
    """
    if warehouse is None:
        warehouse = Warehouse.objects.filter(is_active=True).first()

    default_cost = product.last_purchase_price or Decimal('0')
    if default_cost <= Decimal('0'):
        try:
            default_cost = product.get_average_cost(warehouse) or Decimal('0')
        except Exception:
            default_cost = Decimal('0')
    default_cost = default_cost.quantize(Decimal('0.01'))

    if not warehouse:
        return {
            'has_opening': False,
            'warehouse': None,
            'quantity': Decimal('0.00'),
            'remaining_quantity': Decimal('0.00'),
            'consumed_quantity': Decimal('0.00'),
            'unit_cost': default_cost,
            'default_cost': default_cost,
            'total_cost': Decimal('0.00'),
            'batch': None,
            'journal_entry': None,
            'journal_entry_ref': None,
        }

    # Search for an opening stock movement for this product and warehouse
    opening_movement = StockMovement.objects.filter(
        product=product,
        warehouse=warehouse,
        movement_type='OPENING_BALANCE'
    ).select_related('batch').order_by('created_at').first()

    batch = None
    if opening_movement and opening_movement.batch:
        batch = opening_movement.batch
    else:
        # Fallback: find batch created as opening stock (batch_number starting with 'رصيد' or reference containing 'افتتاحي')
        batch = InventoryBatch.objects.filter(
            product=product,
            warehouse=warehouse,
            source_invoice_line__isnull=True
        ).filter(
            # Only opening-balance batches: adjustment batches ('تسوية ...') are NOT opening stock
            models.Q(batch_number__startswith='رصيد') |
            models.Q(movements__movement_type='OPENING_BALANCE')
        ).distinct().order_by('created_at').first()

    if batch:
        original_qty = (batch.quantity_original or Decimal('0')).quantize(Decimal('0.01'))
        remaining_qty = (batch.quantity_remaining or Decimal('0')).quantize(Decimal('0.01'))
        consumed_qty = max(Decimal('0.00'), (original_qty - remaining_qty).quantize(Decimal('0.01')))
        unit_cost = batch.unit_cost or Decimal('0')
        if unit_cost <= Decimal('0'):
            unit_cost = default_cost
        unit_cost = unit_cost.quantize(Decimal('0.01'))
        total_cost = (original_qty * unit_cost).quantize(Decimal('0.01'))
        
        # Find linked journal entry if any
        prod_ct = ContentType.objects.get_for_model(Product)
        je = JournalEntry.objects.filter(
            content_type=prod_ct,
            object_id=product.id
        ).order_by('-date', '-id').first()
        if not je:
            je = JournalEntry.objects.filter(
                reference=f"OPN-PRD-{product.id}-{warehouse.id}"
            ).first()

        return {
            'has_opening': True,
            'warehouse': warehouse,
            'quantity': original_qty,
            'remaining_quantity': remaining_qty,
            'consumed_quantity': consumed_qty,
            'unit_cost': unit_cost,
            'default_cost': default_cost,
            'total_cost': total_cost,
            'batch': batch,
            'journal_entry': je,
            'journal_entry_ref': je.reference if je else (batch.batch_number or 'رصيد أول المدة'),
        }

    return {
        'has_opening': False,
        'warehouse': warehouse,
        'quantity': Decimal('0.00'),
        'remaining_quantity': Decimal('0.00'),
        'consumed_quantity': Decimal('0.00'),
        'unit_cost': default_cost,
        'default_cost': default_cost,
        'total_cost': Decimal('0.00'),
        'batch': None,
        'journal_entry': None,
        'journal_entry_ref': None,
    }


@transaction.atomic
def update_or_create_product_opening_stock(
    product: Product,
    warehouse: Warehouse,
    new_quantity: Decimal,
    unit_cost: Decimal,
    user=None,
    reason: str = ''
):
    """
    Creates or updates the opening balance for a product in a warehouse.
    Updates the FIFO InventoryBatch, logs a StockMovement, and creates/adjusts the JournalEntry (1140 vs 3999).
    """
    if getattr(product, 'product_type', None) == 'SERVICE':
        raise ValidationError('لا يمكن إدخال رصيد مخزني للمنتجات الخدمية.')

    try:
        new_quantity = Decimal(str(new_quantity)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError('كمية رصيد أول المدة غير صالحة.')

    try:
        unit_cost = Decimal(str(unit_cost)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError('سعر التكلفة غير صالح.')

    if new_quantity < Decimal('0'):
        raise ValidationError('كمية رصيد أول المدة لا يمكن أن تكون سالبة.')
    if unit_cost < Decimal('0'):
        raise ValidationError('تكلفة الوحدة لا يمكن أن تكون سالبة.')

    prod_locked = Product.objects.select_for_update(of=('self',)).get(id=product.id)
    wh_locked = Warehouse.objects.select_for_update(of=('self',)).get(id=warehouse.id)

    # Auto-fallback to product purchase price or average cost if unit_cost is zero
    if unit_cost <= Decimal('0'):
        fallback_cost = prod_locked.last_purchase_price or Decimal('0')
        if fallback_cost <= Decimal('0'):
            try:
                fallback_cost = prod_locked.get_average_cost(wh_locked) or Decimal('0')
            except Exception:
                fallback_cost = Decimal('0')
        if fallback_cost > Decimal('0'):
            unit_cost = fallback_cost.quantize(Decimal('0.01'))

    # 1. Look for existing opening batch
    opening_movement = StockMovement.objects.select_for_update(of=('self',)).filter(
        product=prod_locked,
        warehouse=wh_locked,
        movement_type='OPENING_BALANCE'
    ).select_related('batch').order_by('created_at').first()

    batch = None
    if opening_movement and opening_movement.batch:
        batch = InventoryBatch.objects.select_for_update(of=('self',)).filter(id=opening_movement.batch_id).first()

    if not batch:
        batch = InventoryBatch.objects.select_for_update(of=('self',)).filter(
            product=prod_locked,
            warehouse=wh_locked,
            source_invoice_line__isnull=True
        ).filter(
            # Only opening-balance batches: adjustment batches ('تسوية ...') are NOT opening stock
            models.Q(batch_number__startswith='رصيد') |
            models.Q(movements__movement_type='OPENING_BALANCE')
        ).distinct().order_by('created_at').first()

    today = timezone.now().date()
    inv_acc = _get_system_account('1140')
    opening_equity_acc = _get_system_account('3999')
    prod_ct = ContentType.objects.get_for_model(Product)

    # =========================================================================
    # CASE A: Modifying an existing opening batch
    # =========================================================================
    if batch:
        old_qty = batch.quantity_original
        old_cost = batch.unit_cost
        consumed_qty = max(Decimal('0'), old_qty - batch.quantity_remaining)

        # Safety Check: Cannot reduce below what was already consumed!
        if new_quantity < consumed_qty:
            raise ValidationError(
                f'لا يمكن تقليل رصيد أول المدة إلى {new_quantity}، لأن هناك {consumed_qty} وحدة '
                f'تم بيعها/صرفها بالفعل من هذا الرصيد الافتتاحي. الحد الأدنى المسموح به هو {consumed_qty}.'
            )

        delta_qty = (new_quantity - old_qty).quantize(Decimal('0.01'))
        old_remaining = batch.quantity_remaining
        new_remaining = old_remaining + delta_qty
        # Units already sold/consumed were costed (COGS) at the old cost: they keep it.
        # A cost change only revalues the quantity still on hand.
        consumed_val = (consumed_qty * old_cost).quantize(Decimal('0.01'))
        old_total_val = (consumed_val + old_remaining * old_cost).quantize(Decimal('0.01'))
        new_total_val = (consumed_val + new_remaining * unit_cost).quantize(Decimal('0.01'))
        delta_val = (new_total_val - old_total_val).quantize(Decimal('0.01'))

        # Update batch
        batch.quantity_original = new_quantity
        batch.quantity_remaining = new_remaining
        batch.unit_cost = unit_cost
        batch.save(update_fields=['quantity_original', 'quantity_remaining', 'unit_cost', 'updated_at'])

        # Update product last_purchase_price if cost provided
        if unit_cost > Decimal('0'):
            prod_locked.last_purchase_price = unit_cost
            prod_locked.save(update_fields=['last_purchase_price', 'updated_at'])

        # Audit movement
        audit_note = reason or f'تعديل رصيد أول المدة من {old_qty} إلى {new_quantity}'
        StockMovement.objects.create(
            product=prod_locked,
            warehouse=wh_locked,
            batch=batch,
            movement_type='OPENING_BALANCE',
            quantity=abs(delta_qty) if delta_qty != Decimal('0') else new_quantity,
            unit_cost=unit_cost,
            reference=f'تعديل رصيد - {prod_locked.id}',
            notes=f'{audit_note} (الفارق: {delta_qty:+})'
        )

        # Update or create Journal Entry
        dedicated_entry = JournalEntry.objects.filter(
            content_type=prod_ct,
            object_id=prod_locked.id,
            reference=f"OPN-PRD-{prod_locked.id}-{wh_locked.id}"
        ).order_by('-id').first()

        je = None
        if dedicated_entry and dedicated_entry.status == JournalEntry.DRAFT:
            # Update the dedicated entry while it is still a draft (a posted one is corrected by the
            # delta adjustment entry below — a posted entry is never rewritten)
            dedicated_entry.date = today
            dedicated_entry.description = f'رصيد أول المدة لصنف {prod_locked.name} ({new_quantity} وحدة) - {wh_locked.name}'
            dedicated_entry.save(update_fields=['date', 'description', 'updated_at'])
            dedicated_entry.items.all().delete()
            if new_total_val > Decimal('0'):
                JournalItem.objects.create(
                    entry=dedicated_entry, account=inv_acc, debit=new_total_val, credit=Decimal('0'),
                    description=f'رصيد أول المدة بضاعة - {prod_locked.name}'
                )
                JournalItem.objects.create(
                    entry=dedicated_entry, account=opening_equity_acc, debit=Decimal('0'), credit=new_total_val,
                    description=f'تسوية رصيد أول المدة رأس المال - {prod_locked.name}'
                )
            if new_total_val > Decimal('0'):
                dedicated_entry.post(user)        # lines first, then the checked posting
            je = dedicated_entry
        else:
            # Post an adjustment journal entry for the delta
            if delta_val != Decimal('0'):
                ref = f'OPN-ADJ-{prod_locked.id}-{wh_locked.id}-{int(timezone.now().timestamp())}'
                abs_delta = abs(delta_val)
                je = JournalEntry.objects.create(
                    date=today,
                    reference=ref,
                    description=f'تعديل قيد رصيد أول مدة لصنف {prod_locked.name} في {wh_locked.name}: تعديل من {old_qty} إلى {new_quantity}',
                    status=JournalEntry.DRAFT,
                    created_by=user,
                    content_type=prod_ct,
                    object_id=prod_locked.id
                )
                if delta_val > Decimal('0'):
                    JournalItem.objects.create(
                        entry=je, account=inv_acc, debit=abs_delta, credit=Decimal('0'),
                        description=f'فروق زيادة رصيد أول مدة - {prod_locked.name} (+{delta_qty} وحدة)'
                    )
                    JournalItem.objects.create(
                        entry=je, account=opening_equity_acc, debit=Decimal('0'), credit=abs_delta,
                        description=f'تسوية رأس مال رصيد أول مدة - {prod_locked.name}'
                    )
                else:
                    JournalItem.objects.create(
                        entry=je, account=opening_equity_acc, debit=abs_delta, credit=Decimal('0'),
                        description=f'تسوية رأس مال تخفيض رصيد أول مدة - {prod_locked.name}'
                    )
                    JournalItem.objects.create(
                        entry=je, account=inv_acc, debit=Decimal('0'), credit=abs_delta,
                        description=f'فروق تخفيض رصيد أول مدة - {prod_locked.name} ({delta_qty} وحدة)'
                    )
                je.post()

        return {
            'action': 'updated',
            'product': prod_locked,
            'warehouse': wh_locked,
            'old_quantity': old_qty,
            'new_quantity': new_quantity,
            'delta_quantity': delta_qty,
            'unit_cost': unit_cost,
            'delta_value': delta_val,
            'batch': batch,
            'journal_entry': je,
        }

    # =========================================================================
    # CASE B: Creating a brand new opening balance
    # =========================================================================
    else:
        if new_quantity == Decimal('0'):
            return {
                'action': 'none',
                'product': prod_locked,
                'warehouse': wh_locked,
                'new_quantity': Decimal('0'),
                'delta_quantity': Decimal('0'),
                'unit_cost': unit_cost,
                'delta_value': Decimal('0'),
                'batch': None,
                'journal_entry': None
            }

        total_val = (new_quantity * unit_cost).quantize(Decimal('0.01'))

        # Create batch through the FIFO engine so existing deficit (negative) batches
        # in this warehouse are offset first; records the OPENING_BALANCE movement.
        from apps.tenant.services.fifo_engine import add_inventory_batch
        batch = add_inventory_batch(
            product=prod_locked,
            warehouse=wh_locked,
            quantity=new_quantity,
            unit_cost=unit_cost,
            batch_number=f"رصيد-{today.strftime('%Y%m%d')}",
            reference=f'رصيد افتتاحي - {prod_locked.id}',
            movement_type='OPENING_BALANCE',
            update_last_purchase_price=unit_cost > Decimal('0'),
            notes=reason or f'إدخال رصيد أول المدة لصنف {prod_locked.name}',
        )

        # Create Journal Entry
        je = None
        if total_val > Decimal('0'):
            ref = f"OPN-PRD-{prod_locked.id}-{wh_locked.id}"
            je = JournalEntry.objects.create(
                date=today,
                reference=ref,
                description=f'رصيد أول المدة لصنف {prod_locked.name} ({new_quantity} وحدة) - {wh_locked.name}',
                status=JournalEntry.DRAFT,
                created_by=user,
                content_type=prod_ct,
                object_id=prod_locked.id
            )
            JournalItem.objects.create(
                entry=je, account=inv_acc, debit=total_val, credit=Decimal('0'),
                description=f'رصيد أول المدة بضاعة - {prod_locked.name} ({wh_locked.name})'
            )
            JournalItem.objects.create(
                entry=je, account=opening_equity_acc, debit=Decimal('0'), credit=total_val,
                description=f'مقابل رصيد أول المدة بضاعة رأس المال - {prod_locked.name}'
            )
            je.post()

        return {
            'action': 'created',
            'product': prod_locked,
            'warehouse': wh_locked,
            'old_quantity': Decimal('0'),
            'new_quantity': new_quantity,
            'delta_quantity': new_quantity,
            'unit_cost': unit_cost,
            'delta_value': total_val,
            'batch': batch,
            'journal_entry': je,
        }
