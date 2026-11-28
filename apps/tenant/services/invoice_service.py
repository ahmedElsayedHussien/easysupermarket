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
def check_rep_cash_box(invoice, treasury_id):
    """A sales rep's cash box is used only by that rep's own sales / sales returns (never POS shifts,
    purchases or anyone else's invoice)."""
    from apps.tenant.accounting.models import is_rep_cash_box
    if not is_rep_cash_box(treasury_id):
        return
    rep = invoice.sales_rep if invoice.sales_rep_id else None
    if (rep is None or rep.treasury_id != int(treasury_id) or invoice.pos_session_id
            or invoice.invoice_type not in ('SALE', 'RETURN_SALE')):
        raise ValueError('دي خزنة مندوب — بتتحرك بس من مبيعات المندوب وتحصيلاته وتوريده. اختار خزنة تانية.')


def _confirm_invoice_core(invoice_id: int):
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
        invoice = Invoice.objects.select_for_update(of=('self',)).get(id=invoice_id)
    except Invoice.DoesNotExist:
        raise ValueError(f'الفاتورة رقم {invoice_id} غير موجودة.')

    # --- Validate status ---
    if invoice.status != Invoice.DRAFT:
        raise ValueError(
            f'لا يمكن ترحيل الفاتورة "{invoice.invoice_number}" '
            f'لأنها في حالة "{invoice.get_status_display()}" وليست مسودة.'
        )
    check_rep_cash_box(invoice, invoice.treasury_id)
    if invoice.invoice_type == Invoice.PURCHASE:
        _check_expiry_dates(invoice)

    # --- Validate lines exist ---
    from apps.tenant.services.fifo_engine import lock_order
    # product-id order = the same lock order for every document (no dead-lock between two postings)
    lines = lock_order(list(invoice.lines.select_related('product', 'uom', 'original_line', 'original_line__uom').all()))
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


def _ensure_shift_open(invoice, action_label):
    """A posted invoice of a CLOSED cashier shift is not cancelled / re-paid: the drawer was counted
    against it (same rule as vouchers and transfers)."""
    if invoice.status == 'POSTED' and invoice.pos_session_id:
        from apps.tenant.accounting.models import _ensure_pos_session_open
        _ensure_pos_session_open(invoice.pos_session, action_label)


def _serial_movement(serial, warehouse, movement_type, reference, notes=''):
    """One unit of a serial-tracked product moving: the stock card sees it like any other item."""
    from apps.tenant.inventory.models import StockMovement
    if warehouse is None:
        return
    StockMovement.objects.create(product_id=serial.product_id, warehouse=warehouse, batch=None,
                                 movement_type=movement_type, quantity=Decimal('1'),
                                 unit_cost=serial.actual_cost or Decimal('0'), reference=(reference or '')[:100],
                                 notes=notes or f'سيريال {serial.serial_number}')


def _stock_map(products, warehouse):
    """{product_id: stock in ``warehouse``} like Product.get_stock(), for many products in
    2 queries (batches summed for normal items, unsold serials counted for serialized)."""
    from django.db.models import Count, Sum
    from apps.tenant.inventory.models import InventoryBatch, SerialItem
    serial_ids = {p.id for p in products if p.product_type == 'SERIALIZED'}
    other_ids = {p.id for p in products if p.product_type not in ('SERIALIZED', 'SERVICE')}
    out = {}
    if other_ids:
        out.update(InventoryBatch.objects.filter(product_id__in=other_ids, warehouse=warehouse)
                   .values('product_id').annotate(t=Sum('quantity_remaining')).values_list('product_id', 't'))
    if serial_ids:
        out.update((pid, Decimal(n)) for pid, n in SerialItem.objects.filter(
            product_id__in=serial_ids, warehouse=warehouse, is_sold=False)
            .values('product_id').annotate(n=Count('id')).values_list('product_id', 'n'))
    return {pid: (v or Decimal('0')) for pid, v in out.items()}


