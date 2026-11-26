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
  1190 - ضرائب خصم وإضافة مدينة (WHT مخصومة بمعرفة العملاء) — تُنشأ تلقائياً عند الحاجة
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

# ضريبة الخصم من المنبع لنا (مدينة) — المخصومة من مبيعاتنا بمعرفة العملاء.
# 1150 في الشجرة الافتراضية هو "عهد نقدية ومصروفات مقدمة"، لذلك نستخدم كوداً مستقلاً يُنشأ عند الحاجة.
WHT_RECEIVABLE_CODE = '1155'
# ضريبة القيمة المضافة - مدخلات (مشتريات ومصروفات) — أصل. المخرجات (مبيعات) تبقى على 2130.
INPUT_VAT_CODE = '1190'
OUTPUT_VAT_CODE = '2130'
# عمولات ماكينات الدفع والمحافظ والبنوك
PAYMENT_FEES_CODE = '5305'
# فروق تقريب النقدية في نقطة البيع (Invoice.rounding_amount): دائن = مكسب، مدين = خسارة
POS_ROUNDING_CODE = '5115'

# Lazily created system accounts: code -> (name, account_type, parent codes in preference order, extra)
_LAZY_ACCOUNTS = {
    WHT_RECEIVABLE_CODE: ('ضريبة خصم من المنبع لنا (مدينة)', 'ASSET', ('1100', '1000'), {'allow_reconcile': True}),
    INPUT_VAT_CODE: ('ضريبة القيمة المضافة - مدخلات (مشتريات)', 'ASSET', ('1100', '1000'), {}),
    PAYMENT_FEES_CODE: ('عمولات ماكينات الدفع والمحافظ والبنوك', 'EXPENSE', ('5300', '5000'), {'allow_direct_payment': False}),
    POS_ROUNDING_CODE: ('فروق تقريب نقطة البيع', 'EXPENSE', ('5000',), {'allow_direct_payment': False}),
}


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

    # Auto-seed standard chart of accounts if missing
    try:
        from apps.tenant.core.management.commands.setup_chart_of_accounts import setup_chart_of_accounts_data
        setup_chart_of_accounts_data()
        acc = Account.objects.filter(code=code, is_active=True).first()
        if acc:
            return acc
    except Exception as e:
        logger.warning(f"Error during setup_chart_of_accounts_data in _get_system_account: {e}")

    # Fallback auto-creation for specific dynamic/special accounts
    if code in _LAZY_ACCOUNTS:
        name, acc_type, parent_codes, extra = _LAZY_ACCOUNTS[code]
        existing = Account.objects.filter(code=code).first()
        if existing:  # exists but inactive -> reactivate instead of failing on the unique code
            existing.is_active = True
            existing.save(update_fields=['is_active'])
            return existing
        parent = None
        for pc in parent_codes:
            parent = Account.objects.filter(code=pc).first()
            if parent:
                break
        acc, _ = Account.objects.get_or_create(
            code=code,
            defaults=dict(name=name, account_type=acc_type, parent=parent, is_active=True, **extra),
        )
        return acc
    elif code == '1150':
        curr_assets = Account.objects.filter(code='1100').first()
        if curr_assets:
            return Account.objects.create(
                code='1150', name='عهد نقدية ومصروفات مقدمة',
                account_type=Account.ASSET, parent=curr_assets, allow_reconcile=True
            )
    elif code == '1110':
        curr_assets = Account.objects.filter(code='1100').first() or Account.objects.filter(code='1000').first()
        return Account.objects.create(
            code='1110', name='النقدية والخزينة',
            account_type=Account.ASSET, parent=curr_assets, is_active=True, allow_reconcile=True
        )
    elif code == '1120':
        curr_assets = Account.objects.filter(code='1100').first() or Account.objects.filter(code='1000').first()
        return Account.objects.create(
            code='1120', name='البنوك',
            account_type=Account.ASSET, parent=curr_assets, is_active=True, allow_reconcile=True
        )
    elif code == '1160':
        curr_assets = Account.objects.filter(code='1100').first() or Account.objects.filter(code='1000').first()
        return Account.objects.create(
            code='1160', name='المحافظ الإلكترونية',
            account_type=Account.ASSET, parent=curr_assets, is_active=True, allow_reconcile=True
        )
    elif code == '4999':
        rev_parent = Account.objects.filter(code='4200').first() or Account.objects.filter(code='4000').first()
        return Account.objects.create(
            code='4999', name='أرباح وإيرادات فروق جرد وتسويات بالزيادة',
            account_type=Account.REVENUE, parent=rev_parent, is_active=True
        )
    elif code == '5250':
        exp_parent = Account.objects.filter(code='5200').first() or Account.objects.filter(code='5000').first()
        return Account.objects.create(
            code='5250', name='مصاريف تالف وهالك وعجز مخزون',
            account_type=Account.EXPENSE, parent=exp_parent, is_active=True
        )
    elif code == '3999':
        eq_parent = Account.objects.filter(code='3000').first()
        return Account.objects.create(
            code='3999', name='أرصدة افتتاحية وتسويات رأس المال',
            account_type=Account.EQUITY, parent=eq_parent, is_active=True
        )
    elif code == '1140':
        curr_assets = Account.objects.filter(code='1100').first() or Account.objects.filter(code='1000').first()
        return Account.objects.create(
            code='1140', name='المخزون',
            account_type=Account.ASSET, parent=curr_assets, is_active=True
        )
    elif code == '3100':
        eq_parent = Account.objects.filter(code='3000').first()
        return Account.objects.create(
            code='3100', name='رأس المال',
            account_type=Account.EQUITY, parent=eq_parent, is_active=True
        )
    elif code == '1210':
        fix_parent = Account.objects.filter(code='1200').first() or Account.objects.filter(code='1000').first()
        return Account.objects.create(
            code='1210', name='الأصول الثابتة والآلات',
            account_type=Account.ASSET, parent=fix_parent, is_active=True
        )
    elif code == '1220':
        fix_parent = Account.objects.filter(code='1200').first() or Account.objects.filter(code='1000').first()
        return Account.objects.create(
            code='1220', name='مجمع الإهلاك',
            account_type=Account.ASSET, parent=fix_parent, is_active=True, is_contra=True
        )
    elif code == '5220':
        exp_parent = Account.objects.filter(code='5200').first() or Account.objects.filter(code='5000').first()
        return Account.objects.create(
            code='5220', name='مصروف الإهلاك',
            account_type=Account.EXPENSE, parent=exp_parent, is_active=True, allow_direct_payment=False
        )
    elif code == '4230':
        rev_parent = Account.objects.filter(code='4200').first() or Account.objects.filter(code='4000').first()
        return Account.objects.create(
            code='4230', name='أرباح بيع واستبعاد أصول ثابتة',
            account_type=Account.REVENUE, parent=rev_parent, is_active=True
        )
    elif code == '5370':
        admin_exp = Account.objects.filter(code='5300').first() or Account.objects.filter(code='5000').first()
        return Account.objects.create(
            code='5370', name='خسائر بيع واستبعاد أصول ثابتة',
            account_type=Account.EXPENSE, parent=admin_exp, is_active=True, allow_direct_payment=False
        )
    elif code == '2210':
        long_liab = Account.objects.filter(code='2200').first() or Account.objects.filter(code='2000').first()
        return Account.objects.create(
            code='2210', name='قروض وتسهيلات بنكية طويلة الأجل',
            account_type=Account.LIABILITY, parent=long_liab, is_active=True
        )
    elif code == '2180':
        curr_liab = Account.objects.filter(code='2100').first() or Account.objects.filter(code='2000').first()
        return Account.objects.create(
            code='2180', name='أقساط قروض قصيرة الأجل ومستحقة',
            account_type=Account.LIABILITY, parent=curr_liab, is_active=True, allow_reconcile=True
        )
    elif code == '5500':
        exp_parent = Account.objects.filter(code='5000').first()
        return Account.objects.create(
            code='5500', name='مصاريف فوائد وتمويل بنكي',
            account_type=Account.EXPENSE, parent=exp_parent, is_active=True, allow_direct_payment=False
        )
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
    logger.warning(
        'No specific treasury/bank/e-wallet for %s; posting on parent account %s.',
        context or 'journal entry', parent_code,
    )
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


