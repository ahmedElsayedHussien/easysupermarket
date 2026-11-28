"""
Journal Service
===============
Creates balanced double-entry journal entries for financial transactions.

Functions:
  - post_sale_invoice(): Revenue + VAT + COGS entries for sales
  - post_purchase_invoice(): Inventory + payable/cash entries for purchases

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
  1190 - ضرائب خصم وإضافة مدينة (WHT مخصومة بمعرفة العملاء) — تُنشأ تلقائياً عند الحاجة
  2110 - ذمم دائنة - موردون
  2120 - ضريبة الخصم والإضافة (Tax Withholding)
  2130 - ضريبة القيمة المضافة (VAT Payable)
  4100 - المبيعات
  5100 - تكلفة البضاعة المباعة (COGS)
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ضريبة الخصم من المنبع لنا (مدينة) — المخصومة من مبيعاتنا بمعرفة العملاء.
# 1150 في الشجرة الافتراضية هو "عهد نقدية"، لذلك كود مستقل — موجود في setup_chart_of_accounts.
WHT_RECEIVABLE_CODE = '1155'
# ضريبة القيمة المضافة - مدخلات (مشتريات ومصروفات) — أصل. المخرجات (مبيعات) تبقى على 2130.
INPUT_VAT_CODE = '1190'
OUTPUT_VAT_CODE = '2130'
# عمولات ماكينات الدفع والمحافظ والبنوك
PAYMENT_FEES_CODE = '5305'
# فروق تقريب النقدية في نقطة البيع (Invoice.rounding_amount): دائن = مكسب، مدين = خسارة
POS_ROUNDING_CODE = '5115'


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
    from apps.tenant.accounting.account_mapping import mapped_account
    acc = mapped_account(code)               # the admin may have pointed this operation to another account
    if acc:
        return acc
    acc = Account.objects.filter(code=code, is_active=True).first()
    if acc:
        return acc

    raise ValueError(
        f'الحساب برقم الكود "{code}" غير موجود. '
        f'الرجاء تشغيل أمر "setup_chart_of_accounts" أولاً.'
    )


def _next_reference(prefix: str) -> str:
    """
    Generates the next sequential journal entry reference (race-safe).
    Format: JE-SALE-2024-00001, JE-PUR-2024-00001, etc.

    Uses the locked DocumentSequence counter so two concurrent transactions can never
    receive the same reference. Call it inside the atomic block that saves the entry.

    Args:
        prefix: A short prefix like 'SALE', 'PUR', 'PAY'

    Returns:
        str: Next unique reference string
    """
    from apps.tenant.accounting.models import JournalEntry
    from apps.tenant.core.sequences import next_sequence, max_numeric_suffix
    year = timezone.now().year
    full_prefix = f'JE-{prefix}-{year}-'
    num = next_sequence(
        f'journal_entry:{full_prefix}',
        seed=lambda: max_numeric_suffix(JournalEntry.objects.all(), 'reference', full_prefix),
    )
    return f'{full_prefix}{num:05d}'


def unique_reference(base: str) -> str:
    """base, base-2, base-3 ... - the first reference not used by any journal entry.
    Used when a document is re-posted after its previous entry was reversed (the reversed
    original keeps the document number as its reference)."""
    from apps.tenant.accounting.models import JournalEntry
    ref = base
    n = 2
    while JournalEntry.objects.filter(reference=ref).exists():
        ref = f'{base}-{n}'
        n += 1
    return ref


def delete_orphan_entries(reference: str) -> int:
    """
    Deletes leftover entries carrying ``reference`` that are NOT part of a reversal pair
    (e.g. an empty DRAFT header from an interrupted legacy flow). Reversed originals and
    their reversal entries are audit trail and are never deleted.
    """
    from apps.tenant.accounting.models import JournalEntry
    qs = JournalEntry.objects.filter(
        reference=reference, reversal_of__isnull=True, reversed_by__isnull=True, status=JournalEntry.DRAFT
    )
    count = 0
    for entry in qs:
        entry.delete()
        count += 1
    return count


@transaction.atomic
def reverse_journal_entry(entry, reason: str = '', user=None, date=None) -> 'JournalEntry':
    """
    Reverses a POSTED journal entry for the audit trail instead of deleting it:
    posts a new entry with every line's debit/credit swapped (same accounts and line
    descriptions), linked to the original through ``reversal_of`` and to the same source
    document (content_type/object_id). The original stays POSTED, so both entries remain
    visible in the ledger and net to zero in every balance/report.

    Refuses to reverse an entry twice, to reverse a reversal entry, or a non-posted entry.
    Returns the reversal entry.
    """
    from django.core.exceptions import ValidationError
    from apps.tenant.accounting.models import JournalEntry, JournalItem

    original = JournalEntry.objects.select_for_update(of=('self',)).get(pk=entry.pk)
    if original.status != JournalEntry.POSTED:
        raise ValidationError(f'لا يمكن عكس القيد {original.reference} لأنه غير مرحّل.')
    if original.reversal_of_id:
        raise ValidationError(f'القيد {original.reference} قيد عكسي بالفعل ولا يمكن عكسه.')
    if JournalEntry.objects.filter(reversal_of=original).exists():
        raise ValidationError(f'القيد {original.reference} تم عكسه من قبل.')

    items = list(original.items.all())
    if not items:
        raise ValidationError(f'القيد {original.reference} لا يحتوي على بنود لعكسها.')
    description = f'قيد عكسي للقيد {original.reference}'
    if reason:
        description += f' - {reason}'
    reversal = JournalEntry.objects.create(
        date=date or timezone.localdate(),
        reference=_next_reference('REV'),
        description=description,
        status=JournalEntry.DRAFT,
        created_by=user if getattr(user, 'pk', None) else original.created_by,
        content_type_id=original.content_type_id,
        object_id=original.object_id,
        reversal_of=original,
    )
    JournalItem.objects.bulk_create([
        JournalItem(
            entry=reversal,
            account_id=item.account_id,
            debit=item.credit,
            credit=item.debit,
            description=(f'عكس: {item.description}' if item.description else 'عكس قيد')[:500],
        )
        for item in items
    ])
    reversal.post()
    logger.info('Journal entry %s reversed by %s (%s)', original.reference, reversal.reference, reason)
    return reversal


def reverse_entry_if_active(entry, reason: str = '', user=None, date=None):
    """
    Cancels the ledger effect of ``entry`` (if any):
      - POSTED, not yet reversed and not itself a reversal -> reverse_journal_entry()
      - DRAFT (never affected the ledger)                    -> deleted
      - already reversed / a reversal entry                  -> left untouched
    Returns the reversal entry or None. Must run inside the caller's atomic block.
    """
    from apps.tenant.accounting.models import JournalEntry
    if entry is None:
        return None
    entry = JournalEntry.objects.select_for_update(of=('self',)).filter(pk=entry.pk).first()
    if entry is None:
        return None
    if entry.reversal_of_id or JournalEntry.objects.filter(reversal_of=entry).exists():
        return None
    if entry.status != JournalEntry.POSTED:
        entry.delete()
        return None
    return reverse_journal_entry(entry, reason=reason, user=user, date=date)


def reverse_document_entries(document, reason: str = '', user=None, date=None, extra_entry_ids=()):
    """
    Reverses every active journal entry linked to ``document`` through the generic FK
    (content_type/object_id) plus any ``extra_entry_ids`` (e.g. document.journal_entry_id).
    Returns the list of reversal entries created.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Q
    from apps.tenant.accounting.models import JournalEntry

    ct = ContentType.objects.get_for_model(document)
    q = Q(content_type=ct, object_id=document.pk)
    ids = [i for i in extra_entry_ids if i]
    if ids:
        q |= Q(pk__in=ids)
    reversals = []
    for entry in JournalEntry.objects.filter(q).order_by('pk'):
        rev = reverse_entry_if_active(entry, reason=reason, user=user, date=date)
        if rev is not None:
            reversals.append(rev)
    return reversals


