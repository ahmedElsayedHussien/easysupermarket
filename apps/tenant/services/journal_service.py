"""
Journal Service
===============
Creates balanced double-entry journal entries for financial transactions.

Functions:
  - post_sale_invoice(): Revenue + VAT + COGS entries for sales
  - post_purchase_invoice(): Inventory + payable/cash entries for purchases
  - post_payment(): Cash receipt/payment entries

All functions:
  - Are decorated with @transaction.atomic
  - Return the created JournalEntry
  - Validate balance before posting
  - Use account codes from Chart of Accounts

Required Chart of Accounts codes (created by setup_chart_of_accounts command):
  1110 - النقدية والخزينة
  1120 - البنوك
  1130 - ذمم مدينة - عملاء
  1140 - المخزون
  2110 - ذمم دائنة - موردون
  2120 - ضريبة الخصم والإضافة (Tax Withholding)
  2130 - ضريبة القيمة المضافة (VAT Payable)
  4100 - المبيعات
  5100 - تكلفة البضاعة المباعة (COGS)
"""
from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def _get_system_account(code: str):
    """
    Fetches an Account by its code.

    Args:
        code: Account code string (e.g., '1110')

    Returns:
        Account instance

    Raises:
        ValueError: If the account does not exist
    """
    from apps.tenant.accounting.models import Account
    try:
        return Account.objects.get(code=code, is_active=True)
    except Account.DoesNotExist:
        if code == '1150':
            curr_assets = Account.objects.filter(code='1100').first()
            if curr_assets:
                return Account.objects.create(
                    code='1150', name='ضرائب مدينة - خصم منبع', 
                    account_type=Account.ASSET, parent=curr_assets
                )
        raise ValueError(
            f'الحساب برقم الكود "{code}" غير موجود. '
            f'الرجاء تشغيل أمر "setup_chart_of_accounts" أولاً.'
        )


def _next_reference(prefix: str) -> str:
    """
    Generates the next sequential journal entry reference.
    Format: JE-SALE-2024-00001, JE-PUR-2024-00001, etc.

    Args:
        prefix: A short prefix like 'SALE', 'PUR', 'PAY'

    Returns:
        str: Next unique reference string
    """
    from apps.tenant.accounting.models import JournalEntry
    year = timezone.now().year
    full_prefix = f'JE-{prefix}-{year}-'
    last = JournalEntry.objects.filter(
        reference__startswith=full_prefix
    ).order_by('-reference').first()
    if last:
        try:
            last_num = int(last.reference.split('-')[-1])
        except (ValueError, IndexError):
            last_num = 0
    else:
        last_num = 0
    return f'{full_prefix}{last_num + 1:05d}'


