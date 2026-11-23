"""
Invoice Service
===============
Master coordinator for invoice confirmation.

confirm_invoice() is the single entry point that:
  1. Validates the invoice is in DRAFT status
  2. For SALE: runs FIFO consumption and calculates COGS per line
  3. For PURCHASE: creates FIFO inventory batches for each line
  4. Posts the corresponding accounting journal entry
  5. Updates the invoice to POSTED status

Everything runs inside a single @transaction.atomic block.
If any step fails, ALL changes are rolled back.
"""
from django.db import transaction
from django.core.exceptions import ValidationError
from decimal import Decimal
from typing import Optional, List, Dict, Any
import logging


logger = logging.getLogger(__name__)


@transaction.atomic
def confirm_invoice(invoice_id: int):
    """
    Confirms/posts a DRAFT invoice, triggering all financial and inventory effects.

    Processing logic by invoice type:

    SALE:
      - Validates all products have sufficient stock in the invoice's warehouse
      - Runs FIFO engine to consume stock (oldest batches first)
      - Calculates actual COGS per line from FIFO consumption data
      - Creates journal entry: Dr Cash/A/R  Cr Revenue + VAT + COGS entry
      - Updates invoice.status = POSTED

    PURCHASE:
      - For each line, creates a new InventoryBatch (FIFO in)
      - Creates journal entry: Dr Inventory  Cr Cash/A/P
      - Updates invoice.status = POSTED

    RETURN_SALE:
      - Re-adds consumed stock back to inventory
      - Reverses the original journal entry

    RETURN_PURCHASE:
      - Removes stock from inventory
      - Reverses the original purchase journal entry

    Args:
        invoice_id: Primary key of the Invoice to confirm

    Returns:
        The confirmed Invoice instance (status=POSTED)

    Raises:
        ValueError: If the invoice is not in DRAFT status
        ValueError: If stock is insufficient (for SALE)
        ValidationError: If required accounts are missing
    """
    from apps.tenant.invoicing.models import Invoice, InvoiceLine
    from apps.tenant.services.fifo_engine import consume_fifo_batches, add_inventory_batch
    from apps.tenant.services.journal_service import post_sale_invoice, post_purchase_invoice

    # --- Fetch and lock the invoice ---
    try:
        invoice = Invoice.objects.select_for_update().get(id=invoice_id)
    except Invoice.DoesNotExist:
        raise ValueError(f'الفاتورة رقم {invoice_id} غير موجودة.')

    # --- Validate status ---
    if invoice.status != Invoice.DRAFT:
        raise ValueError(
            f'لا يمكن ترحيل الفاتورة "{invoice.invoice_number}" '
            f'لأنها في حالة "{invoice.get_status_display()}" وليست مسودة.'
        )

    # --- Validate lines exist ---
    lines = list(invoice.lines.select_related('product', 'uom', 'original_line', 'original_line__uom').all())
    if not lines:
        raise ValueError(
            f'الفاتورة "{invoice.invoice_number}" لا تحتوي على بنود.'
        )

    # -------------------------------------------------------------------------
    # SALE and RETURN_PURCHASE invoice processing (Stock OUT)
    # -------------------------------------------------------------------------
    if invoice.invoice_type in (Invoice.SALE, Invoice.RETURN_PURCHASE):
        _process_sale_invoice(invoice, lines)

    # -------------------------------------------------------------------------
    # PURCHASE and RETURN_SALE invoice processing (Stock IN)
    # -------------------------------------------------------------------------
    elif invoice.invoice_type in (Invoice.PURCHASE, Invoice.RETURN_SALE):
        _process_purchase_invoice(invoice, lines)

    else:
        raise ValueError(f'نوع فاتورة غير معروف: {invoice.invoice_type}')

    logger.info(
        f'Invoice confirmed: {invoice.invoice_number} '
        f'(type={invoice.invoice_type}, total={invoice.total_amount})'
    )
    return invoice