# ---------------------------------------------------------------------------
# Treasury / bank / e-wallet sub-account resolution
# ---------------------------------------------------------------------------

def _linked_account(holder):
    """Ledger sub-account of a Treasury / BankAccount / EWallet (auto-created if missing)."""
    if holder is None:
        return None
    ensure = getattr(holder, 'ensure_account', None)
    acc = ensure() if callable(ensure) else None
    return acc or getattr(holder, 'account', None)


def _money_account(candidates, parent_code: str, context: str = ''):
    """
    First specific ledger account found among ``candidates`` (Treasury/BankAccount/EWallet
    instances or None, in preference order). Falls back to the parent account
    ``parent_code`` (1110 / 1120 / 1160) only when nothing is set, logging a warning,
    because postings on the parent cannot be attributed to a specific drawer/bank.
    """
    for holder in candidates:
        acc = _linked_account(holder)
        if acc is not None:
            return acc
    parent = _get_system_account(parent_code)
    if parent.children.exists():
        # postings never go to a group account (JournalEntry.post refuses it): say what is missing
        label = {'1110': 'الخزينة', '1120': 'الحساب البنكي', '1160': 'المحفظة الإلكترونية'}.get(parent_code, 'الحساب')
        from django.core.exceptions import ValidationError
        raise ValidationError(f'حدد {label} اللي الفلوس هتتسجّل عليها ({context or "القيد"}).')
    logger.warning('No specific treasury/bank/e-wallet for %s; posting on %s (no sub-accounts yet).',
                   context or 'journal entry', parent_code)
    return parent