@transaction.atomic
def post_sale_invoice(invoice) -> 'JournalEntry':
    from apps.tenant.accounting.models import JournalEntry, JournalItem
    from django.contrib.contenttypes.models import ContentType

    # --- Determine accounts ---
    # ALWAYS route through Accounts Receivable (for traceability)
    if invoice.partner and invoice.partner.receivable_account:
        debit_account = invoice.partner.receivable_account
    else:
        debit_account = _get_system_account('1130')  # Default A/R

    revenue_account = _get_system_account('4100')    # Sales Revenue
    cogs_account = _get_system_account('5100')        # COGS
    inventory_account = _get_system_account('1140')   # Inventory

    # --- Totals ---
    subtotal = invoice.subtotal
    net_subtotal = subtotal - invoice.discount_amount
    vat_amount = invoice.tax_amount
    wht_amount = invoice.wht_amount
    total_amount = invoice.total_amount

    # Total COGS from all lines
    from django.db.models import Sum
    total_cogs = invoice.lines.aggregate(
        c=Sum('cogs_amount')
    )['c'] or Decimal('0')

    # --- Create Entry (Accounts Receivable) ---
    reference = _next_reference('SALE')
    entry = JournalEntry.objects.create(
        date=invoice.date,
        reference=reference,
        description=f'قيد استحقاق مبيعات - فاتورة رقم {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}',
        status=JournalEntry.DRAFT,
    )

    # Link to source document
    entry.content_type = ContentType.objects.get_for_model(invoice)
    entry.object_id = invoice.id
    entry.save(update_fields=['content_type', 'object_id'])

    items_to_create = []

    # Dr: A/R
    if total_amount > 0:
        items_to_create.append(JournalItem(
            entry=entry,
            account=debit_account,
            debit=total_amount,
            credit=Decimal('0'),
            description=f'استحقاق مبيعات (عميل) - {invoice.invoice_number}'
        ))

    # Dr: WHT (Tax Withheld by customer)
    if wht_amount > 0:
        wht_account = _get_system_account('1150')
        items_to_create.append(JournalItem(
            entry=entry,
            account=wht_account,
            debit=wht_amount,
            credit=Decimal('0'),
            description=f'ضريبة خصم منبع - {invoice.invoice_number}'
        ))

    # Cr: VAT
    if vat_amount > 0:
        vat_account = _get_system_account('2130')
        items_to_create.append(JournalItem(
            entry=entry,
            account=vat_account,
            debit=Decimal('0'),
            credit=vat_amount,
            description=f'ضريبة القيمة المضافة - {invoice.invoice_number}'
        ))

    # Cr: Revenue (Net of discount, excluding tax)
    if net_subtotal > 0:
        items_to_create.append(JournalItem(
            entry=entry,
            account=revenue_account,
            debit=Decimal('0'),
            credit=net_subtotal,
            description=f'المبيعات - {invoice.invoice_number}'
        ))

    # COGS entry: Dr COGS / Cr Inventory
    if total_cogs > 0:
        items_to_create.append(JournalItem(
            entry=entry,
            account=cogs_account,
            debit=total_cogs,
            credit=Decimal('0'),
            description=f'تكلفة البضاعة المباعة - {invoice.invoice_number}'
        ))
        items_to_create.append(JournalItem(
            entry=entry,
            account=inventory_account,
            debit=Decimal('0'),
            credit=total_cogs,
            description=f'إخراج مخزون - {invoice.invoice_number}'
        ))
        
    # Reverse entries for RETURN_SALE
    if invoice.invoice_type == 'RETURN_SALE':
        reference = _next_reference('RSAL')
        entry.reference = reference
        entry.description = f'قيد استحقاق مرتجع مبيعات - فاتورة رقم {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}'
        entry.save(update_fields=['reference', 'description'])
        for item in items_to_create:
            item.debit, item.credit = item.credit, item.debit
            item.description = item.description.replace('مبيعات', 'مرتجع مبيعات').replace('استحقاق', 'عكس استحقاق')

    JournalItem.objects.bulk_create(items_to_create)
    entry.post()
    logger.info(f'Journal posted for sale invoice {invoice.invoice_number}: {reference}')

    # --- Create Receipt Entry if Paid Immediately ---
    if invoice.payment_type in ['CASH', 'CARD', 'EWALLET', 'BANK_TRANSFER'] and total_amount > 0:
        # Determine Treasury Account
        if invoice.payment_type == 'EWALLET' and getattr(invoice, 'ewallet_id', None) and invoice.ewallet.account_id:
            treasury_account = invoice.ewallet.account
        elif invoice.payment_type == 'BANK_TRANSFER' and getattr(invoice, 'bank_account_id', None) and invoice.bank_account.account_id:
            treasury_account = invoice.bank_account.account
        elif invoice.payment_type in ['CASH', 'CARD'] and getattr(invoice, 'treasury_id', None) and invoice.treasury.account_id:
            treasury_account = invoice.treasury.account
        else:
            if invoice.payment_type == 'CASH':
                treasury_account = _get_system_account('1110')
            elif invoice.payment_type == 'EWALLET':
                treasury_account = _get_system_account('1160')
            else:
                treasury_account = _get_system_account('1120')

        pay_reference = _next_reference('PAY')
        pay_desc = f'تحصيل فاتورة مبيعات نقدية - {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}'
        
        if invoice.invoice_type == 'RETURN_SALE':
            pay_desc = f'استرداد نقدي لمرتجع مبيعات - {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}'
            
        pay_entry = JournalEntry.objects.create(
            date=invoice.date,
            reference=pay_reference,
            description=pay_desc,
            status=JournalEntry.DRAFT,
        )
        pay_entry.content_type = ContentType.objects.get_for_model(invoice)
        pay_entry.object_id = invoice.id
        pay_entry.save(update_fields=['content_type', 'object_id'])
        
        pay_items = []
        if invoice.invoice_type == 'RETURN_SALE':
            # Paying cash back to customer
            pay_items.append(JournalItem(entry=pay_entry, account=debit_account, debit=total_amount, credit=Decimal('0'), description=pay_desc))
            pay_items.append(JournalItem(entry=pay_entry, account=treasury_account, debit=Decimal('0'), credit=total_amount, description=pay_desc))
        else:
            # Receiving cash from customer
            pay_items.append(JournalItem(entry=pay_entry, account=treasury_account, debit=total_amount, credit=Decimal('0'), description=pay_desc))
            pay_items.append(JournalItem(entry=pay_entry, account=debit_account, debit=Decimal('0'), credit=total_amount, description=pay_desc))
            
        JournalItem.objects.bulk_create(pay_items)
        pay_entry.post()
        logger.info(f'Receipt Journal posted for sale invoice {invoice.invoice_number}: {pay_reference}')

    return entry