def _process_sale_invoice(invoice, lines):
    """
    Internal handler for SALE invoice confirmation.
    """
    from apps.tenant.services.fifo_engine import consume_fifo_batches
    from apps.tenant.services.journal_service import post_sale_invoice
    from apps.tenant.invoicing.models import Invoice

    from apps.tenant.inventory.models import SerialItem

    total_cogs = Decimal('0')
    used_serials = set()

    from apps.tenant.core.models import SystemSetting
    system_setting = SystemSetting.get_settings()
    allow_negative_global = getattr(system_setting, 'allow_negative_stock', False)

    # --- Pre-validate all stock before consuming anything ---
    for line in lines:
        if line.product.product_type == 'SERVICE':
            continue  # Services have no stock limits
        base_quantity = line.quantity * (line.uom.conversion_factor if getattr(line, 'uom', None) else 1)
        available = line.product.get_stock(warehouse=invoice.warehouse)
        # Master Lock: Global setting or individual product setting governs negative stock
        allow_negative = allow_negative_global or getattr(line.product, 'allow_negative_stock', False)
        if available < base_quantity and not allow_negative:
            raise ValueError(
                f'المخزون غير كافٍ للمنتج "{line.product.name}". '
                f'المتاح: {available}, المطلوب: {base_quantity}'
            )

    # --- Consume stock FIFO and calculate COGS ---
    for line in lines:
        if line.product.product_type == 'SERVICE':
            line.cogs_amount = Decimal('0')
            line.save(update_fields=['cogs_amount'])
            continue

        if line.product.product_type == 'SERIALIZED':
            if not line.serial_item_id:
                raise ValueError(f'يجب تحديد السيريال للمنتج "{line.product.name}"')
            # Lock and validate: same product, still in stock, in this invoice's warehouse
            serial = SerialItem.objects.select_for_update().filter(id=line.serial_item_id).first()
            if (
                serial is None
                or serial.product_id != line.product_id
                or serial.is_sold
                or serial.warehouse_id != invoice.warehouse_id
            ):
                raise ValueError(
                    f'السيريال المحدد للمنتج "{line.product.name}" غير متاح في المستودع '
                    f'"{invoice.warehouse.name}" (مباع مسبقاً أو يخص منتجاً/مستودعاً آخر).'
                )
            if line.quantity != 1:
                raise ValueError(f'المنتج "{line.product.name}" مسلسل؛ كمية السطر يجب أن تكون 1.')
            if serial.id in used_serials:
                raise ValueError(f'السيريال نفسه مكرر في الفاتورة للمنتج "{line.product.name}".')
            used_serials.add(serial.id)

            if invoice.invoice_type == Invoice.RETURN_PURCHASE:
                serial.is_returned = True
                serial.warehouse = None
            else:
                serial.is_sold = True
                serial.warehouse = None

            serial.save()
            line.serial_item = serial
            line.cogs_amount = serial.actual_cost
            line.save(update_fields=['cogs_amount'])
            total_cogs += line.cogs_amount
            continue

        base_quantity = line.quantity * (line.uom.conversion_factor if getattr(line, 'uom', None) else 1)
        is_return_purchase = (invoice.invoice_type == Invoice.RETURN_PURCHASE)
        notes_text = f'مرتجع مشتريات - فاتورة {invoice.invoice_number}' if is_return_purchase else f'بيع - فاتورة {invoice.invoice_number}'
        m_type = 'RETURN_OUT' if is_return_purchase else 'OUT'
        consumptions = []
        if is_return_purchase and getattr(line, 'original_line_id', None):
            # Return the goods from the batch(es) received by the original purchase line first
            consumptions, base_quantity = _consume_from_source_batches(
                line.product, invoice.warehouse, line.original_line, base_quantity,
                reference=invoice.invoice_number, notes=notes_text, movement_type=m_type,
            )
        if base_quantity > Decimal('0'):
            consumptions += consume_fifo_batches(
                product=line.product,
                warehouse=invoice.warehouse,
                quantity_needed=base_quantity,
                reference=invoice.invoice_number,
                notes=notes_text,
                movement_type=m_type,
            )

        # Sum COGS from all consumed batches for this line
        line_cogs = sum(c['total_cost'] for c in consumptions)
        line.cogs_amount = Decimal(str(line_cogs)).quantize(Decimal('0.0001'))
        line.save(update_fields=['cogs_amount'])
        total_cogs += line.cogs_amount

    # --- Recalculate invoice totals (in case they drifted) ---
    invoice.recalculate_totals()

    # --- Create and post journal entry ---
    if invoice.invoice_type == Invoice.RETURN_PURCHASE:
        from apps.tenant.services.journal_service import post_purchase_invoice
        journal_entry = post_purchase_invoice(invoice)
    else:
        journal_entry = post_sale_invoice(invoice)

    # --- Update invoice status ---
    invoice.journal_entry = journal_entry
    invoice.status = Invoice.POSTED
    invoice.save(update_fields=['journal_entry', 'status', 'updated_at'])