def _partner_account(partner, field, parent_code):
    """The partner's own receivable / payable sub-account (created now if missing); the parent only when
    there is no partner at all."""
    if partner is not None:
        if not getattr(partner, f'{field}_id', None):
            from apps.tenant.partners.models import create_partner_account
            create_partner_account(type(partner), partner, False)
        acc = getattr(partner, field, None)
        if acc is not None:
            return acc
    return _get_system_account(parent_code)


def _single(qs):
    """The only row of qs, or None when there are zero or several rows."""
    rows = list(qs[:2])
    return rows[0] if len(rows) == 1 else None


def _branch_treasury(branch):
    """The branch's treasury when the branch has exactly one (unambiguous)."""
    if branch is None:
        return None
    from apps.tenant.accounting.models import Treasury
    return _single(Treasury.objects.business().filter(branch=branch).order_by('pk'))


def _invoice_cash_account(invoice):
    pos_session = invoice.pos_session if getattr(invoice, 'pos_session_id', None) else None
    return _money_account(
        [
            invoice.treasury if getattr(invoice, 'treasury_id', None) else None,
            pos_session.treasury if (pos_session is not None and pos_session.treasury_id) else None,
            _branch_treasury(invoice.branch if getattr(invoice, 'branch_id', None) else None),
        ],
        '1110', f'invoice {invoice.invoice_number} (cash)',
    )


def _invoice_card_account(invoice):
    return _money_account(
        [invoice.bank_account if getattr(invoice, 'bank_account_id', None) else None],
        '1120', f'invoice {invoice.invoice_number} (card)',
    )


def _invoice_transfer_account(invoice):
    return _money_account(
        [
            invoice.insta_bank_account if getattr(invoice, 'insta_bank_account_id', None) else None,
            invoice.bank_account if getattr(invoice, 'bank_account_id', None) else None,
        ],
        '1120', f'invoice {invoice.invoice_number} (bank transfer)',
    )


def _invoice_ewallet_account(invoice):
    return _money_account(
        [invoice.ewallet if getattr(invoice, 'ewallet_id', None) else None],
        '1160', f'invoice {invoice.invoice_number} (e-wallet)',
    )