def _process_sale_invoice(invoice, lines):
    """
    Internal handler for SALE invoice confirmation.
    """
    from apps.tenant.services.fifo_engine import _negative_stock_allowed, consume_fifo_batches
    from apps.tenant.services.journal_service import post_sale_invoice
    from apps.tenant.invoicing.models import Invoice

    from apps.tenant.inventory.models import SerialItem

    total_cogs = Decimal('0')
    used_serials = set()

    from apps.tenant.core.models import SystemSetting
    system_setting = SystemSetting.get_settings()
    allow_negative_global = getattr(system_setting, 'allow_negative_stock', False)

    # --- Pre-validate all stock before consuming anything ---
    # (stock of every line's product in 2 queries, not one per line; quantities of the
    #  same product on several lines are added together before comparing)
    available_map = _stock_map([line.product for line in lines], invoice.warehouse)
    needed = {}
    for line in lines:
        if line.product.product_type == 'SERVICE':
            continue  # Services have no stock limits
        base_quantity = line.base_quantity
        needed[line.product_id] = needed.get(line.product_id, Decimal('0')) + base_quantity
        base_quantity = needed[line.product_id]
        available = available_map.get(line.product_id, Decimal('0'))
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
            serial = SerialItem.objects.select_for_update(of=('self',)).filter(id=line.serial_item_id).first()
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
            _serial_movement(serial, invoice.warehouse,
                             'RETURN_OUT' if invoice.invoice_type == Invoice.RETURN_PURCHASE else 'OUT',
                             invoice.invoice_number)
            line.serial_item = serial
            line.cogs_amount = serial.actual_cost
            line.save(update_fields=['cogs_amount'])
            total_cogs += line.cogs_amount
            continue

        base_quantity = line.base_quantity
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
                allow_negative=_negative_stock_allowed(line.product, allow_negative_global),
                # a SALE never takes expired batches unless the seller is allowed to
                skip_expired=(not is_return_purchase and not _may_sell_expired(invoice)),
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
        InventoryBatch.objects.select_for_update(of=('self',)).filter(
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
                _serial_movement(line.serial_item, invoice.warehouse,
                                 'RETURN_IN' if invoice.invoice_type == Invoice.RETURN_SALE else 'IN',
                                 invoice.invoice_number)
            continue

        conversion_factor = line.factor
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
                    orig_conv = orig_line.factor
                    orig_base_qty = orig_line.quantity * orig_conv
                    unit_cost = (orig_line.cogs_amount / orig_base_qty).quantize(Decimal('0.0001'))
        else:
            unit_cost = (line.subtotal / base_quantity).quantize(Decimal('0.0001')) if base_quantity > 0 else (line.unit_price / conversion_factor)

        add_inventory_batch(
            product=line.product,
            warehouse=invoice.warehouse,
            quantity=base_quantity,
            unit_cost=unit_cost,
            expiry_date=(_returned_goods_expiry(invoice, line.product) if is_return_sale
                         else getattr(line, 'expiry_date', None)),
            # a customer return is its own batch, so it is never mistaken for a cancelled sale
            batch_number=f'مرتجع {invoice.invoice_number}' if is_return_sale else (getattr(line, 'batch_number', '') or ''),
            invoice_line=line,
            reference=invoice.invoice_number,
            movement_type=m_type,
            user=getattr(invoice, 'cashier', None),
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
    paid_credit: Optional[Decimal] = None,
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

    invoice = Invoice.objects.select_for_update(of=('self',)).select_related('pos_session').get(id=invoice_id)
    _ensure_shift_open(invoice, 'تعديل طريقة سداد')

    # Protection Guard: Cannot modify payment method of an invoice that has returns
    if invoice.return_invoices.exclude(status=Invoice.CANCELLED).exists() or any(line.returned_quantity > 0 for line in invoice.lines.all()):
        raise ValueError(
            f'لا يمكن تعديل طريقة سداد الفاتورة رقم "{invoice.invoice_number}" لوجود مرتجعات محررة عليها.'
        )

    # Update partner if provided
    if partner_id:
        new_partner = Partner.objects.get(id=partner_id)
        invoice.partner = new_partner

    total = invoice.total_amount or Decimal('0')
    paid_cash, paid_card = Decimal(paid_cash or 0), Decimal(paid_card or 0)
    paid_ewallet, paid_bank = Decimal(paid_ewallet or 0), Decimal(paid_bank or 0)
    if min(paid_cash, paid_card, paid_ewallet, paid_bank) < 0:
        raise ValueError('مبالغ السداد مينفعش تكون بالسالب.')
    if payment_type == 'SPLIT':
        # an on-account part only when it is given explicitly: the legs must still add up to the total
        # (a shortfall is never turned into credit silently)
        paid_credit = Decimal(paid_credit or 0)
        if paid_credit < 0:
            raise ValueError('الجزء الآجل مينفعش يكون بالسالب.')
    else:
        # one method carries the whole invoice
        paid_cash = total if payment_type == 'CASH' else Decimal('0')
        paid_card = total if payment_type == 'CARD' else Decimal('0')
        paid_ewallet = total if payment_type == 'EWALLET' else Decimal('0')
        paid_bank = total if payment_type == 'BANK_TRANSFER' else Decimal('0')
        paid_credit = total if payment_type == 'CREDIT' else Decimal('0')

    # Protection Guard: Credit Payment requires an explicit, non-cash partner
    if payment_type == 'CREDIT' or (payment_type == 'SPLIT' and paid_credit > 0):
        curr_partner = invoice.partner
        if not curr_partner or 'عميل نقدي' in curr_partner.name or curr_partner.name.strip() in ['عميل نقدي', 'نقدي', 'طياري', 'مورد نقدي']:
            raise ValueError('لا يمكن تغيير طريقة السداد إلى (آجل) لـ "عميل نقدي". يرجى اختيار عميل/مورد آخر مخصص للآجل.')

    # Split payment: the legs must add up to the invoice total (the screen checks this in the
    # browser, but the server never trusts it). 0.05 covers piastre rounding.
    if payment_type == 'SPLIT':
        legs = paid_cash + paid_card + paid_ewallet + paid_bank + paid_credit
        if abs(legs - invoice.total_amount) > Decimal('0.05'):
            raise ValueError(f'مجموع مبالغ الدفع المقسّم ({legs:.2f}) لا يساوي إجمالي الفاتورة '
                             f'({invoice.total_amount:.2f}).')

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
    check_rep_cash_box(invoice, treasury_id)
    invoice.payment_type = payment_type
    invoice.treasury_id = treasury_id
    invoice.bank_account_id = bank_account_id
    invoice.insta_bank_account_id = insta_bank_account_id
    invoice.ewallet_id = ewallet_id
    invoice.paid_cash = paid_cash
    invoice.paid_card = paid_card
    invoice.paid_ewallet = paid_ewallet
    invoice.paid_bank = paid_bank
    invoice.paid_credit = paid_credit
    # The amounts are re-entered manually here: the POS cash rounding no longer applies.
    invoice.rounding_amount = Decimal('0')

    invoice.save(update_fields=[
        'partner', 'payment_type', 'treasury_id', 'bank_account_id', 'insta_bank_account_id', 'ewallet_id',
        'paid_cash', 'paid_card', 'paid_ewallet', 'paid_bank', 'paid_credit', 'rounding_amount', 'updated_at'
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
def _cancel_invoice_core(invoice_id: int, user=None):
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

    invoice = Invoice.objects.select_for_update(of=('self',)).select_related('pos_session').get(id=invoice_id)

    if invoice.status == Invoice.CANCELLED:
        raise ValueError(f'الفاتورة رقم "{invoice.invoice_number}" ملغاة بالفعل.')
    _ensure_shift_open(invoice, 'إلغاء')

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
        from apps.tenant.services.fifo_engine import lock_order
        lines = lock_order(list(invoice.lines.select_related('product', 'serial_item', 'uom').all()))

        if invoice.invoice_type == Invoice.SALE:
            consumed = _consumed_batches(invoice)
            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue

                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    line.serial_item.is_sold = False
                    line.serial_item.warehouse = invoice.warehouse
                    line.serial_item.save(update_fields=['is_sold', 'warehouse', 'updated_at'])
                    _serial_movement(line.serial_item, invoice.warehouse, 'CANCEL_IN',
                                     f'إلغاء {invoice.invoice_number}')
                    continue

                net_unreturned_qty = line.quantity - line.returned_quantity
                if net_unreturned_qty <= Decimal('0'):
                    continue

                conv = line.factor
                base_qty = net_unreturned_qty * conv
                # back into the very batches the sale consumed (same cost & expiry): a cancellation
                # undoes the sale; only a customer RETURN makes a new ("مرتجع") batch
                base_qty -= _restore_consumed(invoice, line.product, base_qty, consumed)
                if base_qty <= Decimal('0'):
                    continue
                unit_cost = (line.cogs_amount / (line.quantity * conv)).quantize(Decimal('0.0001')) if (line.cogs_amount > 0 and line.quantity > 0) else (line.product.get_average_cost(invoice.warehouse) or Decimal('0'))

                add_inventory_batch(
                    product=line.product,
                    warehouse=invoice.warehouse,
                    quantity=base_qty,
                    unit_cost=unit_cost,
                    invoice_line=line,
                    reference=f'إلغاء فاتورة مبيعات {invoice.invoice_number}',
                    movement_type='CANCEL_IN',
                    update_last_purchase_price=False,
                )

        elif invoice.invoice_type == Invoice.PURCHASE:
            from apps.tenant.inventory.models import InventoryBatch, StockMovement

            # Pre-validate that all purchase batches are intact before making any changes
            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue
                conv = line.factor
                net_unreturned_qty = line.quantity - line.returned_quantity
                if net_unreturned_qty <= Decimal('0'):
                    continue
                base_qty = net_unreturned_qty * conv

                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    continue

                line_batches = InventoryBatch.objects.select_for_update(of=('self',)).filter(source_invoice_line=line)
                batch_remaining = sum((b.quantity_remaining for b in line_batches), Decimal('0'))
                if batch_remaining < base_qty:
                    raise ValueError(
                        f'لا يمكن إلغاء فاتورة الشراء رقم "{invoice.invoice_number}". '
                        f'تم بيع أو استهلاك جزء من بضاعة المنتج "{line.product.name}" بالفعل (المتبقي {batch_remaining} من أصل {base_qty}). '
                        f'يرجى عمل فاتورة مرتجع مشتريات للكمية المتبقية بدلاً من الإلغاء.'
                    )

            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue

                if line.product.product_type == 'SERIALIZED' and line.serial_item_id:
                    from apps.tenant.inventory.models import SerialItem
                    from apps.tenant.invoicing.models import InvoiceLine as _Line
                    serial = SerialItem.objects.select_for_update(of=('self',)).get(pk=line.serial_item_id)
                    if serial.is_sold or serial.warehouse_id != invoice.warehouse_id:
                        raise ValueError(f'مينفعش إلغاء الشراء: السيريال ({serial.serial_number}) اتباع أو اتنقل '
                                         'من المخزن.')
                    _serial_movement(serial, invoice.warehouse, 'OUT', f'إلغاء فاتورة شراء {invoice.invoice_number}')
                    _Line.objects.filter(pk=line.pk).update(serial_item=None)
                    serial.delete()
                    continue

                conv = line.factor
                net_unreturned_qty = line.quantity - line.returned_quantity
                if net_unreturned_qty <= Decimal('0'):
                    continue

                qty_to_remove = net_unreturned_qty * conv
                batches = InventoryBatch.objects.select_for_update(of=('self',)).filter(source_invoice_line=line).order_by('created_at')
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
            # Pre-validate that all return batches are intact
            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue
                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    from apps.tenant.inventory.models import SerialItem
                    serial = SerialItem.objects.select_for_update(of=('self',)).get(pk=line.serial_item_id)
                    if serial.is_sold or serial.warehouse_id != invoice.warehouse_id:
                        raise ValueError(
                            f'لا يمكن إلغاء المرتجع: السيريال الخاص بالمنتج "{line.product.name}" لم يعد في المستودع.'
                        )
                    continue

                conv = line.factor
                base_qty = line.quantity * conv
                ret_batches = InventoryBatch.objects.select_for_update(of=('self',)).filter(source_invoice_line=line)
                batch_remaining = sum((b.quantity_remaining for b in ret_batches), Decimal('0'))
                if batch_remaining < base_qty:
                    raise ValueError(
                        f'لا يمكن إلغاء مرتجع المبيعات رقم "{invoice.invoice_number}". '
                        f'تم إعادة بيع أو صرف جزء من البضاعة المرتجعة للمنتج "{line.product.name}" بالفعل (المتبقي {batch_remaining} من أصل {base_qty}). '
                        f'يرجى عمل فاتورة بيع جديدة بدلاً من الإلغاء.'
                    )

            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue

                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    from apps.tenant.inventory.models import SerialItem
                    serial = SerialItem.objects.select_for_update(of=('self',)).get(pk=line.serial_item_id)
                    serial.is_sold = True
                    serial.warehouse = None
                    serial.save(update_fields=['is_sold', 'warehouse', 'updated_at'])
                    _serial_movement(serial, invoice.warehouse, 'CANCEL_OUT', f'إلغاء مرتجع {invoice.invoice_number}')
                    continue

                conv = line.factor
                base_qty = line.quantity * conv
                from apps.tenant.inventory.models import StockMovement
                for b in InventoryBatch.objects.select_for_update(of=('self',)).filter(
                        source_invoice_line=line, quantity_remaining__gt=0).order_by('id'):
                    take = min(b.quantity_remaining, base_qty)
                    if take <= Decimal('0'):
                        break
                    b.quantity_remaining -= take
                    b.save(update_fields=['quantity_remaining', 'updated_at'])
                    StockMovement.objects.create(
                        product=line.product, warehouse=invoice.warehouse, batch=b, movement_type='CANCEL_OUT',
                        quantity=take, unit_cost=b.unit_cost, reference=f'إلغاء مرتجع {invoice.invoice_number}'[:100],
                        notes=f'إلغاء مرتجع مبيعات {invoice.invoice_number} — من دفعة المرتجع نفسها')
                    base_qty -= take
                if base_qty > Decimal('0'):          # part of the return batch was already sold again
                    consume_fifo_batches(
                        product=line.product,
                        warehouse=invoice.warehouse,
                        quantity_needed=base_qty,
                        reference=f'إلغاء مرتجع مبيعات {invoice.invoice_number}'
                    )

        elif invoice.invoice_type == Invoice.RETURN_PURCHASE:
            # the goods go back to the very batches the return took them from, at the cost they left at
            # (the reversal entry puts them back on 1140 at that cost)
            consumed = _consumed_batches(invoice, 'RETURN_OUT')
            for line in lines:
                if line.product.product_type == 'SERVICE':
                    continue

                if line.product.product_type == 'SERIALIZED' and line.serial_item:
                    line.serial_item.is_returned = False
                    line.serial_item.is_sold = False
                    line.serial_item.warehouse = invoice.warehouse
                    line.serial_item.save(update_fields=['is_returned', 'is_sold', 'warehouse', 'updated_at'])
                    _serial_movement(line.serial_item, invoice.warehouse, 'CANCEL_IN',
                                     f'إلغاء مرتجع مشتريات {invoice.invoice_number}')
                    continue

                conv = line.factor
                base_qty = line.quantity * conv
                base_qty -= _restore_consumed(invoice, line.product, base_qty, consumed,
                                              reference=f'إلغاء مرتجع مشتريات {invoice.invoice_number}')
                if base_qty <= Decimal('0'):
                    continue
                # no movement found (old data): at the cost booked on the line, else the price net of discount
                full_qty = line.quantity * conv
                unit_cost = ((line.cogs_amount / full_qty) if (line.cogs_amount and full_qty) else
                             (line.subtotal / full_qty if full_qty else Decimal('0'))).quantize(Decimal('0.0001'))
                add_inventory_batch(
                    product=line.product,
                    warehouse=invoice.warehouse,
                    quantity=base_qty,
                    unit_cost=unit_cost,
                    invoice_line=line,
                    reference=f'إلغاء مرتجع مشتريات {invoice.invoice_number}',
                    movement_type='CANCEL_IN',
                    update_last_purchase_price=False,
                )

    # Restore returned_quantity on parent invoice lines if this was a return invoice
    if invoice.parent_invoice and invoice.invoice_type in (Invoice.RETURN_SALE, Invoice.RETURN_PURCHASE):
        from apps.tenant.invoicing.models import InvoiceLine
        for rline in invoice.lines.all():
            pline = None
            if rline.original_line_id:
                pline = InvoiceLine.objects.select_for_update(of=('self',)).filter(
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


def _consumed_batches(invoice, movement_type='OUT'):
    """{product_id: [[batch, qty still to give back, cost it left at], ...]} — the batches the document
    took its goods from (its OUT / RETURN_OUT stock movements, newest first)."""
    from apps.tenant.inventory.models import StockMovement
    out = {}
    for m in (StockMovement.objects.filter(reference=invoice.invoice_number, movement_type=movement_type,
                                           warehouse=invoice.warehouse, batch__isnull=False)
              .select_related('batch').order_by('-id')):
        out.setdefault(m.product_id, []).append([m.batch, m.quantity, m.unit_cost])
    return out


def _restore_consumed(invoice, product, qty, consumed, reference=None):
    """Give ``qty`` back to the batches the sale consumed; returns what was given back.
    The goods come back at the cost they left at (the reversal entry credits COGS at that cost): if the
    batch's cost was changed since (e.g. an opening-balance cost edit), they go to a new batch at the
    original cost instead, so stock value and the inventory account stay equal."""
    from apps.tenant.inventory.models import InventoryBatch, StockMovement
    from apps.tenant.services.fifo_engine import add_inventory_batch
    given = Decimal('0')
    for slot in consumed.get(product.id, []):
        if qty - given <= Decimal('0'):
            break
        batch, left, out_cost = slot
        take = min(left, qty - given)
        if take <= Decimal('0'):
            continue
        b = InventoryBatch.objects.select_for_update(of=('self',)).get(pk=batch.pk)
        if out_cost is not None and b.unit_cost != out_cost:
            add_inventory_batch(product=product, warehouse=invoice.warehouse, quantity=take, unit_cost=out_cost,
                                expiry_date=b.expiry_date, batch_number=(b.batch_number or '')[:90],
                                reference=(reference or f'إلغاء {invoice.invoice_number}')[:100], movement_type='CANCEL_IN',
                                update_last_purchase_price=False,
                                notes=f'إلغاء {invoice.invoice_number} — بتكلفة خروجها الأصلية')
            slot[1] = left - take
            given += take
            continue
        b.quantity_remaining += take
        b.save(update_fields=['quantity_remaining', 'updated_at'])
        StockMovement.objects.create(
            product=product, warehouse=invoice.warehouse, batch=b, movement_type='CANCEL_IN', quantity=take,
            unit_cost=b.unit_cost, reference=(reference or f'إلغاء {invoice.invoice_number}')[:100],
            notes=f'إلغاء {invoice.invoice_number} — رجوع لنفس الدفعة')
        slot[1] = left - take
        given += take
    return given


def _queue_einvoice(invoice):
    """Keep the Tax-Authority queue in step with the invoice (explicit call, no signal)."""
    from apps.tenant.einvoicing.signals import create_einvoice_log
    create_einvoice_log(type(invoice), invoice)


def confirm_invoice(invoice_id: int):
    """Post a DRAFT invoice (see _confirm_invoice_core), then queue it for the e-invoice system."""
    from django.db import transaction
    with transaction.atomic():
        invoice = _confirm_invoice_core(invoice_id)
        _queue_einvoice(invoice)
    return invoice


def cancel_invoice(invoice_id: int, user=None):
    """Cancel an invoice (see _cancel_invoice_core), then update its e-invoice queue entry."""
    from django.db import transaction
    with transaction.atomic():
        invoice = _cancel_invoice_core(invoice_id, user=user)
        _queue_einvoice(invoice)
    return invoice


def _may_sell_expired(invoice):
    user = invoice.cashier if getattr(invoice, 'cashier_id', None) else None
    return bool(user and user.has_perm('inventory.sell_expired_batch'))


def _check_expiry_dates(invoice):
    """A purchase of a product that has an expiry date must say it (the batch keeps it for FEFO)."""
    missing = [l.product.name for l in invoice.lines.select_related('product')
               if getattr(l.product, 'has_expiry', False) and not getattr(l, 'expiry_date', None)]
    if missing:
        raise ValueError('اكتب تاريخ الصلاحية للأصناف: ' + '، '.join(missing)
                         + ' (لو العمود مش ظاهر فعّل تتبع الصلاحية من الإعدادات).')


def _returned_goods_expiry(return_invoice, product):
    """Expiry date for goods a customer gives back: the NEAREST expiry of the batches the original sale
    took them from (FEFO then never treats them as fresh). None when the sale is unknown or had none."""
    from apps.tenant.inventory.models import StockMovement
    parent = return_invoice.parent_invoice if getattr(return_invoice, 'parent_invoice_id', None) else None
    if parent is None:
        return None
    return (StockMovement.objects.filter(reference=parent.invoice_number, movement_type='OUT', product=product,
                                         batch__expiry_date__isnull=False)
            .order_by('batch__expiry_date').values_list('batch__expiry_date', flat=True).first())