def _payment_money_account(payment):
    """
    Specific treasury / bank sub-account for a partners.Payment.
    Payment has no treasury/bank field today, so the account is resolved from (in order):
    an explicit treasury / bank_account attribute (if the model gains one), the branch's
    single treasury (CASH) / the single bank account or the system default (CARD/BANK).
    Falls back to the parent 1110 / 1120 with a warning when ambiguous.
    """
    from apps.tenant.accounting.models import BankAccount
    from apps.tenant.core.settings_helpers import get_pos_defaults
    branch = payment.branch if getattr(payment, 'branch_id', None) else None
    defaults = get_pos_defaults(branch)  # per-branch defaults, then global; never another branch's treasury
    ctx = f'payment {payment.reference}'
    if payment.method == 'CASH':
        default_tr = defaults['treasury']
        return _money_account(
            [
                getattr(payment, 'treasury', None),
                _branch_treasury(payment.branch if getattr(payment, 'branch_id', None) else None),
                default_tr,
            ],
            '1110', ctx,
        )
    return _money_account(
        [
            getattr(payment, 'bank_account', None),
            defaults['bank_account'],
            _single(BankAccount.objects.order_by('pk')),
        ],
        '1120', ctx,
    )


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
        diff = total_amount + rounding_amount - split_sum
        if diff != 0 and abs(diff) <= Decimal('0.05') and active_splits:
            active_splits[0][1] += diff

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
        service_revenue_account = _get_system_account('4110') or revenue_account
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
    if invoice.invoice_type == 'RETURN_SALE':
        reference = _next_reference('RSAL')
        entry.reference = reference
        entry.description = f'قيد استحقاق مرتجع مبيعات - فاتورة رقم {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}'
        entry.save(update_fields=['reference', 'description'])
        
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
    entry.post()
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
    if invoice.partner and invoice.partner.payable_account:
        credit_account = invoice.partner.payable_account
    else:
        credit_account = _get_system_account('2110')  # Default A/P

    # --- Totals ---
    subtotal = invoice.subtotal
    net_subtotal = subtotal
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
        diff = total_amount - split_sum
        if diff != 0 and abs(diff) <= Decimal('0.05') and active_splits:
            active_splits[0][1] += diff

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
    if invoice.invoice_type == 'RETURN_PURCHASE':
        reference = _next_reference('RPUR')
        entry.reference = reference
        entry.description = f'قيد استحقاق مرتجع مشتريات - فاتورة رقم {invoice.invoice_number} - {getattr(invoice.partner, "name", "")}'
        entry.save(update_fields=['reference', 'description'])
        
        # NOTE: In a perpetual inventory system, a purchase return correctly credits Inventory directly.
        # We do not use a separate "Purchase Returns (5110)" account for the inventory value.
        for item in items_to_create:
            item.debit, item.credit = item.credit, item.debit
            item.description = item.description.replace('مشتريات', 'مرتجع مشتريات').replace('استحقاق', 'عكس استحقاق')

    JournalItem.objects.bulk_create(items_to_create)
    entry.post()
    logger.info(f'Journal posted for purchase invoice {invoice.invoice_number}: {reference}')

    return entry


def _create_purchase_payment_entry(invoice):
    """Deprecated: Settlement accounts are integrated directly in post_purchase_invoice."""
    return None



@transaction.atomic
def post_payment(payment) -> 'JournalEntry':
    """
    Creates and posts a journal entry for a customer receipt or supplier payment.

    Customer Receipt (RECEIPT):
      Dr 1110-<id>/1120-<id> (Treasury/Bank) amount
        Cr 1130 (A/R - Customer)          amount

    Supplier Payment (PAYMENT):
      Dr 2110 (A/P - Supplier)            amount
        Cr 1110-<id>/1120-<id> (Treasury/Bank) amount

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

    # Specific treasury / bank sub-account (1110-<id> / 1120-<id>); parent only as a logged fallback
    cash_account = _payment_money_account(payment)

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