@transaction.atomic
def post_purchase_invoice(invoice) -> 'JournalEntry':
    from apps.tenant.accounting.models import JournalEntry, JournalItem
    from django.contrib.contenttypes.models import ContentType

    # --- Accounts ---
    inventory_account = _get_system_account('1140')   # Inventory

    # ALWAYS route through Accounts Payable (for traceability)
    if invoice.partner and invoice.partner.payable_account:
        credit_account = invoice.partner.payable_account
    else:
        credit_account = _get_system_account('2110')  # Default A/P

    # --- Totals ---
    subtotal = invoice.subtotal
    net_subtotal = subtotal - invoice.discount_amount
    vat_amount = invoice.tax_amount
    wht_amount = invoice.wht_amount
    total_amount = invoice.total_amount

    # --- Create Entry (Accounts Payable) ---
    reference = _next_reference('PUR')
    entry = JournalEntry.objects.create(
        date=invoice.date,
        reference=reference,
        description=f'قيد استحقاق مشتريات - فاتورة رقم {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}',
        status=JournalEntry.DRAFT,
    )

    entry.content_type = ContentType.objects.get_for_model(invoice)
    entry.object_id = invoice.id
    entry.save(update_fields=['content_type', 'object_id'])

    items_to_create = []

    # Dr: Inventory (net cost)
    if net_subtotal > 0:
        items_to_create.append(JournalItem(
            entry=entry,
            account=inventory_account,
            debit=net_subtotal,
            credit=Decimal('0'),
            description=f'مخزون مشتريات - {invoice.invoice_number}'
        ))

    # Dr: Input VAT
    if vat_amount > 0:
        vat_account = _get_system_account('2130')
        items_to_create.append(JournalItem(
            entry=entry,
            account=vat_account,
            debit=vat_amount,
            credit=Decimal('0'),
            description=f'ضريبة القيمة المضافة - {invoice.invoice_number}'
        ))

    # Cr: Tax Withholding
    if wht_amount > 0:
        wht_account = _get_system_account('2120')
        items_to_create.append(JournalItem(
            entry=entry,
            account=wht_account,
            debit=Decimal('0'),
            credit=wht_amount,
            description=f'ضريبة الخصم والإضافة - {invoice.invoice_number}'
        ))

    # Cr: Accounts Payable
    if total_amount > 0:
        items_to_create.append(JournalItem(
            entry=entry,
            account=credit_account,
            debit=Decimal('0'),
            credit=total_amount,
            description=f'استحقاق مشتريات (مورد) - {invoice.invoice_number}'
        ))

    # Reverse entries for RETURN_PURCHASE
    if invoice.invoice_type == 'RETURN_PURCHASE':
        reference = _next_reference('RPUR')
        entry.reference = reference
        entry.description = f'قيد استحقاق مرتجع مشتريات - فاتورة رقم {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}'
        entry.save(update_fields=['reference', 'description'])
        for item in items_to_create:
            item.debit, item.credit = item.credit, item.debit
            item.description = item.description.replace('مشتريات', 'مرتجع مشتريات').replace('استحقاق', 'عكس استحقاق')

    JournalItem.objects.bulk_create(items_to_create)
    entry.post()
    logger.info(f'Journal posted for purchase invoice {invoice.invoice_number}: {reference}')

    # --- Create Payment Entry if Paid Immediately ---
    if invoice.payment_type in ['CASH', 'CARD', 'EWALLET', 'BANK_TRANSFER'] and total_amount > 0:
        # Determine Treasury Account
        if invoice.payment_type == 'EWALLET' and getattr(invoice, 'ewallet_id', None) and invoice.ewallet.account_id:
            treasury_account = invoice.ewallet.account
        elif invoice.payment_type == 'BANK_TRANSFER' and getattr(invoice, 'bank_account_id', None) and invoice.bank_account.account_id:
            treasury_account = invoice.bank_account.account
        elif invoice.payment_type in ['CASH', 'CARD'] and getattr(invoice, 'treasury_id', None) and invoice.treasury.account_id:
            treasury_account = invoice.treasury.account
        else:
            if invoice.payment_type == 'CASH':
                treasury_account = _get_system_account('1110')
            elif invoice.payment_type == 'EWALLET':
                treasury_account = _get_system_account('1160')
            else:
                treasury_account = _get_system_account('1120')

        pay_reference = _next_reference('PAY')
        pay_desc = f'سداد فاتورة مشتريات نقدية - {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}'
        
        if invoice.invoice_type == 'RETURN_PURCHASE':
            pay_desc = f'استرداد نقدي لمرتجع مشتريات - {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}'
            
        pay_entry = JournalEntry.objects.create(
            date=invoice.date,
            reference=pay_reference,
            description=pay_desc,
            status=JournalEntry.DRAFT,
        )
        pay_entry.content_type = ContentType.objects.get_for_model(invoice)
        pay_entry.object_id = invoice.id
        pay_entry.save(update_fields=['content_type', 'object_id'])
        
        pay_items = []
        if invoice.invoice_type == 'RETURN_PURCHASE':
            # Receiving cash back
            pay_items.append(JournalItem(entry=pay_entry, account=treasury_account, debit=total_amount, credit=Decimal('0'), description=pay_desc))
            pay_items.append(JournalItem(entry=pay_entry, account=credit_account, debit=Decimal('0'), credit=total_amount, description=pay_desc))
        else:
            # Paying cash
            pay_items.append(JournalItem(entry=pay_entry, account=credit_account, debit=total_amount, credit=Decimal('0'), description=pay_desc))
            pay_items.append(JournalItem(entry=pay_entry, account=treasury_account, debit=Decimal('0'), credit=total_amount, description=pay_desc))
            
        JournalItem.objects.bulk_create(pay_items)
        pay_entry.post()
        logger.info(f'Payment Journal posted for purchase invoice {invoice.invoice_number}: {pay_reference}')

    return entry