def _consume_from_source_batches(product, warehouse, original_line, quantity, reference='', notes='', movement_type='RETURN_OUT'):
    """
    Consumes up to `quantity` from the batches created by `original_line` (the purchase
    line being returned). Returns (consumptions, quantity_still_needed).
    """
    from apps.tenant.inventory.models import InventoryBatch, StockMovement

    batches = list(
        InventoryBatch.objects.select_for_update().filter(
            source_invoice_line=original_line,
            product=product,
            warehouse=warehouse,
            quantity_remaining__gt=Decimal('0'),
        ).order_by('created_at', 'id')
    )
    consumptions = []
    remaining = quantity
    for b in batches:
        if remaining <= Decimal('0'):
            break
        take = min(b.quantity_remaining, remaining)
        b.quantity_remaining -= take
        b.save(update_fields=['quantity_remaining', 'updated_at'])
        StockMovement.objects.create(
            product=product,
            warehouse=warehouse,
            batch=b,
            movement_type=movement_type,
            quantity=take,
            unit_cost=b.unit_cost,
            reference=reference,
            notes=notes or f'مرتجع من الدفعة الأصلية #{b.id}',
        )
        consumptions.append({
            'batch': b,
            'quantity_consumed': take,
            'unit_cost': b.unit_cost,
            'total_cost': (take * b.unit_cost).quantize(Decimal('0.0001')),
        })
        remaining -= take
    return consumptions, remaining


def _process_purchase_invoice(invoice, lines):
    """
    Internal handler for PURCHASE invoice confirmation.
    """
    from apps.tenant.services.fifo_engine import add_inventory_batch
    from apps.tenant.services.journal_service import post_purchase_invoice
    from apps.tenant.invoicing.models import Invoice

    # --- Create inventory batches ---
    for line in lines:
        if line.product.product_type == 'SERVICE':
            continue  # No stock tracking for services

        if line.product.product_type == 'SERIALIZED':
            # For SERIALIZED, the SerialItem is created from the frontend with the serial number.
            # Here we just ensure the serial is assigned to the correct warehouse.
            if line.serial_item:
                line.serial_item.warehouse = invoice.warehouse
                line.serial_item.is_sold = False
                line.serial_item.save()
            continue

        conversion_factor = line.uom.conversion_factor if getattr(line, 'uom', None) else Decimal('1')
        base_quantity = line.quantity * conversion_factor
        is_return_sale = (invoice.invoice_type == Invoice.RETURN_SALE)
        m_type = 'RETURN_IN' if is_return_sale else 'IN'

        # For RETURN_SALE, use the exact original line's unit COGS if available from parent invoice, else fallback to average_cost / last_purchase_price
        if is_return_sale:
            unit_cost = line.product.get_average_cost(invoice.warehouse) or line.product.last_purchase_price
            if getattr(invoice, 'parent_invoice', None):
                orig_line = line.original_line if getattr(line, 'original_line_id', None) else None
                if orig_line is None:
                    orig_line = invoice.parent_invoice.lines.filter(product=line.product).first()
                if orig_line and orig_line.quantity > 0 and orig_line.cogs_amount > 0:
                    orig_conv = orig_line.uom.conversion_factor if getattr(orig_line, 'uom', None) else Decimal('1')
                    orig_base_qty = orig_line.quantity * orig_conv
                    unit_cost = (orig_line.cogs_amount / orig_base_qty).quantize(Decimal('0.0001'))
        else:
            unit_cost = (line.subtotal / base_quantity).quantize(Decimal('0.0001')) if base_quantity > 0 else (line.unit_price / conversion_factor)

        add_inventory_batch(
            product=line.product,
            warehouse=invoice.warehouse,
            quantity=base_quantity,
            unit_cost=unit_cost,
            expiry_date=None if is_return_sale else getattr(line, 'expiry_date', None),
            invoice_line=line,
            reference=invoice.invoice_number,
            movement_type=m_type,
            # Only a real purchase updates last_purchase_price (not a customer return)
            update_last_purchase_price=not is_return_sale,
        )

        if invoice.invoice_type == Invoice.RETURN_SALE:
            line.cogs_amount = (unit_cost * base_quantity).quantize(Decimal('0.0001'))
            line.save(update_fields=['cogs_amount'])

    # --- Recalculate invoice totals ---
    invoice.recalculate_totals()

    # --- Create and post journal entry ---
    if invoice.invoice_type == Invoice.RETURN_SALE:
        from apps.tenant.services.journal_service import post_sale_invoice
        journal_entry = post_sale_invoice(invoice)
    else:
        journal_entry = post_purchase_invoice(invoice)

    # --- Update invoice status ---
    invoice.journal_entry = journal_entry
    invoice.status = Invoice.POSTED
    invoice.save(update_fields=['journal_entry', 'status', 'updated_at'])