@transaction.atomic
def post_sale_invoice(invoice) -> 'JournalEntry':
    from apps.tenant.accounting.models import JournalEntry, JournalItem
    from django.contrib.contenttypes.models import ContentType

    # --- Determine accounts ---
    # ALWAYS route through Accounts Receivable (for traceability)
    debit_account = _partner_account(invoice.partner if invoice.partner_id else None, 'receivable_account', '1130')

    revenue_account = _get_system_account('4100')    # Sales Revenue
    cogs_account = _get_system_account('5100')        # COGS
    inventory_account = _get_system_account('1140')   # Inventory

    # --- Totals ---
    subtotal = invoice.subtotal
    net_subtotal = subtotal
    vat_amount = invoice.tax_amount
    wht_amount = invoice.wht_amount
    total_amount = invoice.total_amount
    # POS cash rounding: cash actually collected = exact due + rounding_amount (+/-).
    rounding_amount = Decimal(str(getattr(invoice, 'rounding_amount', None) or 0))
    if invoice.payment_type not in ('CASH', 'SPLIT') or (invoice.payment_type == 'SPLIT' and (invoice.paid_cash or 0) <= 0):
        rounding_amount = Decimal('0')

    # Total COGS from all lines
    from django.db.models import Sum
    total_cogs = invoice.lines.aggregate(
        c=Sum('cogs_amount')
    )['c'] or Decimal('0')

    # --- Create Entry (Accounts Receivable) ---
    is_return = invoice.invoice_type == 'RETURN_SALE'
    reference = _next_reference('RSAL' if is_return else 'SALE')     # one number, the right series
    user = invoice.cashier if getattr(invoice, 'cashier_id', None) else None
    entry = JournalEntry.objects.create(
        date=invoice.date,
        reference=reference,
        description=(f'قيد استحقاق {"مرتجع مبيعات" if is_return else "مبيعات"} - فاتورة رقم {invoice.invoice_number} - '
                     f'{getattr(invoice.partner, "name", "")}'),
        status=JournalEntry.DRAFT,
        created_by=user,
    )

    # Link to source document
    entry.content_type = ContentType.objects.get_for_model(invoice)
    entry.object_id = invoice.id
    entry.save(update_fields=['content_type', 'object_id'])

    items_to_create = []

    # Dr: Accounts based on payment_type (Cash, Bank, EWallet, A/R)
    if invoice.payment_type == 'SPLIT':
        # Resolve each payment leg's specific sub-account only when that leg has an amount
        cash_acc = _invoice_cash_account(invoice) if (invoice.paid_cash or 0) > 0 else None
        card_acc = _invoice_card_account(invoice) if (invoice.paid_card or 0) > 0 else None
        ewallet_acc = _invoice_ewallet_account(invoice) if (invoice.paid_ewallet or 0) > 0 else None
        bank_acc = _invoice_transfer_account(invoice) if (invoice.paid_bank or 0) > 0 else None

        splits = [
            (cash_acc, invoice.paid_cash or Decimal('0'), f'سداد نقدي - {invoice.invoice_number}'),
            (card_acc, invoice.paid_card or Decimal('0'), f'سداد بطاقة / فيزا - {invoice.invoice_number}'),
            (ewallet_acc, invoice.paid_ewallet or Decimal('0'), f'سداد محفظة إلكترونية - {invoice.invoice_number}'),
            (bank_acc, invoice.paid_bank or Decimal('0'), f'سداد تحويل بنكي / إنستاباي - {invoice.invoice_number}'),
            (debit_account, invoice.paid_credit or Decimal('0'), f'مبيعات آجلة (عميل) - {invoice.invoice_number}'),
        ]
        active_splits = [[acc, amt, desc] for acc, amt, desc in splits if amt > 0]
        split_sum = sum((item[1] for item in active_splits), Decimal('0'))
        # the legs are the total exactly (+ the POS cash rounding, the one rounding there is): never absorbed
        if split_sum != total_amount + rounding_amount:
            raise ValidationError(f'مجموع مبالغ الدفع المقسّم ({split_sum:.2f}) لا يساوي إجمالي الفاتورة '
                                  f'{invoice.invoice_number} ({total_amount + rounding_amount:.2f}).')

        for acc, amt, desc in active_splits:
            if amt > 0:
                items_to_create.append(JournalItem(
                    entry=entry,
                    account=acc,
                    debit=amt,
                    credit=Decimal('0'),
                    description=desc
                ))
    elif invoice.payment_type == 'CASH':
        cash_acc = _invoice_cash_account(invoice)
        items_to_create.append(JournalItem(
            entry=entry,
            account=cash_acc,
            debit=total_amount + rounding_amount,
            credit=Decimal('0'),
            description=f'مبيعات نقدية - {invoice.invoice_number}'
        ))
    elif invoice.payment_type in ('CARD', 'BANK_TRANSFER'):
        bank_acc = _invoice_transfer_account(invoice) if invoice.payment_type == 'BANK_TRANSFER' else _invoice_card_account(invoice)
        desc = f'مبيعات بطاقة - {invoice.invoice_number}' if invoice.payment_type == 'CARD' else f'مبيعات تحويل بنكي - {invoice.invoice_number}'
        items_to_create.append(JournalItem(
            entry=entry,
            account=bank_acc,
            debit=total_amount,
            credit=Decimal('0'),
            description=desc
        ))
    elif invoice.payment_type == 'EWALLET':
        ewallet_acc = _invoice_ewallet_account(invoice)
        items_to_create.append(JournalItem(
            entry=entry,
            account=ewallet_acc,
            debit=total_amount,
            credit=Decimal('0'),
            description=f'مبيعات محفظة إلكترونية - {invoice.invoice_number}'
        ))
    else:  # CREDIT
        if total_amount > 0:
            items_to_create.append(JournalItem(
                entry=entry,
                account=debit_account,
                debit=total_amount,
                credit=Decimal('0'),
                description=f'استحقاق مبيعات (عميل) - {invoice.invoice_number}'
            ))

    # POS cash rounding difference: collected more than due -> Cr 5115 (gain);
    # collected less -> Dr 5115 (loss). Keeps the entry balanced.
    if rounding_amount != 0:
        rounding_account = _get_system_account(POS_ROUNDING_CODE)
        items_to_create.append(JournalItem(
            entry=entry,
            account=rounding_account,
            debit=-rounding_amount if rounding_amount < 0 else Decimal('0'),
            credit=rounding_amount if rounding_amount > 0 else Decimal('0'),
            description=f'فروق تقريب نقطة البيع - {invoice.invoice_number}'
        ))

    # Dr: WHT (Tax Withheld by customer)
    if wht_amount > 0:
        wht_account = _get_system_account(WHT_RECEIVABLE_CODE)
        items_to_create.append(JournalItem(
            entry=entry,
            account=wht_account,
            debit=wht_amount,
            credit=Decimal('0'),
            description=f'ضريبة خصم منبع - {invoice.invoice_number}'
        ))

    # Cr: VAT
    if vat_amount > 0:
        vat_account = _get_system_account(OUTPUT_VAT_CODE)  # Output VAT (sales)
        items_to_create.append(JournalItem(
            entry=entry,
            account=vat_account,
            debit=Decimal('0'),
            credit=vat_amount,
            description=f'ضريبة القيمة المضافة - {invoice.invoice_number}'
        ))

    # --- Cr: Revenue ---
    # We must split revenue between normal products and services (Labor).
    from django.db.models import DecimalField, Q, Sum, Value
    from django.db.models.functions import Coalesce
    _money = DecimalField(max_digits=20, decimal_places=4)
    revenue = invoice.lines.aggregate(
        products=Coalesce(Sum('subtotal', filter=~Q(product__product_type='SERVICE')), Value(Decimal('0')),
                          output_field=_money),
        services=Coalesce(Sum('subtotal', filter=Q(product__product_type='SERVICE')), Value(Decimal('0')),
                          output_field=_money),
    )
    product_revenue_gross = revenue['products']
    service_revenue_gross = revenue['services']
    total_gross = product_revenue_gross + service_revenue_gross

    product_revenue_net = product_revenue_gross.quantize(Decimal('0.00'))
    service_revenue_net = service_revenue_gross.quantize(Decimal('0.00'))

    # If rounding causes a tiny discrepancy, adjust the largest one
    diff = net_subtotal - (product_revenue_net + service_revenue_net)
    if diff != 0:
        if product_revenue_net >= service_revenue_net:
            product_revenue_net += diff
        else:
            service_revenue_net += diff

    if product_revenue_net > 0:
        items_to_create.append(JournalItem(
            entry=entry,
            account=revenue_account,
            debit=Decimal('0'),
            credit=product_revenue_net,
            description=f'المبيعات - {invoice.invoice_number}'
        ))

    if service_revenue_net > 0:
        try:
            service_revenue_account = _get_system_account('4110')
        except ValueError:
            service_revenue_account = revenue_account
        items_to_create.append(JournalItem(
            entry=entry,
            account=service_revenue_account,
            debit=Decimal('0'),
            credit=service_revenue_net,
            description=f'إيرادات المصنعيات والخدمات - {invoice.invoice_number}'
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
    if is_return:
        
        try:
            sales_return_account = _get_system_account('4120')
        except ValueError:
            sales_return_account = revenue_account  # Fallback
            
        for item in items_to_create:
            # Reverse Dr/Cr
            item.debit, item.credit = item.credit, item.debit
            
            # If this is the revenue line, change its account to Sales Returns
            if item.account == revenue_account or item.account.code == '4110':
                item.account = sales_return_account
                
            item.description = item.description.replace('مبيعات', 'مرتجع مبيعات').replace('استحقاق', 'عكس استحقاق')

    JournalItem.objects.bulk_create(items_to_create)
    entry.post(user)
    logger.info(f'Journal posted for sale invoice {invoice.invoice_number}: {reference}')

    return entry


def _create_sale_payment_entry(invoice):
    """Deprecated: Settlement accounts are integrated directly in post_sale_invoice."""
    return None


@transaction.atomic
def post_purchase_invoice(invoice) -> 'JournalEntry':
    from apps.tenant.accounting.models import JournalEntry, JournalItem
    from django.contrib.contenttypes.models import ContentType

    # --- Accounts ---
    inventory_account = _get_system_account('1140')   # Inventory

    # ALWAYS route through Accounts Payable (for traceability)
    credit_account = _partner_account(invoice.partner if invoice.partner_id else None, 'payable_account', '2110')

    # --- Totals ---
    subtotal = invoice.subtotal
    net_subtotal = subtotal
    vat_amount = invoice.tax_amount
    wht_amount = invoice.wht_amount
    total_amount = invoice.total_amount

    # --- Create Entry (Accounts Payable) ---
    is_return = invoice.invoice_type == 'RETURN_PURCHASE'
    reference = _next_reference('RPUR' if is_return else 'PUR')      # one number, the right series
    user = invoice.cashier if getattr(invoice, 'cashier_id', None) else None
    entry = JournalEntry.objects.create(
        date=invoice.date,
        reference=reference,
        description=(f'قيد استحقاق {"مرتجع مشتريات" if is_return else "مشتريات"} - فاتورة رقم {invoice.invoice_number} - '
                     f'{getattr(invoice.partner, "name", "")}'),
        status=JournalEntry.DRAFT,
        created_by=user,
    )

    entry.content_type = ContentType.objects.get_for_model(invoice)
    entry.object_id = invoice.id
    entry.save(update_fields=['content_type', 'object_id'])

    items_to_create = []

    # Dr: Inventory (net cost)
    inventory_value, price_gap = net_subtotal, Decimal('0')
    if is_return:
        from django.db.models import Sum as _Sum
        goods = invoice.lines.exclude(product__product_type='SERVICE').aggregate(
            net=_Sum('subtotal'), cost=_Sum('cogs_amount'))
        goods_net, goods_cost = goods['net'] or Decimal('0'), goods['cost'] or Decimal('0')
        if goods_cost > 0:
            inventory_value = net_subtotal - goods_net + goods_cost      # stock leaves at its batch cost
            price_gap = goods_net - goods_cost                            # + : returned above cost
    if inventory_value > 0:
        items_to_create.append(JournalItem(
            entry=entry,
            account=inventory_account,
            debit=inventory_value,
            credit=Decimal('0'),
            description=f'مخزون مشتريات - {invoice.invoice_number}'
        ))
    if price_gap:
        try:
            gap_account = _get_system_account('5110')     # مردودات ومسموحات المشتريات
        except ValueError:
            gap_account = _get_system_account('5100')
        items_to_create.append(JournalItem(              # the whole entry is mirrored below for a return
            entry=entry, account=gap_account,
            debit=price_gap if price_gap > 0 else Decimal('0'), credit=-price_gap if price_gap < 0 else Decimal('0'),
            description=f'فرق سعر المرتجع عن تكلفة المخزون - {invoice.invoice_number}'
        ))

    # Dr: Input VAT
    if vat_amount > 0:
        vat_account = _get_system_account(INPUT_VAT_CODE)  # Input VAT (purchases) - reversed automatically for purchase returns
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

    # Cr: Accounts Payable / Cash / Bank / EWallet / Split
    if invoice.payment_type == 'SPLIT':
        # Resolve each payment leg's specific sub-account only when that leg has an amount
        cash_acc = _invoice_cash_account(invoice) if (invoice.paid_cash or 0) > 0 else None
        card_acc = _invoice_card_account(invoice) if (invoice.paid_card or 0) > 0 else None
        ewallet_acc = _invoice_ewallet_account(invoice) if (invoice.paid_ewallet or 0) > 0 else None
        bank_acc = _invoice_transfer_account(invoice) if (invoice.paid_bank or 0) > 0 else None

        splits = [
            (cash_acc, invoice.paid_cash or Decimal('0'), f'سداد نقدي مشتريات - {invoice.invoice_number}'),
            (card_acc, invoice.paid_card or Decimal('0'), f'سداد بطاقة مشتريات - {invoice.invoice_number}'),
            (ewallet_acc, invoice.paid_ewallet or Decimal('0'), f'سداد محفظة مشتريات - {invoice.invoice_number}'),
            (bank_acc, invoice.paid_bank or Decimal('0'), f'سداد تحويل بنكي مشتريات - {invoice.invoice_number}'),
            (credit_account, invoice.paid_credit or Decimal('0'), f'مشتريات آجلة (مورد) - {invoice.invoice_number}'),
        ]
        active_splits = [[acc, amt, desc] for acc, amt, desc in splits if amt > 0]
        split_sum = sum((item[1] for item in active_splits), Decimal('0'))
        if split_sum != total_amount:
            raise ValidationError(f'مجموع مبالغ الدفع المقسّم ({split_sum:.2f}) لا يساوي إجمالي الفاتورة '
                                  f'{invoice.invoice_number} ({total_amount:.2f}).')

        for acc, amt, desc in active_splits:
            if amt > 0:
                items_to_create.append(JournalItem(
                    entry=entry,
                    account=acc,
                    debit=Decimal('0'),
                    credit=amt,
                    description=desc
                ))
    elif invoice.payment_type == 'CASH':
        cash_acc = _invoice_cash_account(invoice)
        items_to_create.append(JournalItem(
            entry=entry,
            account=cash_acc,
            debit=Decimal('0'),
            credit=total_amount,
            description=f'مشتريات نقدية - {invoice.invoice_number}'
        ))
    elif invoice.payment_type in ('CARD', 'BANK_TRANSFER'):
        bank_acc = _invoice_transfer_account(invoice) if invoice.payment_type == 'BANK_TRANSFER' else _invoice_card_account(invoice)
        desc = f'مشتريات بطاقة - {invoice.invoice_number}' if invoice.payment_type == 'CARD' else f'مشتريات تحويل بنكي - {invoice.invoice_number}'
        items_to_create.append(JournalItem(
            entry=entry,
            account=bank_acc,
            debit=Decimal('0'),
            credit=total_amount,
            description=desc
        ))
    elif invoice.payment_type == 'EWALLET':
        ewallet_acc = _invoice_ewallet_account(invoice)
        items_to_create.append(JournalItem(
            entry=entry,
            account=ewallet_acc,
            debit=Decimal('0'),
            credit=total_amount,
            description=f'مشتريات محفظة إلكترونية - {invoice.invoice_number}'
        ))
    else:  # CREDIT
        if total_amount > 0:
            items_to_create.append(JournalItem(
                entry=entry,
                account=credit_account,
                debit=Decimal('0'),
                credit=total_amount,
                description=f'استحقاق مشتريات (مورد) - {invoice.invoice_number}'
            ))

    # Reverse entries for RETURN_PURCHASE
    if is_return:
        
        # Perpetual inventory: a purchase return credits Inventory at the batches' cost; only the gap between
        # the return price and that cost goes to purchase returns & allowances (5110) — built above.
        for item in items_to_create:
            item.debit, item.credit = item.credit, item.debit
            item.description = item.description.replace('مشتريات', 'مرتجع مشتريات').replace('استحقاق', 'عكس استحقاق')

    JournalItem.objects.bulk_create(items_to_create)
    entry.post(user)
    logger.info(f'Journal posted for purchase invoice {invoice.invoice_number}: {reference}')

    return entry


def _create_purchase_payment_entry(invoice):
    """Deprecated: Settlement accounts are integrated directly in post_purchase_invoice."""
    return None



def _own_sums(account_ids):
    """{account_id: (total_debit, total_credit)} للقيود المرحلة المسجلة مباشرة على الحسابات (استعلام واحد)."""
    from django.db.models import Sum
    from django.db.models.functions import Coalesce
    from apps.tenant.accounting.models import JournalItem, JournalEntry

    if not account_ids:
        return {}
    rows = JournalItem.objects.filter(
        entry__status=JournalEntry.POSTED,
        account_id__in=list(account_ids)
    ).values('account_id').annotate(
        total_debit=Coalesce(Sum('debit'), Decimal('0.00')),
        total_credit=Coalesce(Sum('credit'), Decimal('0.00'))
    )
    return {r['account_id']: (r['total_debit'], r['total_credit']) for r in rows}


def _signed_balance(acc, total_d, total_c):
    if acc.normal_balance == 'DEBIT':
        return (total_d - total_c).quantize(Decimal('0.01'))
    return (total_c - total_d).quantize(Decimal('0.01'))


def _is_leaf_account(acc):
    return acc.lft is not None and acc.rght is not None and acc.rght - acc.lft == 1


def with_balances(objects):
    """List of treasuries / banks / wallets / POS machines (anything with an ``account``
    FK) whose ``current_balance`` is ready without one query per object. The objects'
    accounts should be select_related; returns the list."""
    objects = list(objects)
    accounts = [o.account for o in objects if getattr(o, 'account_id', None)]
    if accounts:
        batch_get_account_balances(account_qs=accounts)
    return objects


def batch_get_account_balances(account_qs=None, account_ids=None):
    """
    حساب أرصدة مجموعة حسابات (الحساب + كل أبنائه) دفعة واحدة بعدد ثابت من الاستعلامات:
      - استعلام واحد لعُقد الشجرة المطلوبة (فقط عند وجود حسابات رئيسية ضمن المطلوب)
      - استعلام تجميعي واحد للقيود المرحلة
    يعتمد على نطاقات MPTT (tree_id / lft / rght) بدلاً من get_descendants لكل حساب،
    مع اختصار مباشر للحسابات الطرفية (leaf).
    يقوم بحقن الرصيد المحسوب مباشرة في `_prefetched_balance` لكل حساب ليعمل تلقائياً
    مع `account.current_balance` دون تشغيل أي استعلام إضافي.
    """
    from apps.tenant.accounting.models import Account

    if account_qs is None and account_ids is None:
        account_qs = Account.objects.filter(is_active=True)
    elif account_qs is None and account_ids is not None:
        account_qs = Account.objects.filter(id__in=account_ids)

    accounts_list = list(account_qs)
    if not accounts_list:
        return {}

    parents = [acc for acc in accounts_list if not _is_leaf_account(acc)]
    needed_ids = {acc.id for acc in accounts_list}

    # tree_id -> [(node_id, lft)] for every node of the trees that contain requested parent accounts
    tree_nodes = {}
    if parents:
        tree_ids = {acc.tree_id for acc in parents}
        for row in Account.objects.filter(tree_id__in=tree_ids).values('id', 'tree_id', 'lft'):
            tree_nodes.setdefault(row['tree_id'], []).append((row['id'], row['lft']))
            needed_ids.add(row['id'])

    raw_sums = _own_sums(needed_ids)
    zero = (Decimal('0.00'), Decimal('0.00'))

    balances_map = {}
    for acc in accounts_list:
        if _is_leaf_account(acc):
            total_d, total_c = raw_sums.get(acc.id, zero)
        else:
            total_d = Decimal('0.00')
            total_c = Decimal('0.00')
            for node_id, lft in tree_nodes.get(acc.tree_id, ()):
                if acc.lft <= lft < acc.rght and node_id in raw_sums:
                    d, c = raw_sums[node_id]
                    total_d += d
                    total_c += c
        bal = _signed_balance(acc, total_d, total_c)
        acc._prefetched_balance = bal
        balances_map[acc.id] = bal

    return balances_map


def batch_get_own_balances(account_qs=None):
    """
    أرصدة الحسابات من القيود المسجلة عليها **مباشرة** فقط (بدون أبنائها) في استعلام تجميعي واحد.
    مخصصة للتقارير (الميزانية / قائمة الدخل / ميزان المراجعة): مجموع الأرصدة المباشرة لكل الحسابات
    لا يكرر أي مبلغ، ويُظهر المبالغ المرحلة مباشرة على الحسابات الرئيسية (مثل 1110 / 1120 / 1130 / 1160)
    والتي كانت تختفي عند الاعتماد على الحسابات الطرفية فقط.

    يحقن في كل حساب:
      - own_debit / own_credit : إجمالي المدين والدائن المباشر
      - own_balance            : الرصيد الموقّع حسب طبيعة الحساب
      - is_leaf                : هل الحساب طرفي (بدون أبناء) — بدون استعلام إضافي
    ويعيد {account_id: own_balance}.
    """
    from apps.tenant.accounting.models import Account

    if account_qs is None:
        account_qs = Account.objects.filter(is_active=True)
    accounts_list = list(account_qs)
    raw_sums = _own_sums({acc.id for acc in accounts_list})
    zero = (Decimal('0.00'), Decimal('0.00'))
    result = {}
    for acc in accounts_list:
        total_d, total_c = raw_sums.get(acc.id, zero)
        acc.own_debit = total_d
        acc.own_credit = total_c
        acc.own_balance = _signed_balance(acc, total_d, total_c)
        acc.is_leaf = _is_leaf_account(acc)
        result[acc.id] = acc.own_balance
    return result
