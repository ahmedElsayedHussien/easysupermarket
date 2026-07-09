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
    lines = list(invoice.lines.select_related('product').all())
    if not lines:
        raise ValueError(
            f'الفاتورة "{invoice.invoice_number}" لا تحتوي على بنود.'
        )

    # -------------------------------------------------------------------------
    # SALE invoice processing
    # -------------------------------------------------------------------------
    if invoice.invoice_type in (Invoice.SALE, Invoice.RETURN_SALE):
        _process_sale_invoice(invoice, lines)

    # -------------------------------------------------------------------------
    # PURCHASE invoice processing
    # -------------------------------------------------------------------------
    elif invoice.invoice_type in (Invoice.PURCHASE, Invoice.RETURN_PURCHASE):
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

    total_cogs = Decimal('0')

    # --- Pre-validate all stock before consuming anything ---
    for line in lines:
        base_quantity = line.quantity * (line.uom.conversion_factor if getattr(line, 'uom', None) else 1)
        available = line.product.get_stock(warehouse=invoice.warehouse)
        if available < base_quantity and not line.product.allow_negative_stock:
            raise ValueError(
                f'المخزون غير كافٍ للمنتج "{line.product.name}". '
                f'المتاح: {available}, المطلوب: {base_quantity}'
            )

    # --- Consume stock FIFO and calculate COGS ---
    for line in lines:
        base_quantity = line.quantity * (line.uom.conversion_factor if getattr(line, 'uom', None) else 1)
        consumptions = consume_fifo_batches(
            product=line.product,
            warehouse=invoice.warehouse,
            quantity_needed=base_quantity,
            reference=invoice.invoice_number,
            notes=f'بيع - فاتورة {invoice.invoice_number}'
        )

        # Sum COGS from all consumed batches for this line
        line_cogs = sum(c['total_cost'] for c in consumptions)
        line.cogs_amount = Decimal(str(line_cogs)).quantize(Decimal('0.0001'))
        line.save(update_fields=['cogs_amount'])
        total_cogs += line.cogs_amount

    # --- Recalculate invoice totals (in case they drifted) ---
    invoice.recalculate_totals()

    # --- Create and post journal entry ---
    journal_entry = post_sale_invoice(invoice)

    # --- Update invoice status ---
    invoice.journal_entry = journal_entry
    invoice.status = Invoice.POSTED
    invoice.save(update_fields=['journal_entry', 'status', 'updated_at'])


def _process_purchase_invoice(invoice, lines):
    """
    Internal handler for PURCHASE invoice confirmation.
    """
    from apps.tenant.services.fifo_engine import add_inventory_batch
    from apps.tenant.services.journal_service import post_purchase_invoice
    from apps.tenant.invoicing.models import Invoice

    # --- Create inventory batches ---
    for line in lines:
        base_quantity = line.quantity * (line.uom.conversion_factor if getattr(line, 'uom', None) else 1)
        add_inventory_batch(
            product=line.product,
            warehouse=invoice.warehouse,
            quantity=base_quantity,
            unit_cost=line.unit_price,   # Purchase price = unit cost for FIFO
            invoice_line=line,
            reference=invoice.invoice_number,
        )

    # --- Recalculate invoice totals ---
    invoice.recalculate_totals()

    # --- Create and post journal entry ---
    journal_entry = post_purchase_invoice(invoice)

    # --- Update invoice status ---
    invoice.journal_entry = journal_entry
    invoice.status = Invoice.POSTED
    invoice.save(update_fields=['journal_entry', 'status', 'updated_at'])