@transaction.atomic
def update_invoice_payment_method(
    invoice_id: int,
    payment_type: str,
    partner_id: Optional[int] = None,
    treasury_id: Optional[int] = None,
    bank_account_id: Optional[int] = None,
    ewallet_id: Optional[int] = None,
    paid_cash: Decimal = Decimal('0'),
    paid_card: Decimal = Decimal('0'),
    paid_ewallet: Decimal = Decimal('0'),
    paid_bank: Decimal = Decimal('0'),
    insta_bank_account_id: Optional[int] = None,
    user=None,
):
    """
    Updates payment_type and payment destination fields of an existing invoice.
    If the invoice is POSTED, its active journal entries (main accrual entry + any legacy
    settlement entries) are reversed - never deleted - and a fresh entry matching the
    updated payment method / partner is posted. invoice.journal_entry points to the new one.
    """
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.partners.models import Partner
    from apps.tenant.services.journal_service import reverse_document_entries

    invoice = Invoice.objects.select_for_update().get(id=invoice_id)

    # Protection Guard: Cannot modify payment method of an invoice that has returns
    if invoice.return_invoices.exclude(status=Invoice.CANCELLED).exists() or any(line.returned_quantity > 0 for line in invoice.lines.all()):
        raise ValueError(
            f'لا يمكن تعديل طريقة سداد الفاتورة رقم "{invoice.invoice_number}" لوجود مرتجعات محررة عليها.'
        )

    # Update partner if provided
    if partner_id:
        new_partner = Partner.objects.get(id=partner_id)
        invoice.partner = new_partner

    # Protection Guard: Credit Payment requires an explicit, non-cash partner
    if payment_type == 'CREDIT':
        curr_partner = invoice.partner
        if not curr_partner or 'عميل نقدي' in curr_partner.name or curr_partner.name.strip() in ['عميل نقدي', 'نقدي', 'طياري', 'مورد نقدي']:
            raise ValueError('لا يمكن تغيير طريقة السداد إلى (آجل) لـ "عميل نقدي". يرجى اختيار عميل/مورد آخر مخصص للآجل.')

    # 1. If invoice is POSTED, reverse its active entries (audit trail: posted lines are never
    #    edited or deleted). The new entry posted in step 3 uses the (possibly new) partner's
    #    receivable/payable account and the new payment destination.
    if invoice.status == Invoice.POSTED:
        reverse_document_entries(
            invoice,
            reason=f'تعديل طريقة سداد الفاتورة {invoice.invoice_number}',
            user=user,
            extra_entry_ids=[invoice.journal_entry_id],
        )
        invoice.journal_entry = None

    # 2. Update payment fields on invoice
    invoice.payment_type = payment_type
    invoice.treasury_id = treasury_id
    invoice.bank_account_id = bank_account_id
    invoice.insta_bank_account_id = insta_bank_account_id
    invoice.ewallet_id = ewallet_id
    invoice.paid_cash = paid_cash
    invoice.paid_card = paid_card
    invoice.paid_ewallet = paid_ewallet
    invoice.paid_bank = paid_bank
    # The amounts are re-entered manually here: the POS cash rounding no longer applies.
    invoice.rounding_amount = Decimal('0')

    invoice.save(update_fields=[
        'partner', 'payment_type', 'treasury_id', 'bank_account_id', 'insta_bank_account_id', 'ewallet_id',
        'paid_cash', 'paid_card', 'paid_ewallet', 'paid_bank', 'rounding_amount', 'updated_at'
    ])

    # 3. If POSTED, post a fresh unified journal entry matching the updated payment method
    if invoice.status == Invoice.POSTED:
        from apps.tenant.services.journal_service import post_sale_invoice, post_purchase_invoice
        if invoice.invoice_type in (Invoice.SALE, Invoice.RETURN_SALE):
            je = post_sale_invoice(invoice)
        else:
            je = post_purchase_invoice(invoice)
        invoice.journal_entry = je
        invoice.save(update_fields=['journal_entry'])

    return invoice