@transaction.atomic
def post_payment(payment) -> 'JournalEntry':
    """
    Creates and posts a journal entry for a customer receipt or supplier payment.

    Customer Receipt (RECEIPT):
      Dr 1110/1120 (Cash/Bank)            amount
        Cr 1130 (A/R - Customer)          amount

    Supplier Payment (PAYMENT):
      Dr 2110 (A/P - Supplier)            amount
        Cr 1110/1120 (Cash/Bank)          amount

    Args:
        payment: Payment instance

    Returns:
        JournalEntry instance (POSTED status)

    Raises:
        ValueError: If required accounts are missing or amount is zero
    """
    from apps.tenant.accounting.models import JournalEntry, JournalItem
    from django.contrib.contenttypes.models import ContentType

    if payment.amount <= Decimal('0'):
        raise ValueError('مبلغ الدفعة يجب أن يكون أكبر من صفر.')

    # Cash/Bank account based on payment method
    if payment.method == 'CASH':
        cash_account = _get_system_account('1110')
    else:
        cash_account = _get_system_account('1120')  # CARD or BANK

    reference = _next_reference('PAY')

    if payment.payment_type == 'RECEIPT':
        # Receiving money from customer
        if payment.partner.receivable_account:
            partner_account = payment.partner.receivable_account
        else:
            partner_account = _get_system_account('1130')

        description = (
            f'تحصيل من عميل - {payment.partner.name} - {payment.reference}'
        )
        entry = JournalEntry.objects.create(
            date=payment.date,
            reference=reference,
            description=description,
            status=JournalEntry.DRAFT,
        )
        entry.content_type = ContentType.objects.get_for_model(payment)
        entry.object_id = payment.id
        entry.save(update_fields=['content_type', 'object_id'])

        JournalItem.objects.create(
            entry=entry, account=cash_account,
            debit=payment.amount, credit=Decimal('0'),
            description=description
        )
        JournalItem.objects.create(
            entry=entry, account=partner_account,
            debit=Decimal('0'), credit=payment.amount,
            description=description
        )

    else:  # PAYMENT - paying supplier
        if payment.partner.payable_account:
            partner_account = payment.partner.payable_account
        else:
            partner_account = _get_system_account('2110')

        description = (
            f'دفع لمورد - {payment.partner.name} - {payment.reference}'
        )
        entry = JournalEntry.objects.create(
            date=payment.date,
            reference=reference,
            description=description,
            status=JournalEntry.DRAFT,
        )
        entry.content_type = ContentType.objects.get_for_model(payment)
        entry.object_id = payment.id
        entry.save(update_fields=['content_type', 'object_id'])

        JournalItem.objects.create(
            entry=entry, account=partner_account,
            debit=payment.amount, credit=Decimal('0'),
            description=description
        )
        JournalItem.objects.create(
            entry=entry, account=cash_account,
            debit=Decimal('0'), credit=payment.amount,
            description=description
        )

    entry.post()

    # Link the journal entry back to the payment
    payment.journal_entry = entry
    payment.status = 'POSTED'
    payment.save(update_fields=['journal_entry', 'status', 'updated_at'])

    logger.info(f'Journal posted for payment {payment.reference}: {reference}')
    return entry