@transaction.atomic
def cancel_invoice(invoice_id: int, user=None):
    """
    Cancels an invoice (DRAFT or POSTED).

    If DRAFT:
      - Sets status to CANCELLED.

    If POSTED:
      - Reverses linked journal entries (main accrual entry + payment settlement entries):
        each active POSTED entry gets a reversal entry (debit <-> credit); nothing is deleted,
        so the audit trail is kept and the pair nets to zero. invoice.journal_entry is cleared.
      - Reverses inventory effects:
        - For SALE: Restores consumed stock by adding FIFO batches (or resetting serial items).
        - For PURCHASE: Removes inventory batches created by the purchase invoice lines.
        - For RETURN_SALE: Deducts stock via FIFO.
        - For RETURN_PURCHASE: Restores stock by adding FIFO batches.
      - Sets status to CANCELLED.
    """
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.inventory.models import InventoryBatch
    from apps.tenant.services.fifo_engine import add_inventory_batch, consume_fifo_batches
    from apps.tenant.services.journal_service import reverse_document_entries

    invoice = Invoice.objects.select_for_update().get(id=invoice_id)

    if invoice.status == Invoice.CANCELLED:
        raise ValueError(f'الفاتورة رقم "{invoice.invoice_number}" ملغاة بالفعل.')

    # Protection Guard: Cannot cancel an invoice that has returns
    if invoice.return_invoices.exclude(status=Invoice.CANCELLED).exists() or any(line.returned_quantity > 0 for line in invoice.lines.all()):
        raise ValueError(
            f'لا يمكن إلغاء الفاتورة رقم "{invoice.invoice_number}" لوجود مرتجعات محررة عليها. '
            'يجب إلغاء فواتير المرتجع المرتبطة أولاً قبل إلغاء الفاتورة الأصلية.'
        )

    if invoice.status == Invoice.POSTED:
        # 1. Reverse Financial Effects (reversal entries - the posted entries are kept)
        reverse_document_entries(
            invoice,
            reason=f'إلغاء الفاتورة {invoice.invoice_number}',
            user=user,
            extra_entry_ids=[invoice.journal_entry_id],
        )
        invoice.journal_entry = None

        # 2. Reverse Inventory Effects
        lines = list(invoice.lines.select_related('product', 'serial_item', 'uom').all())

        if invoice.invoice_type == Invoice.SALE:
            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue

                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    line.serial_item.is_sold = False
                    line.serial_item.warehouse = invoice.warehouse
                    line.serial_item.save(update_fields=['is_sold', 'warehouse', 'updated_at'])
                    continue

                net_unreturned_qty = line.quantity - line.returned_quantity
                if net_unreturned_qty <= Decimal('0'):
                    continue

                conv = line.uom.conversion_factor if getattr(line, 'uom', None) else Decimal('1')
                base_qty = net_unreturned_qty * conv
                unit_cost = (line.cogs_amount / (line.quantity * conv)).quantize(Decimal('0.0001')) if (line.cogs_amount > 0 and line.quantity > 0) else (line.product.get_average_cost(invoice.warehouse) or Decimal('0'))

                add_inventory_batch(
                    product=line.product,
                    warehouse=invoice.warehouse,
                    quantity=base_qty,
                    unit_cost=unit_cost,
                    invoice_line=line,
                    reference=f'إلغاء فاتورة مبيعات {invoice.invoice_number}',
                    movement_type='RETURN_IN',
                    update_last_purchase_price=False,
                )

        elif invoice.invoice_type == Invoice.PURCHASE:
            from apps.tenant.core.models import SystemSetting
            allow_negative = getattr(SystemSetting.get_settings(), 'allow_negative_stock', False)

            # Pre-validate available stock before making any changes
            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue
                conv = line.uom.conversion_factor if getattr(line, 'uom', None) else Decimal('1')
                net_unreturned_qty = line.quantity - line.returned_quantity
                if net_unreturned_qty <= Decimal('0'):
                    continue
                base_qty = net_unreturned_qty * conv
                available = line.product.get_stock(warehouse=invoice.warehouse)
                if available < base_qty and not allow_negative:
                    raise ValueError(
                        f'لا يمكن إلغاء فاتورة الشراء رقم "{invoice.invoice_number}". '
                        f'الكمية المتاحة من المنتج "{line.product.name}" بالمستودع ({available}) '
                        f'أقل من الكمية المطلوب إلغاؤها ({base_qty}) نظراً لبيع أو صرف جزء من المخزون.'
                    )

            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue

                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    line.serial_item.delete()
                    continue

                conv = line.uom.conversion_factor if getattr(line, 'uom', None) else Decimal('1')
                net_unreturned_qty = line.quantity - line.returned_quantity
                if net_unreturned_qty <= Decimal('0'):
                    continue

                qty_to_remove = net_unreturned_qty * conv
                batches = InventoryBatch.objects.select_for_update().filter(source_invoice_line=line).order_by('created_at')
                from apps.tenant.inventory.models import StockMovement
                for b in batches:
                    if qty_to_remove <= Decimal('0'):
                        break
                    to_deduct = min(b.quantity_remaining, qty_to_remove)
                    if to_deduct > Decimal('0'):
                        b.quantity_remaining -= to_deduct
                        b.save(update_fields=['quantity_remaining', 'updated_at'])
                        StockMovement.objects.create(
                            product=line.product,
                            warehouse=invoice.warehouse,
                            batch=b,
                            movement_type='OUT',
                            quantity=to_deduct,
                            unit_cost=b.unit_cost,
                            reference=f'إلغاء فاتورة شراء {invoice.invoice_number}',
                            notes=f'إلغاء فاتورة شراء {invoice.invoice_number}'
                        )
                        qty_to_remove -= to_deduct
                    if b.quantity_remaining <= Decimal('0'):
                        b.delete()

                if qty_to_remove > Decimal('0'):
                    consume_fifo_batches(
                        product=line.product,
                        warehouse=invoice.warehouse,
                        quantity_needed=qty_to_remove,
                        reference=f'إلغاء فاتورة شراء {invoice.invoice_number}',
                        notes=f'إلغاء فاتورة شراء {invoice.invoice_number} (خصم من مخزون متاح)',
                        movement_type='OUT'
                    )

        elif invoice.invoice_type == Invoice.RETURN_SALE:
            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue

                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    from apps.tenant.inventory.models import SerialItem
                    serial = SerialItem.objects.select_for_update().get(pk=line.serial_item_id)
                    if serial.is_sold or serial.warehouse_id != invoice.warehouse_id:
                        raise ValueError(
                            f'لا يمكن إلغاء المرتجع: السيريال الخاص بالمنتج "{line.product.name}" لم يعد في المستودع.'
                        )
                    serial.is_sold = True
                    serial.warehouse = None
                    serial.save(update_fields=['is_sold', 'warehouse', 'updated_at'])
                    continue

                conv = line.uom.conversion_factor if getattr(line, 'uom', None) else Decimal('1')
                base_qty = line.quantity * conv
                consume_fifo_batches(
                    product=line.product,
                    warehouse=invoice.warehouse,
                    quantity_needed=base_qty,
                    reference=f'إلغاء مرتجع مبيعات {invoice.invoice_number}'
                )

        elif invoice.invoice_type == Invoice.RETURN_PURCHASE:
            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue

                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    line.serial_item.is_returned = False
                    line.serial_item.is_sold = False
                    line.serial_item.warehouse = invoice.warehouse
                    line.serial_item.save(update_fields=['is_returned', 'is_sold', 'warehouse', 'updated_at'])
                    continue

                conv = line.uom.conversion_factor if getattr(line, 'uom', None) else Decimal('1')
                base_qty = line.quantity * conv
                unit_cost = line.product.get_average_cost(invoice.warehouse) or line.product.last_purchase_price
                add_inventory_batch(
                    product=line.product,
                    warehouse=invoice.warehouse,
                    quantity=base_qty,
                    unit_cost=unit_cost,
                    invoice_line=line,
                    reference=f'إلغاء مرتجع مشتريات {invoice.invoice_number}',
                    movement_type='RETURN_IN',
                    update_last_purchase_price=False,
                )

    # Restore returned_quantity on parent invoice lines if this was a return invoice
    if invoice.parent_invoice and invoice.invoice_type in (Invoice.RETURN_SALE, Invoice.RETURN_PURCHASE):
        from apps.tenant.invoicing.models import InvoiceLine
        for rline in invoice.lines.all():
            pline = None
            if rline.original_line_id:
                pline = InvoiceLine.objects.select_for_update().filter(
                    id=rline.original_line_id, invoice=invoice.parent_invoice
                ).first()
            if pline is None:
                # Legacy return lines (created before original_line existed): old behaviour
                pline = invoice.parent_invoice.lines.filter(product=rline.product).first()
            if pline:
                pline.returned_quantity = max(Decimal('0'), pline.returned_quantity - rline.quantity)
                pline.save(update_fields=['returned_quantity'])

    # Update invoice status
    invoice.status = Invoice.CANCELLED
    invoice.save(update_fields=['status', 'journal_entry', 'updated_at'])

    return invoice


