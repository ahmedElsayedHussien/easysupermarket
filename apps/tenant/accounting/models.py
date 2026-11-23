"""
Accounting models - Chart of Accounts, Journal Entries, Journal Items.
Implements full double-entry bookkeeping with MPTT account hierarchy.

Rules enforced:
  - Every JournalEntry must have sum(debit) == sum(credit) before posting.
  - A JournalItem may not have both debit > 0 AND credit > 0 simultaneously.
  - Accounts use MPTT for nested tree structure (Assets → Current Assets → Cash).
"""
from django.db import models, transaction
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import Sum, Q
from mptt.models import MPTTModel, TreeForeignKey
from decimal import Decimal
import datetime
import logging

logger = logging.getLogger(__name__)


def _next_doc_number(namespace, model, field, prefix, width=5):
    """
    Race-safe "last number + 1" for document numbers.
    The counter key is namespaced per model so two models sharing a textual prefix
    (e.g. Voucher 'PAY-2026-' vs partners Payment 'PAY-2026-') never share a counter.
    Must be called inside the transaction.atomic() block that saves the document.
    """
    from apps.tenant.core.sequences import next_sequence, max_numeric_suffix
    num = next_sequence(
        f'{namespace}:{prefix}',
        seed=lambda: max_numeric_suffix(model.objects.all(), field, prefix),
    )
    return f'{prefix}{num:0{width}d}'


def _ensure_pos_session_open(pos_session, action_label='تعديل'):
    """Refuse changing posted money documents that belong to an already-closed cashier shift."""
    if pos_session is None:
        return
    from apps.tenant.invoicing.models import POSSession
    if pos_session.status == POSSession.CLOSED:
        raise ValidationError(
            f'لا يمكن {action_label} هذا المستند لأنه مرتبط بوردية كاشير مغلقة '
            f'({pos_session}). يرجى عمل سند/قيد تسوية عكسي بدلاً من ذلك.'
        )


# ---------------------------------------------------------------------------
# Account (Chart of Accounts) - MPTT tree
# ---------------------------------------------------------------------------

class Account(MPTTModel):
    """
    A node in the Chart of Accounts tree.
    Uses django-mptt for efficient tree traversal and rendering.

    Account types determine normal balance:
      ASSET / EXPENSE  → normal balance is DEBIT  (Dr increases the account)
      LIABILITY / EQUITY / REVENUE → normal balance is CREDIT
    """
    ASSET = 'ASSET'
    LIABILITY = 'LIABILITY'
    EQUITY = 'EQUITY'
    REVENUE = 'REVENUE'
    EXPENSE = 'EXPENSE'

    ACCOUNT_TYPES = [
        (ASSET, 'أصول'),
        (LIABILITY, 'خصوم'),
        (EQUITY, 'حقوق ملكية'),
        (REVENUE, 'إيرادات'),
        (EXPENSE, 'مصروفات'),
    ]

    code = models.CharField(max_length=20, unique=True, verbose_name='كود الحساب')
    name = models.CharField(max_length=200, verbose_name='اسم الحساب')
    account_type = models.CharField(
        max_length=20, choices=ACCOUNT_TYPES, verbose_name='نوع الحساب'
    )
    parent = TreeForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children',
        verbose_name='الحساب الأب'
    )
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    allow_reconcile = models.BooleanField(default=False, verbose_name='قابل للتسوية')
    is_cogs = models.BooleanField(
        default=False,
        verbose_name='حساب تكلفة بضاعة (COGS)',
        help_text='خصصه لحسابات تكلفة البضاعة المباعة حتى يظهر في قائمة الدخل ضمن بند تكلفة المبيعات'
    )
    is_contra_revenue = models.BooleanField(
        default=False,
        verbose_name='إيراد معاكس (مرتجعات/خصومات)',
        help_text='خصصه لحسابات مرتجعات المبيعات والخصومات حتى تُخصم من صافي المبيعات'
    )
    is_contra = models.BooleanField(
        default=False,
        verbose_name='حساب معاكس (Contra)',
        help_text='حساب رصيده الطبيعي عكس نوعه ويُعرض في القوائم كمبلغ موجب مخصوم '
                  '(مثل مجمع الإهلاك تحت الأصول، مردودات المشتريات من التكلفة، مردودات المبيعات من الإيرادات)'
    )
    allow_direct_payment = models.BooleanField(
        default=True,
        verbose_name='يظهر في سندات الصرف المباشرة',
        help_text='حدد هذا الخيار إذا كان الحساب يمثل مصروفاً قابلاً للصرف المباشر بالسندات (مثل الإيجار، المرافق، الأجور)'
    )
    description = models.TextField(blank=True, verbose_name='وصف')
    created_at = models.DateTimeField(auto_now_add=True)

    class MPTTMeta:
        order_insertion_by = ['code']

    class Meta:
        verbose_name = 'حساب'
        verbose_name_plural = 'الحسابات'

    def __str__(self):
        return f'{self.code} - {self.name}'

    # Contra accounts of the default chart (accumulated depreciation, purchase returns,
    # sales returns / discounts): flagged automatically when created.
    DEFAULT_CONTRA_CODES = frozenset({'1220', '5110', '4120', '4130'})

    def save(self, *args, **kwargs):
        if self._state.adding and self.code in self.DEFAULT_CONTRA_CODES:
            self.is_contra = True
        # A contra-revenue account is by definition a contra account (keeps both flags consistent).
        if self.is_contra_revenue and not self.is_contra:
            self.is_contra = True
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'is_contra'})
        super().save(*args, **kwargs)

    @property
    def normal_balance(self):
        """
        Returns 'DEBIT' or 'CREDIT' based on account type.
        ASSET and EXPENSE accounts have a natural debit balance.
        LIABILITY, EQUITY, and REVENUE accounts have a natural credit balance.
        """
        if self.account_type in (self.ASSET, self.EXPENSE):
            return 'DEBIT'
        return 'CREDIT'

    @property
    def current_balance(self):
        """
        Calculates the running balance for this account from all POSTED entries.
        For parent accounts, it aggregates the balances of all its descendants as well.
        Returns a signed Decimal: positive means normal-side balance.
        Uses _prefetched_balance when available to eliminate N+1 queries.
        """
        if hasattr(self, '_prefetched_balance'):
            return self._prefetched_balance

        items = JournalItem.objects.filter(
            account__in=self.get_descendants(include_self=True),
            entry__status=JournalEntry.POSTED
        )
        total_debit = items.aggregate(s=Sum('debit'))['s'] or Decimal('0')
        total_credit = items.aggregate(s=Sum('credit'))['s'] or Decimal('0')
        if self.normal_balance == 'DEBIT':
            return (total_debit - total_credit).quantize(Decimal('0.01'))
        return (total_credit - total_debit).quantize(Decimal('0.01'))

    @property
    def has_children(self):
        return self.children.exists()

    def get_balance_display(self):
        """Return formatted balance string for display."""
        balance = self.current_balance
        return f'{balance:,.4f}'


# ---------------------------------------------------------------------------
# Journal Entry (the container for a balanced set of debits/credits)
# ---------------------------------------------------------------------------

class JournalEntry(models.Model):
    """
    A double-entry bookkeeping record containing two or more JournalItems.
    The entry is only valid when sum(debit) == sum(credit) across all items.

    Lifecycle: DRAFT → POSTED (irreversible in normal flow).
    Source document is linked via Generic FK for full traceability.
    """
    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحّل'),
    ]

    date = models.DateField(verbose_name='التاريخ')
    reference = models.CharField(
        max_length=50, unique=True, verbose_name='رقم القيد'
    )
    description = models.TextField(verbose_name='البيان')
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة'
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='journal_entries',
        verbose_name='أنشئ بواسطة'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Generic FK to the source document (Invoice, Payment, etc.)
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='نوع المستند المصدر'
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    source_document = GenericForeignKey('content_type', 'object_id')

    # Audit trail: a posted entry is never deleted when its document is cancelled/edited;
    # a mirror entry (debit <-> credit) is posted instead and points to the original here.
    # The original stays POSTED (history visible) and the pair nets to zero in every report.
    reversal_of = models.OneToOneField(
        'self',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='reversed_by',
        verbose_name='قيد عكسي لـ'
    )

    class Meta:
        verbose_name = 'قيد يومية'
        verbose_name_plural = 'قيود اليومية'
        ordering = ['-date', '-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            year = self.date.year if self.date else datetime.date.today().year
            prefix = f"JE-MANUAL-{year}-"
            with transaction.atomic():
                self.reference = _next_doc_number('journal_entry', JournalEntry, 'reference', prefix)
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.reference} - {self.date} [{self.get_status_display()}]'

    @property
    def is_reversal(self):
        return self.reversal_of_id is not None

    @property
    def reversing_entry(self):
        """The entry that reversed this one, or None."""
        try:
            return self.reversed_by
        except JournalEntry.DoesNotExist:
            return None

    @property
    def is_reversed(self):
        return self.reversing_entry is not None

    def get_total_debit(self):
        """Sum of all debit amounts across journal items."""
        return self.items.aggregate(s=Sum('debit'))['s'] or Decimal('0')

    def get_total_credit(self):
        """Sum of all credit amounts across journal items."""
        return self.items.aggregate(s=Sum('credit'))['s'] or Decimal('0')

    def is_balanced(self):
        """Returns True if total debits equal total credits (within rounding tolerance)."""
        return abs(self.get_total_debit() - self.get_total_credit()) < Decimal('0.0001')

    def clean(self):
        """Enforce balance constraint when posting."""
        if self.status == self.POSTED:
            if not self.is_balanced():
                debit = self.get_total_debit()
                credit = self.get_total_credit()
                diff = debit - credit
                raise ValidationError(
                    f'القيد غير متوازن: مجموع المدين ({debit:.4f}) لا يساوي مجموع الدائن ({credit:.4f}).'
                    f' الفرق: {diff:.4f}'
                )

    def post(self):
        """
        Post the journal entry after validating balance.
        Raises ValidationError if the entry is not balanced.
        """
        if not self.is_balanced():
            debit = self.get_total_debit()
            credit = self.get_total_credit()
            diff = debit - credit
            raise ValidationError(
                f'القيد غير متوازن. إجمالي المدين: {debit:.4f}, إجمالي الدائن: {credit:.4f}. الفرق: {diff:.4f}'
            )
        self.status = self.POSTED
        self.save(update_fields=['status', 'updated_at'])
        return self


# ---------------------------------------------------------------------------
# Journal Item (a single line within a JournalEntry)
# ---------------------------------------------------------------------------

class JournalItem(models.Model):
    """
    One line within a JournalEntry. Either debit OR credit must be non-zero,
    never both simultaneously. This is enforced by a DB check constraint.
    """
    entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='القيد'
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name='journal_items',
        verbose_name='الحساب'
    )
    debit = models.DecimalField(
        max_digits=15,
        decimal_places=4,
        default=Decimal('0'),
        verbose_name='مدين'
    )
    credit = models.DecimalField(
        max_digits=15,
        decimal_places=4,
        default=Decimal('0'),
        verbose_name='دائن'
    )
    description = models.CharField(
        max_length=500, blank=True, verbose_name='البيان'
    )

    class Meta:
        verbose_name = 'بند قيد'
        verbose_name_plural = 'بنود القيد'
        constraints = [
            models.CheckConstraint(
                check=~Q(debit__gt=0, credit__gt=0),
                name='accounting_not_both_debit_and_credit'
            ),
            models.CheckConstraint(
                check=Q(debit__gte=0),
                name='accounting_debit_non_negative'
            ),
            models.CheckConstraint(
                check=Q(credit__gte=0),
                name='accounting_credit_non_negative'
            ),
        ]

    def __str__(self):
        if self.debit > 0:
            return f'{self.account} | مدين: {self.debit:.4f}'
        return f'{self.account} | دائن: {self.credit:.4f}'

    def clean(self):
        """Ensure a journal item has either debit or credit, not both."""
        if self.debit > 0 and self.credit > 0:
            raise ValidationError('لا يمكن أن يكون للبند قيمة مدينة ودائنة في نفس الوقت.')
        if self.debit < 0 or self.credit < 0:
            raise ValidationError('لا يمكن أن تكون القيم سالبة.')


class PaymentMethod(models.Model):
    name = models.CharField(max_length=100, verbose_name='طريقة الدفع')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name='حساب الدفع')
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    
    class Meta:
        verbose_name = 'طريقة الدفع'
        verbose_name_plural = 'طرق الدفع'
        
    def __str__(self):
        return self.name


class Treasury(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم الخزينة')
    branch = models.ForeignKey('core.Branch', on_delete=models.PROTECT, verbose_name='الفرع')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الخزينة')

    class Meta:
        verbose_name = 'خزينة'
        verbose_name_plural = 'الخزائن'

    def __str__(self):
        return self.name

    @property
    def current_balance(self):
        if self.account:
            return self.account.current_balance
        return Decimal('0.00')

    def ensure_account(self):
        """Ensures this Treasury is linked to a valid ledger account, auto-creating it if missing."""
        if self.account_id and self.account and getattr(self.account, 'code', None) not in ['1100', '1110', '1120']:
            return self.account
        from apps.tenant.services.journal_service import _get_system_account
        try:
            with transaction.atomic():
                parent_code = '1120' if 'بنك' in self.name else '1110'
                parent_acc = _get_system_account(parent_code)
                if parent_acc:
                    acc, _ = Account.objects.get_or_create(
                        code=f"{parent_acc.code}-{self.id}",
                        defaults={
                            'name': self.name,
                            'account_type': parent_acc.account_type,
                            'parent': parent_acc,
                            'allow_reconcile': True
                        }
                    )
                    if self.account_id != acc.id:
                        self.account = acc
                        Treasury.objects.filter(id=self.id).update(account=acc)
                    return acc
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error ensuring treasury account: {e}")
        return self.account

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.account_id:
            self.ensure_account()


from django.db.models.signals import post_save
from django.dispatch import receiver

class BankAccount(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم الحساب البنكي')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الدليل')

    class Meta:
        verbose_name = 'حساب بنكي'
        verbose_name_plural = 'حسابات البنوك'

    def __str__(self):
        return self.name

    @property
    def current_balance(self):
        if self.account:
            return self.account.current_balance
        return Decimal('0.00')

    def ensure_account(self):
        """Ensures this BankAccount is linked to a valid ledger account, auto-creating it if missing."""
        if self.account_id and self.account and getattr(self.account, 'code', None) not in ['1100', '1120']:
            return self.account
        from apps.tenant.services.journal_service import _get_system_account
        try:
            with transaction.atomic():
                parent_acc = _get_system_account('1120')
                if parent_acc:
                    acc, _ = Account.objects.get_or_create(
                        code=f"{parent_acc.code}-{self.id}",
                        defaults={
                            'name': self.name,
                            'account_type': parent_acc.account_type,
                            'parent': parent_acc,
                            'allow_reconcile': True
                        }
                    )
                    if self.account_id != acc.id:
                        self.account = acc
                        BankAccount.objects.filter(id=self.id).update(account=acc)
                    return acc
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error ensuring bank account: {e}")
        return self.account

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.account_id:
            self.ensure_account()

class EWallet(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم المحفظة')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الدليل')

    class Meta:
        verbose_name = 'محفظة إلكترونية'
        verbose_name_plural = 'المحافظ الإلكترونية'

    def __str__(self):
        return self.name

    @property
    def current_balance(self):
        if self.account:
            return self.account.current_balance
        return Decimal('0.00')

    def ensure_account(self):
        """Ensures this EWallet is linked to a valid ledger account, auto-creating it if missing."""
        if self.account_id and self.account and getattr(self.account, 'code', None) not in ['1100', '1160']:
            return self.account
        from apps.tenant.services.journal_service import _get_system_account
        try:
            with transaction.atomic():
                parent_acc = _get_system_account('1160')
                if parent_acc:
                    acc, _ = Account.objects.get_or_create(
                        code=f"{parent_acc.code}-{self.id}",
                        defaults={
                            'name': self.name,
                            'account_type': parent_acc.account_type,
                            'parent': parent_acc,
                            'allow_reconcile': True
                        }
                    )
                    if self.account_id != acc.id:
                        self.account = acc
                        EWallet.objects.filter(id=self.id).update(account=acc)
                    return acc
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error ensuring ewallet account: {e}")
        return self.account

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.account_id:
            self.ensure_account()


OPENING_REFERENCE_PREFIXES = {
    'Treasury': 'OPENING-TR',
    'BankAccount': 'OPENING-BK',
    'EWallet': 'OPENING-EW',
    'POSMachine': 'OPENING-PM',
}


def book_opening_balance(instance, ref_prefix=None):
    """
    Posts the opening-balance entry of a Treasury / BankAccount / EWallet / POSMachine:
        Dr  <instance ledger account>   opening_balance
        Cr  3100 رأس المال              opening_balance

    Atomic and idempotent (keyed by reference '<PREFIX>-<pk>'):
      - a POSTED entry that already has items is returned untouched;
      - an empty / DRAFT header left behind by the old broken signal is repaired in place.
    Returns the posted JournalEntry, or None when there is nothing to book.
    """
    from apps.tenant.services.journal_service import _get_system_account

    amount = Decimal(str(instance.opening_balance or 0))
    if amount <= 0 or not instance.pk:
        return None
    account = instance.account
    if account is None:
        return None
    ref_prefix = ref_prefix or OPENING_REFERENCE_PREFIXES[type(instance).__name__]
    ref = f"{ref_prefix}-{instance.pk}"

    with transaction.atomic():
        entry = JournalEntry.objects.select_for_update().filter(reference=ref).first()
        if entry is not None and entry.status == JournalEntry.POSTED and entry.items.exists():
            return entry

        capital_acc = _get_system_account('3100')
        if entry is None:
            entry = JournalEntry.objects.create(
                date=datetime.date.today(),
                reference=ref,
                description=f"رصيد افتتاحي - {instance.name}",
                status=JournalEntry.DRAFT,
            )
        else:
            entry.items.all().delete()
            if entry.status != JournalEntry.DRAFT:
                entry.status = JournalEntry.DRAFT
                entry.save(update_fields=['status', 'updated_at'])

        entry.content_type = ContentType.objects.get_for_model(instance)
        entry.object_id = instance.pk
        entry.save(update_fields=['content_type', 'object_id'])

        JournalItem.objects.create(entry=entry, account=account, debit=amount, credit=0,
                                   description=f"رصيد افتتاحي - {instance.name}")
        JournalItem.objects.create(entry=entry, account=capital_acc, debit=0, credit=amount,
                                   description=f"رصيد افتتاحي - {instance.name}")
        entry.post()
        return entry


def _book_opening_balance_safely(instance):
    """Called from post_save: never breaks the save of the treasury/bank/wallet itself."""
    try:
        book_opening_balance(instance)
    except Exception:
        logger.exception(
            "Error booking opening balance for %s #%s (run 'manage.py repair_opening_balances' to retry)",
            type(instance).__name__, instance.pk,
        )


@receiver(post_save, sender=Treasury)
def create_treasury_account(sender, instance, created, **kwargs):
    updated = False
    has_valid_account = instance.account and getattr(instance.account, 'code', None) not in ['1100', '1110', '1120']
    
    if not has_valid_account:
        from apps.tenant.services.journal_service import _get_system_account
        try:
            parent_code = '1120' if 'بنك' in instance.name else '1110'
            parent_acc = _get_system_account(parent_code)
            
            acc, _ = Account.objects.get_or_create(
                code=f"{parent_acc.code}-{instance.id}",
                defaults={
                    'name': instance.name,
                    'account_type': parent_acc.account_type,
                    'parent': parent_acc,
                    'allow_reconcile': True
                }
            )
            instance.account = acc
            updated = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error creating treasury account: {str(e)}")
            pass
            
    if updated and instance.account:
        Treasury.objects.filter(id=instance.id).update(account=instance.account)

    if created and instance.opening_balance > 0 and instance.account:
        _book_opening_balance_safely(instance)

@receiver(post_save, sender=BankAccount)
def create_bank_account(sender, instance, created, **kwargs):
    updated = False
    has_valid_account = instance.account and getattr(instance.account, 'code', None) not in ['1100', '1120']
    
    if not has_valid_account:
        from apps.tenant.services.journal_service import _get_system_account
        try:
            parent_acc = _get_system_account('1120')
            acc, _ = Account.objects.get_or_create(
                code=f"{parent_acc.code}-{instance.id}",
                defaults={
                    'name': instance.name,
                    'account_type': parent_acc.account_type,
                    'parent': parent_acc,
                    'allow_reconcile': True
                }
            )
            instance.account = acc
            updated = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error creating bank account: {str(e)}")
            pass
            
    if updated and instance.account:
        BankAccount.objects.filter(id=instance.id).update(account=instance.account)

    if created and instance.opening_balance > 0 and instance.account:
        _book_opening_balance_safely(instance)

@receiver(post_save, sender=EWallet)
def create_ewallet_account(sender, instance, created, **kwargs):
    updated = False
    has_valid_account = instance.account and getattr(instance.account, 'code', None) not in ['1100', '1160']
    
    if not has_valid_account:
        from apps.tenant.services.journal_service import _get_system_account
        try:
            parent_acc = _get_system_account('1160')
            acc, _ = Account.objects.get_or_create(
                code=f"{parent_acc.code}-{instance.id}",
                defaults={
                    'name': instance.name,
                    'account_type': parent_acc.account_type,
                    'parent': parent_acc,
                    'allow_reconcile': True
                }
            )
            instance.account = acc
            updated = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error creating ewallet account: {str(e)}")
            pass
            
    if updated and instance.account:
        EWallet.objects.filter(id=instance.id).update(account=instance.account)

    if created and instance.opening_balance > 0 and instance.account:
        _book_opening_balance_safely(instance)

# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------

class Expense(models.Model):
    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    CANCELLED = 'CANCELLED'

    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحل'),
        (CANCELLED, 'ملغى'),
    ]

    expense_number = models.CharField(max_length=30, unique=True, blank=True, verbose_name='رقم المصروف')
    date = models.DateField(default=datetime.date.today, verbose_name='تاريخ المصروف')
    branch = models.ForeignKey('core.Branch', on_delete=models.PROTECT, verbose_name='الفرع')
    
    expense_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='expenses_debited', limit_choices_to={'account_type': Account.EXPENSE}, verbose_name='حساب المصروف')
    payment_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='expenses_credited', limit_choices_to={'account_type': Account.ASSET}, verbose_name='حساب الدفع (الخزينة/البنك)')
    
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='المبلغ (قبل الضريبة)')
    vat_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name='نسبة القيمة المضافة (%)')
    vat_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='قيمة الضريبة المضافة')
    
    withholding_tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name='نسبة الخصم والإضافة (%)')
    withholding_tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='قيمة الخصم والإضافة')
    
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='الإجمالي الصافي')
    
    description = models.TextField(blank=True, verbose_name='البيان / ملاحظات')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة')
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='أُنشئ بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name='expense_doc', verbose_name='قيد اليومية')

    class Meta:
        verbose_name = 'مصروف'
        verbose_name_plural = 'المصروفات'
        ordering = ['-date', '-id']

    def __str__(self):
        return f"{self.expense_number} - {self.description[:30]}"

    def save(self, *args, **kwargs):
        # Calculate amounts
        self.vat_amount = (self.amount * (self.vat_percent / Decimal('100.0'))).quantize(Decimal('0.01'))
        self.withholding_tax_amount = (self.amount * (self.withholding_tax_percent / Decimal('100.0'))).quantize(Decimal('0.01'))
        self.total_amount = self.amount + self.vat_amount - self.withholding_tax_amount

        if not self.expense_number:
            year = datetime.date.today().year
            with transaction.atomic():
                self.expense_number = _next_doc_number('expense', Expense, 'expense_number', f"EXP-{year}-")
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def confirm_expense(self):
        """Generates the accounting journal entry and marks as POSTED."""
        from django.db import transaction
        
        with transaction.atomic():
            # Lock the row and re-check status so a double click / concurrent request can't post twice
            current_status = Expense.objects.select_for_update().filter(pk=self.pk).values_list('status', flat=True).first()
            if current_status is not None:
                self.status = current_status
            if self.status != self.DRAFT:
                raise ValidationError("لا يمكن ترحيل إلا مسودة المصروف.")
            if (self.amount or Decimal('0')) <= 0 or (self.total_amount or Decimal('0')) <= 0:
                raise ValidationError("لا يمكن ترحيل مصروف بمبلغ صفر أو سالب.")

            from apps.tenant.services.journal_service import _get_system_account, delete_orphan_entries, unique_reference

            # Clean up any orphaned journal entry from previous failed attempts (never a reversal pair)
            delete_orphan_entries(self.expense_number)

            entry = JournalEntry.objects.create(
                date=self.date,
                reference=unique_reference(self.expense_number),
                description=f"سداد مصروف: {self.description}",
                status=JournalEntry.POSTED,
                created_by=self.created_by
            )
            
            # Dr: Expense Account (Separate line per ExpenseItem with its specific description)
            items = list(self.items.all())
            if items:
                for item in items:
                    if item.amount > 0:
                        JournalItem.objects.create(
                            entry=entry,
                            account=self.expense_account,
                            debit=item.amount,
                            credit=0,
                            description=item.description or self.description or f"سداد مصروف: {self.expense_account.name}"
                        )
            else:
                JournalItem.objects.create(
                    entry=entry,
                    account=self.expense_account,
                    debit=self.amount,
                    credit=0,
                    description=self.description or f"سداد مصروف: {self.expense_account.name}"
                )
            
            # Dr: VAT Account
            if self.vat_amount > 0:
                vat_account = _get_system_account('1190')  # 1190 = Input VAT (مدخلات) — same as purchase invoices
                if vat_account:
                    JournalItem.objects.create(
                        entry=entry,
                        account=vat_account,
                        debit=self.vat_amount,
                        credit=0,
                        description=f"ضريبة قيمة مضافة - مصروف {self.expense_number}"
                    )
                    
            # Cr: Withholding Tax Payable (2120 - same account used by purchase invoices)
            if self.withholding_tax_amount > 0:
                wh_account = _get_system_account('2120')  # raises ValueError if missing
                JournalItem.objects.create(
                    entry=entry,
                    account=wh_account,
                    debit=0,
                    credit=self.withholding_tax_amount,
                    description=f"ضريبة خصم وإضافة - مصروف {self.expense_number}"
                )
                    
            # Cr: Payment Account (Cash/Bank/Wallet)
            JournalItem.objects.create(
                entry=entry,
                account=self.payment_account,
                debit=0,
                credit=self.total_amount,
                description=f"سداد مصروف {self.expense_account.name}"
            )
            
            self.status = self.POSTED
            self.journal_entry = entry
            self.save()

class ExpenseItem(models.Model):
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name='items', verbose_name='المصروف')
    description = models.CharField(max_length=255, verbose_name='بيان البند')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ')

    class Meta:
        verbose_name = 'بند المصروف'
        verbose_name_plural = 'بنود المصروفات'

    def __str__(self):
        return self.description

# ---------------------------------------------------------------------------
# Vouchers (Receipts & Payments)
# ---------------------------------------------------------------------------

class Voucher(models.Model):
    RECEIPT = 'RECEIPT'
    PAYMENT = 'PAYMENT'
    VOUCHER_TYPE_CHOICES = [
        (RECEIPT, 'سند قبض'),
        (PAYMENT, 'سند صرف')
    ]

    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مُرحل'),
        (CANCELLED, 'ملغي')
    ]

    CASH = 'CASH'
    CARD = 'CARD'
    EWALLET = 'EWALLET'
    BANK_TRANSFER = 'BANK_TRANSFER'
    SPLIT = 'SPLIT'
    PAYMENT_METHOD_CHOICES = [
        (CASH, 'نقدي (كاش)'),
        (CARD, 'بطاقة / فيزا'),
        (EWALLET, 'محفظة إلكترونية'),
        (BANK_TRANSFER, 'تحويل بنكي / إنستاباي'),
        (SPLIT, 'سداد مقسم / متنوع'),
    ]

    voucher_type = models.CharField(max_length=15, choices=VOUCHER_TYPE_CHOICES, verbose_name='نوع السند')
    voucher_number = models.CharField(max_length=20, unique=True, editable=False, verbose_name='رقم السند')
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ')
    
    partner = models.ForeignKey(
        'partners.Partner', on_delete=models.PROTECT, null=True, blank=True,
        related_name='vouchers', verbose_name='الشريك (عميل/مورد)'
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='vouchers',
        verbose_name='الحساب المقابل'
    )
    
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default=CASH, verbose_name='طريقة الدفع')
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, verbose_name='الخزينة')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True, blank=True, verbose_name='الحساب البنكي / ماكينة الفيزا')
    ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, null=True, blank=True, verbose_name='المحفظة الإلكترونية')
    transfer_bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, null=True, blank=True,
        related_name='transfer_vouchers', verbose_name='حساب إنستاباي / التحويل البنكي'
    )

    # حقول تفاصيل السداد المقسم / المتنوع
    paid_cash = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='المبلغ النقدي')
    paid_card = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ البطاقة / فيزا')
    paid_ewallet = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ المحفظة الإلكترونية')
    paid_bank = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ التحويل البنكي / إنستاباي')

    pos_session = models.ForeignKey(
        'invoicing.POSSession', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='vouchers', verbose_name='الوردية المرتبطة'
    )
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='vouchers', verbose_name='الفرع'
    )

    description = models.TextField(verbose_name='البيان', blank=True, null=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة')
    
    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='voucher', verbose_name='قيد اليومية'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'سند'
        verbose_name_plural = 'السندات'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.get_voucher_type_display()} #{self.voucher_number} - {self.amount}"

    def save(self, *args, **kwargs):
        # مزامنة تفاصيل السداد المقسم مع طريقة الدفع
        if self.payment_method == self.CASH:
            self.paid_cash = self.amount or Decimal('0')
            self.paid_card = Decimal('0')
            self.paid_ewallet = Decimal('0')
            self.paid_bank = Decimal('0')
        elif self.payment_method == self.CARD:
            self.paid_card = self.amount or Decimal('0')
            self.paid_cash = Decimal('0')
            self.paid_ewallet = Decimal('0')
            self.paid_bank = Decimal('0')
        elif self.payment_method == self.EWALLET:
            self.paid_ewallet = self.amount or Decimal('0')
            self.paid_cash = Decimal('0')
            self.paid_card = Decimal('0')
            self.paid_bank = Decimal('0')
        elif self.payment_method == self.BANK_TRANSFER:
            self.paid_bank = self.amount or Decimal('0')
            self.paid_cash = Decimal('0')
            self.paid_card = Decimal('0')
            self.paid_ewallet = Decimal('0')
            if not self.bank_account and self.transfer_bank_account:
                self.bank_account = self.transfer_bank_account
            elif not self.transfer_bank_account and self.bank_account:
                self.transfer_bank_account = self.bank_account
        elif self.payment_method == self.SPLIT:
            split_sum = (self.paid_cash or 0) + (self.paid_card or 0) + (self.paid_ewallet or 0) + (self.paid_bank or 0)
            if split_sum == 0 and self.amount:
                self.paid_cash = self.amount

        # Branch attribution (used for branch-scoped visibility of vouchers)
        if not self.branch_id:
            if self.pos_session_id and self.pos_session and self.pos_session.branch_id:
                self.branch_id = self.pos_session.branch_id
            elif self.treasury_id and self.treasury and self.treasury.branch_id:
                self.branch_id = self.treasury.branch_id

        if not self.voucher_number:
            prefix = 'REC' if self.voucher_type == self.RECEIPT else 'PAY'
            year = datetime.date.today().year
            with transaction.atomic():
                self.voucher_number = _next_doc_number('voucher', Voucher, 'voucher_number', f"{prefix}-{year}-")
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @property
    def payment_account(self):
        if self.payment_method == self.CASH:
            if not self.treasury:
                from apps.tenant.core.models import SystemSetting
                sys_setting = SystemSetting.get_settings()
                if self.pos_session and self.pos_session.treasury:
                    self.treasury = self.pos_session.treasury
                elif sys_setting and sys_setting.default_pos_treasury:
                    self.treasury = sys_setting.default_pos_treasury
                elif Treasury.objects.exists():
                    self.treasury = Treasury.objects.first()
                if self.treasury and self.pk:
                    Voucher.objects.filter(pk=self.pk).update(treasury=self.treasury)

            if self.treasury:
                return self.treasury.ensure_account()
            return None

        elif self.payment_method in (self.BANK_TRANSFER, self.CARD):
            if not self.bank_account and self.transfer_bank_account and self.payment_method == self.BANK_TRANSFER:
                self.bank_account = self.transfer_bank_account

            if not self.bank_account:
                from apps.tenant.core.models import SystemSetting
                sys_setting = SystemSetting.get_settings()
                if self.payment_method == self.CARD and sys_setting and sys_setting.default_pos_bank_account:
                    self.bank_account = sys_setting.default_pos_bank_account
                elif self.payment_method == self.BANK_TRANSFER and sys_setting and sys_setting.default_pos_instapay_account:
                    self.bank_account = sys_setting.default_pos_instapay_account
                elif self.payment_method == self.BANK_TRANSFER and sys_setting and sys_setting.default_pos_bank_account:
                    self.bank_account = sys_setting.default_pos_bank_account
                elif BankAccount.objects.exists():
                    self.bank_account = BankAccount.objects.first()
                if self.bank_account and self.pk:
                    Voucher.objects.filter(pk=self.pk).update(bank_account=self.bank_account)

            if self.bank_account:
                return self.bank_account.ensure_account()
            return None

        elif self.payment_method == self.EWALLET:
            if not self.ewallet:
                from apps.tenant.core.models import SystemSetting
                sys_setting = SystemSetting.get_settings()
                if sys_setting and sys_setting.default_pos_ewallet:
                    self.ewallet = sys_setting.default_pos_ewallet
                elif EWallet.objects.exists():
                    self.ewallet = EWallet.objects.first()
                if self.ewallet and self.pk:
                    Voucher.objects.filter(pk=self.pk).update(ewallet=self.ewallet)

            if self.ewallet:
                return self.ewallet.ensure_account()
            return None

        elif self.payment_method == self.SPLIT:
            if self.treasury:
                acc = self.treasury.ensure_account()
                if acc: return acc
            if self.bank_account:
                acc = self.bank_account.ensure_account()
                if acc: return acc
            if self.ewallet:
                acc = self.ewallet.ensure_account()
                if acc: return acc
            trans_acc = self.transfer_bank_account or self.bank_account
            if trans_acc:
                acc = trans_acc.ensure_account()
                if acc: return acc
        return None

    def confirm_voucher(self):
        """Generates the accounting journal entry and marks as POSTED."""
        from django.db import transaction
        from django.core.exceptions import ValidationError
        
        with transaction.atomic():
            # Lock the row and re-check status so a voucher can never be posted twice concurrently
            current_status = Voucher.objects.select_for_update().filter(pk=self.pk).values_list('status', flat=True).first()
            if current_status is not None:
                self.status = current_status
            if self.status != self.DRAFT:
                raise ValidationError("لا يمكن ترحيل إلا مسودة السند.")
            if (self.amount or Decimal('0')) <= 0:
                raise ValidationError("مبلغ السند يجب أن يكون أكبر من صفر.")
            if any((v or Decimal('0')) < 0 for v in (self.paid_cash, self.paid_card, self.paid_ewallet, self.paid_bank)):
                raise ValidationError("مبالغ السداد لا يمكن أن تكون سالبة.")

            if self.payment_method == self.SPLIT:
                split_sum = (self.paid_cash or Decimal('0')) + (self.paid_card or Decimal('0')) + (self.paid_ewallet or Decimal('0')) + (self.paid_bank or Decimal('0'))
                if abs(split_sum - self.amount) > Decimal('0.05'):
                    raise ValidationError(f"إجمالي مبالغ السداد المقسم ({split_sum}) لا يساوي إجمالي مبلغ السند ({self.amount}).")

                if self.paid_cash > 0:
                    if not self.treasury:
                        from apps.tenant.core.models import SystemSetting
                        sys_setting = SystemSetting.get_settings()
                        self.treasury = (sys_setting.default_pos_treasury if sys_setting else None) or Treasury.objects.first()
                        if self.treasury and self.pk:
                            Voucher.objects.filter(pk=self.pk).update(treasury=self.treasury)
                    if not self.treasury or not self.treasury.ensure_account():
                        raise ValidationError("يجب تحديد الخزينة للجزء النقدي والتأكد من ربطها بحساب محاسبي.")

                if self.paid_card > 0:
                    if not self.bank_account:
                        from apps.tenant.core.models import SystemSetting
                        sys_setting = SystemSetting.get_settings()
                        self.bank_account = (sys_setting.default_pos_bank_account if sys_setting else None) or BankAccount.objects.first()
                        if self.bank_account and self.pk:
                            Voucher.objects.filter(pk=self.pk).update(bank_account=self.bank_account)
                    if not self.bank_account or not self.bank_account.ensure_account():
                        raise ValidationError("يجب تحديد ماكينة الفيزا / الحساب البنكي لجزء البطاقة.")

                if self.paid_ewallet > 0:
                    if not self.ewallet:
                        from apps.tenant.core.models import SystemSetting
                        sys_setting = SystemSetting.get_settings()
                        self.ewallet = (sys_setting.default_pos_ewallet if sys_setting else None) or EWallet.objects.first()
                        if self.ewallet and self.pk:
                            Voucher.objects.filter(pk=self.pk).update(ewallet=self.ewallet)
                    if not self.ewallet or not self.ewallet.ensure_account():
                        raise ValidationError("يجب تحديد المحفظة الإلكترونية لجزء المحفظة.")

                trans_acc = self.transfer_bank_account or self.bank_account
                if self.paid_bank > 0:
                    if not trans_acc:
                        from apps.tenant.core.models import SystemSetting
                        sys_setting = SystemSetting.get_settings()
                        trans_acc = (sys_setting.default_pos_instapay_account if sys_setting else None) or (sys_setting.default_pos_bank_account if sys_setting else None) or BankAccount.objects.first()
                        if trans_acc and self.pk:
                            Voucher.objects.filter(pk=self.pk).update(transfer_bank_account=trans_acc)
                    if not trans_acc or not trans_acc.ensure_account():
                        raise ValidationError("يجب تحديد حساب إنستاباي / التحويل البنكي لجزء التحويل.")

                # Clean up any orphaned journal entry (reversed entries of a previous posting are kept)
                from apps.tenant.services.journal_service import delete_orphan_entries, unique_reference
                delete_orphan_entries(self.voucher_number)

                entry = JournalEntry.objects.create(
                    date=self.date,
                    reference=unique_reference(self.voucher_number),
                    description=f"{self.get_voucher_type_display()} (سداد مقسم) - {self.description or ''}",
                    status=JournalEntry.POSTED
                )

                if self.voucher_type == self.RECEIPT:
                    # Receipt: Dr Payment Methods | Cr Account
                    if self.paid_cash > 0:
                        JournalItem.objects.create(entry=entry, account=self.treasury.account, debit=self.paid_cash, credit=0, description=f"{self.voucher_number} - نقدي")
                    if self.paid_card > 0:
                        JournalItem.objects.create(entry=entry, account=self.bank_account.account, debit=self.paid_card, credit=0, description=f"{self.voucher_number} - بطاقة / فيزا")
                    if self.paid_ewallet > 0:
                        JournalItem.objects.create(entry=entry, account=self.ewallet.account, debit=self.paid_ewallet, credit=0, description=f"{self.voucher_number} - محفظة إلكترونية")
                    if self.paid_bank > 0:
                        JournalItem.objects.create(entry=entry, account=trans_acc.account, debit=self.paid_bank, credit=0, description=f"{self.voucher_number} - إنستاباي / تحويل بنكي")
                    JournalItem.objects.create(entry=entry, account=self.account, debit=0, credit=self.amount)
                else:
                    # Payment: Dr Account | Cr Payment Methods
                    JournalItem.objects.create(entry=entry, account=self.account, debit=self.amount, credit=0)
                    if self.paid_cash > 0:
                        JournalItem.objects.create(entry=entry, account=self.treasury.account, debit=0, credit=self.paid_cash, description=f"{self.voucher_number} - نقدي")
                    if self.paid_card > 0:
                        JournalItem.objects.create(entry=entry, account=self.bank_account.account, debit=0, credit=self.paid_card, description=f"{self.voucher_number} - بطاقة / فيزا")
                    if self.paid_ewallet > 0:
                        JournalItem.objects.create(entry=entry, account=self.ewallet.account, debit=0, credit=self.paid_ewallet, description=f"{self.voucher_number} - محفظة إلكترونية")
                    if self.paid_bank > 0:
                        JournalItem.objects.create(entry=entry, account=trans_acc.account, debit=0, credit=self.paid_bank, description=f"{self.voucher_number} - إنستاباي / تحويل بنكي")

                entry.post()
                self.status = self.POSTED
                self.journal_entry = entry
                self.save()
            else:
                if not self.payment_account:
                    raise ValidationError("يجب تحديد حساب الدفع (الخزينة/البنك/المحفظة) المرتبط بطريقة الدفع.")
                    
                # Clean up any orphaned journal entry (reversed entries of a previous posting are kept)
                from apps.tenant.services.journal_service import delete_orphan_entries, unique_reference
                delete_orphan_entries(self.voucher_number)

                entry = JournalEntry.objects.create(
                    date=self.date,
                    reference=unique_reference(self.voucher_number),
                    description=f"{self.get_voucher_type_display()} - {self.description or ''}",
                    status=JournalEntry.POSTED
                )
                
                if self.voucher_type == self.RECEIPT:
                    # Receipt: Dr Treasury | Cr Account
                    JournalItem.objects.create(entry=entry, account=self.payment_account, debit=self.amount, credit=0)
                    JournalItem.objects.create(entry=entry, account=self.account, debit=0, credit=self.amount)
                else:
                    # Payment: Dr Account | Cr Treasury
                    JournalItem.objects.create(entry=entry, account=self.account, debit=self.amount, credit=0)
                    JournalItem.objects.create(entry=entry, account=self.payment_account, debit=0, credit=self.amount)
                
                entry.post()
                
                self.status = self.POSTED
                self.journal_entry = entry
                self.save()

    def cancel_voucher(self, user=None):
        """
        Cancels the voucher. Its posted journal entry is NOT deleted: a reversal entry
        (debit <-> credit) is posted and linked to it, so the audit trail is kept and the
        pair nets to zero. ``journal_entry`` is cleared (no active entry any more).
        Runs atomically on a locked row, and is refused when the voucher belongs to an
        already-closed cashier shift (the shift's cash count was reconciled against it).
        """
        from apps.tenant.services.journal_service import reverse_entry_if_active
        with transaction.atomic():
            locked = Voucher.objects.select_for_update().select_related('pos_session', 'journal_entry').get(pk=self.pk)
            if locked.status == self.CANCELLED:
                raise ValidationError("السند ملغي بالفعل.")
            if locked.status == self.POSTED:
                _ensure_pos_session_open(locked.pos_session, 'إلغاء')

            if locked.journal_entry_id:
                reverse_entry_if_active(
                    locked.journal_entry, reason=f'إلغاء السند {locked.voucher_number}', user=user
                )

            self.journal_entry = None
            self.status = self.CANCELLED
            self.save(update_fields=['status', 'journal_entry', 'updated_at'])


# ---------------------------------------------------------------------------
# POS Machine (ماكينات الدفع الإلكتروني - فوري، أمان، فيزا البنوك)
# ---------------------------------------------------------------------------

class POSMachine(models.Model):
    """
    Represents a physical payment machine (e.g., Fawry, Aman, Bank Visa terminal).
    Each machine has its own ledger account under parent code 1170.
    """
    FAWRY = 'FAWRY'
    AMAN = 'AMAN'
    BANK_VISA = 'BANK_VISA'
    INSTAPAY = 'INSTAPAY'
    OTHER = 'OTHER'

    MACHINE_TYPE_CHOICES = [
        (FAWRY, 'فوري'),
        (AMAN, 'أمان'),
        (BANK_VISA, 'ماكينة فيزا بنك'),
        (INSTAPAY, 'إنستاباي'),
        (OTHER, 'أخرى'),
    ]

    name = models.CharField(max_length=150, verbose_name='اسم الماكينة')
    machine_type = models.CharField(
        max_length=20, choices=MACHINE_TYPE_CHOICES, default=OTHER,
        verbose_name='نوع الماكينة'
    )
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.PROTECT,
        related_name='pos_machines', verbose_name='الفرع'
    )
    opening_balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
        verbose_name='رصيد أول المدة'
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name='pos_machines', verbose_name='حساب الدليل'
    )
    is_active = models.BooleanField(default=True, verbose_name='نشطة')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'ماكينة دفع إلكتروني'
        verbose_name_plural = 'ماكينات الدفع الإلكتروني'
        ordering = ['branch', 'name']

    def __str__(self):
        return f'{self.name} ({self.get_machine_type_display()})'

    @property
    def current_balance(self):
        if not self.account:
            return Decimal('0')
        return self.account.current_balance


@receiver(post_save, sender=POSMachine)
def create_pos_machine_account(sender, instance, created, **kwargs):
    """Auto-create a ledger account under 1170 for each POS machine."""
    updated = False
    has_valid_account = instance.account and getattr(instance.account, 'code', None) not in ['1100', '1170']

    if not has_valid_account:
        from apps.tenant.services.journal_service import _get_system_account
        try:
            parent_acc = _get_system_account('1170')
            if not parent_acc:
                asset_parent = Account.objects.filter(code='1100').first()
                parent_acc = Account.objects.create(
                    code='1170',
                    name='ماكينات الدفع الإلكتروني',
                    account_type=Account.ASSET,
                    parent=asset_parent
                )
            acc = Account.objects.create(
                code=f'1170-{instance.id}',
                name=f'{instance.name}',
                account_type=Account.ASSET,
                parent=parent_acc
            )
            instance.account = acc
            updated = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'Error creating POS machine account: {e}')

    if updated:
        instance.save(update_fields=['account'])

    if created and instance.opening_balance > 0 and instance.account:
        # Same pattern as treasuries/banks/wallets: atomic + idempotent (errors propagate as before)
        book_opening_balance(instance)


# ---------------------------------------------------------------------------
# E-Service Transaction (حركات مركز خدمات الدفع)
# ---------------------------------------------------------------------------

class EServiceTransaction(models.Model):
    """
    Records all e-service operations:
      - CASH_OUT: Customer withdraws cash via store machine.
      - CASH_IN:  Customer pays cash to transfer balance.
      - MERCHANT_RECHARGE: Store recharges machine from a vendor/agent.
    """
    CASH_OUT = 'CASH_OUT'
    CASH_IN = 'CASH_IN'
    MERCHANT_RECHARGE = 'MERCHANT_RECHARGE'

    TRANSACTION_TYPE_CHOICES = [
        (CASH_OUT, 'سحب كاش (عميل يسحب من محفظته)'),
        (CASH_IN, 'إيداع / تحويل (عميل يحول رصيد)'),
        (MERCHANT_RECHARGE, 'شحن الماكينة من مندوب'),
    ]

    SOURCE_MACHINE = 'MACHINE'
    SOURCE_EWALLET = 'EWALLET'
    SOURCE_TYPE_CHOICES = [
        (SOURCE_MACHINE, 'ماكينة دفع'),
        (SOURCE_EWALLET, 'محفظة إلكترونية'),
    ]

    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحل'),
    ]

    transaction_number = models.CharField(
        max_length=30, unique=True, blank=True, verbose_name='رقم العملية'
    )
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    transaction_type = models.CharField(
        max_length=20, choices=TRANSACTION_TYPE_CHOICES, verbose_name='نوع العملية'
    )

    source_type = models.CharField(
        max_length=10, choices=SOURCE_TYPE_CHOICES, default=SOURCE_MACHINE,
        verbose_name='مصدر الحركة'
    )
    pos_machine = models.ForeignKey(
        POSMachine, on_delete=models.PROTECT, null=True, blank=True,
        related_name='transactions', verbose_name='الماكينة'
    )
    ewallet = models.ForeignKey(
        EWallet, on_delete=models.PROTECT, null=True, blank=True,
        related_name='eservice_transactions', verbose_name='المحفظة الإلكترونية'
    )
    treasury = models.ForeignKey(
        Treasury, on_delete=models.PROTECT,
        related_name='eservice_transactions', verbose_name='الخزينة / الدرج'
    )

    principal_amount = models.DecimalField(
        max_digits=15, decimal_places=2, verbose_name='المبلغ الأصلي'
    )
    commission_revenue = models.DecimalField(
        max_digits=15, decimal_places=2, default=0, verbose_name='عمولة المحل (الربح)'
    )
    commission_expense = models.DecimalField(
        max_digits=15, decimal_places=2, default=0, verbose_name='رسوم المندوب (المصروف)'
    )

    description = models.TextField(blank=True, verbose_name='البيان')
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة'
    )

    invoice = models.OneToOneField(
        'invoicing.Invoice', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='eservice_transaction', verbose_name='فاتورة المبيعات'
    )
    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='eservice_transaction', verbose_name='قيد اليومية'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='بواسطة'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'حركة خدمات دفع'
        verbose_name_plural = 'حركات خدمات الدفع'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.transaction_number} - {self.get_transaction_type_display()} - {self.principal_amount}'

    def save(self, *args, **kwargs):
        if not self.transaction_number:
            year = datetime.date.today().year
            with transaction.atomic():
                self.transaction_number = _next_doc_number(
                    'eservice', EServiceTransaction, 'transaction_number', f'ESVC-{year}-'
                )
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def get_source_account(self):
        if self.source_type == self.SOURCE_MACHINE and self.pos_machine:
            return self.pos_machine.account
        elif self.source_type == self.SOURCE_EWALLET and self.ewallet:
            return self.ewallet.account
        return None

    def post_transaction(self):
        """Creates journal entries and service invoice, then marks as POSTED."""
        from django.db import transaction as db_transaction
        from django.core.exceptions import ValidationError
        from apps.tenant.services.journal_service import _get_system_account

        with db_transaction.atomic():
            current_status = EServiceTransaction.objects.select_for_update().filter(pk=self.pk).values_list('status', flat=True).first()
            if current_status is not None:
                self.status = current_status
            if self.status == self.POSTED:
                raise ValidationError('هذه العملية مرحلة بالفعل.')
            principal = self.principal_amount or Decimal('0')
            if principal <= 0:
                raise ValidationError('المبلغ الأصلي يجب أن يكون أكبر من صفر.')
            if (self.commission_revenue or 0) < 0 or (self.commission_expense or 0) < 0:
                raise ValidationError('العمولات لا يمكن أن تكون سالبة.')
            if self.transaction_type == self.CASH_OUT and (self.commission_revenue or 0) > principal:
                raise ValidationError('عمولة المحل لا يمكن أن تتجاوز المبلغ الأصلي في عملية السحب.')

            source_account = self.get_source_account()
            if not source_account:
                raise ValidationError('يجب تحديد ماكينة أو محفظة للعملية.')
            if not self.treasury or not self.treasury.account:
                raise ValidationError('يجب تحديد خزينة/درج صحيح.')

            treasury_account = self.treasury.account
            revenue_account = _get_system_account('4200') or _get_system_account('4100')
            expense_account = Cheque.get_bank_expense_account()  # 5305 - agent / machine / wallet fees

            from apps.tenant.services.journal_service import delete_orphan_entries, unique_reference
            delete_orphan_entries(self.transaction_number)

            entry = JournalEntry.objects.create(
                date=self.date,
                reference=unique_reference(self.transaction_number),
                description=f'{self.get_transaction_type_display()} - {self.description}'
            )

            if self.transaction_type == self.CASH_OUT:
                # Dr: Machine (gets customer's transfer) | Cr: Treasury (net cash out) + Cr: Revenue (commission)
                net_cash_out = self.principal_amount - self.commission_revenue
                JournalItem.objects.create(entry=entry, account=source_account,
                                           debit=self.principal_amount, credit=0,
                                           description='استلام تحويل من عميل')
                JournalItem.objects.create(entry=entry, account=treasury_account,
                                           debit=0, credit=net_cash_out,
                                           description='صرف كاش للعميل')
                if self.commission_revenue > 0 and revenue_account:
                    JournalItem.objects.create(entry=entry, account=revenue_account,
                                               debit=0, credit=self.commission_revenue,
                                               description='عمولة المحل')

            elif self.transaction_type == self.CASH_IN:
                # Dr: Treasury (gross cash) | Cr: Machine (sends transfer) + Cr: Revenue (commission)
                gross_cash_in = self.principal_amount + self.commission_revenue
                JournalItem.objects.create(entry=entry, account=treasury_account,
                                           debit=gross_cash_in, credit=0,
                                           description='استلام كاش من عميل')
                JournalItem.objects.create(entry=entry, account=source_account,
                                           debit=0, credit=self.principal_amount,
                                           description='تحويل رصيد للعميل')
                if self.commission_revenue > 0 and revenue_account:
                    JournalItem.objects.create(entry=entry, account=revenue_account,
                                               debit=0, credit=self.commission_revenue,
                                               description='عمولة المحل')

            elif self.transaction_type == self.MERCHANT_RECHARGE:
                # Dr: Machine + Dr: Expense (fees) | Cr: Treasury (total cash paid to agent)
                gross_cash_out = self.principal_amount + self.commission_expense
                JournalItem.objects.create(entry=entry, account=source_account,
                                           debit=self.principal_amount, credit=0,
                                           description='شحن رصيد الماكينة')
                if self.commission_expense > 0 and expense_account:
                    JournalItem.objects.create(entry=entry, account=expense_account,
                                               debit=self.commission_expense, credit=0,
                                               description='رسوم مندوب الشحن')
                JournalItem.objects.create(entry=entry, account=treasury_account,
                                           debit=0, credit=gross_cash_out,
                                           description='دفع كاش للمندوب')

            entry.post()

            # Create service invoice for revenue-generating transactions
            if self.transaction_type in [self.CASH_OUT, self.CASH_IN] and self.commission_revenue > 0:
                self._create_service_invoice()

            self.journal_entry = entry
            self.status = self.POSTED
            self.save()

    def _create_service_invoice(self):
        """
        Creates a POSTED sales invoice documenting the commission revenue.

        The revenue itself is already booked by this transaction's journal entry, so the
        invoice is a supporting document only and the caller (post_transaction) treats it
        as optional: when no partner/warehouse exists it is simply skipped. Any DB error is
        confined to its own savepoint (so on PostgreSQL the caller's transaction stays
        usable), logged with the traceback, and the transaction posting continues.
        """
        from apps.tenant.invoicing.models import Invoice
        from apps.tenant.partners.models import Partner
        import datetime as dt

        cash_partner = Partner.objects.filter(name='عميل نقدي').first() or Partner.objects.first()
        if not cash_partner:
            logger.warning('Service invoice skipped for %s: no partner available.', self.transaction_number)
            return None

        branch = self.treasury.branch
        warehouse = branch.warehouses.first()
        if not warehouse:
            logger.warning('Service invoice skipped for %s: branch %s has no warehouse.', self.transaction_number, branch)
            return None

        try:
            with transaction.atomic():
                year = dt.date.today().year
                inv_number = _next_doc_number('invoice', Invoice, 'invoice_number', f'SVC-{year}-')

                invoice = Invoice.objects.create(
                    invoice_number=inv_number,
                    invoice_type=Invoice.SALE,
                    partner=cash_partner,
                    branch=branch,
                    warehouse=warehouse,
                    date=self.date,
                    payment_type=Invoice.CASH,
                    treasury=self.treasury,
                    subtotal=self.commission_revenue,
                    discount_amount=Decimal('0'),
                    total_amount=self.commission_revenue,
                    status=Invoice.POSTED,
                    notes=f'عمولة خدمة: {self.get_transaction_type_display()} - {self.transaction_number}'
                )
                self.invoice = invoice
                self.save(update_fields=['invoice'])
                return invoice
        except Exception:
            logger.exception('Error creating service invoice for e-service transaction %s', self.transaction_number)
            return None


# ---------------------------------------------------------------------------
# Internal Transfer (تحويل الأموال بين الحسابات: خزن، بنوك، ومحافظ فقط)
# ---------------------------------------------------------------------------

class InternalTransfer(models.Model):
    TREASURY = 'TREASURY'
    BANK = 'BANK'
    EWALLET = 'EWALLET'

    ACCOUNT_TYPE_CHOICES = [
        (TREASURY, 'خزينة / درج'),
        (BANK, 'حساب بنكي'),
        (EWALLET, 'محفظة إلكترونية'),
    ]

    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    CANCELLED = 'CANCELLED'

    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مُرحل'),
        (CANCELLED, 'ملغي'),
    ]

    transfer_number = models.CharField(max_length=30, unique=True, editable=False, verbose_name='رقم سند التحويل')
    date = models.DateField(default=datetime.date.today, verbose_name='تاريخ التحويل')
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ المحول')

    # Source Account
    from_account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default=TREASURY, verbose_name='نوع الجهة المحول منها')
    from_treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_from', verbose_name='الخزينة (المحول منها)')
    from_bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_from', verbose_name='الحساب البنكي (المحول منه)')
    from_ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_from', verbose_name='المحفظة الإلكترونية (المحول منها)')

    # Destination Account
    to_account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default=TREASURY, verbose_name='نوع الجهة المحول إليها')
    to_treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_to', verbose_name='الخزينة (المحول إليها)')
    to_bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_to', verbose_name='الحساب البنكي (المحول إليه)')
    to_ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, null=True, blank=True, related_name='transfers_to', verbose_name='المحفظة الإلكترونية (المحول إليها)')

    description = models.TextField(blank=True, null=True, verbose_name='البيان / ملاحظات')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة')

    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='internal_transfer', verbose_name='قيد اليومية'
    )
    pos_session = models.ForeignKey(
        'invoicing.POSSession', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transfers', verbose_name='الوردية المرتبطة'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='internal_transfers', verbose_name='المحرر'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'تحويل مالي بين الحسابات'
        verbose_name_plural = 'تحويلات الأموال بين الحسابات'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"تحويل #{self.transfer_number} - {self.amount} ج.م"

    def save(self, *args, **kwargs):
        if not self.transfer_number:
            year = datetime.date.today().year
            with transaction.atomic():
                self.transfer_number = _next_doc_number(
                    'internal_transfer', InternalTransfer, 'transfer_number', f"TRF-{year}-"
                )
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @property
    def source_object(self):
        if self.from_account_type == self.TREASURY:
            return self.from_treasury
        elif self.from_account_type == self.BANK:
            return self.from_bank_account
        elif self.from_account_type == self.EWALLET:
            return self.from_ewallet
        return None

    @property
    def destination_object(self):
        if self.to_account_type == self.TREASURY:
            return self.to_treasury
        elif self.to_account_type == self.BANK:
            return self.to_bank_account
        elif self.to_account_type == self.EWALLET:
            return self.to_ewallet
        return None

    @property
    def source_name(self):
        obj = self.source_object
        return obj.name if obj else '-'

    @property
    def destination_name(self):
        obj = self.destination_object
        return obj.name if obj else '-'

    @property
    def source_gl_account(self):
        obj = self.source_object
        return obj.account if (obj and hasattr(obj, 'account')) else None

    @property
    def destination_gl_account(self):
        obj = self.destination_object
        return obj.account if (obj and hasattr(obj, 'account')) else None

    def confirm_transfer(self):
        """Generates accounting entry and marks transfer as POSTED."""
        from django.db import transaction
        from django.core.exceptions import ValidationError

        with transaction.atomic():
            # Lock the transfer row and re-check its status (no double posting)
            current_status = InternalTransfer.objects.select_for_update().filter(pk=self.pk).values_list('status', flat=True).first()
            if current_status is not None:
                self.status = current_status
            if self.status != self.DRAFT:
                raise ValidationError("لا يمكن ترحيل إلا مسودة التحويل.")
            if (self.amount or Decimal('0')) <= 0:
                raise ValidationError("يجب أن يكون مبلغ التحويل أكبر من صفر.")

            src_acc = self.source_gl_account
            dst_acc = self.destination_gl_account

            if not src_acc:
                raise ValidationError("يجب تحديد حساب شجرة الحسابات المرتبط بالجهة المحول منها.")
            if not dst_acc:
                raise ValidationError("يجب تحديد حساب شجرة الحسابات المرتبط بالجهة المحول إليها.")
            if src_acc == dst_acc:
                raise ValidationError("غير مسموح بالتحويل من وإلى نفس الحساب المحاسبي.")

            # Lock the source ledger account row: concurrent transfers out of the same
            # treasury/bank/wallet are serialized, so the balance check below can't be raced.
            locked_src_acc = Account.objects.select_for_update().get(pk=src_acc.pk)

            src_obj = self.source_object
            if src_obj:
                src_balance = locked_src_acc.current_balance  # fresh instance -> live ledger balance
                if src_balance < self.amount:
                    raise ValidationError(f"عفواً، رصيد {self.source_name} الحالي ({src_balance:,.2f} ج.م) غير كافٍ لإكمال تحويل بمبلغ {self.amount:,.2f} ج.م.")


            # Delete any orphaned journal entry (never a reversal pair)
            from apps.tenant.services.journal_service import delete_orphan_entries, unique_reference
            delete_orphan_entries(self.transfer_number)

            entry = JournalEntry.objects.create(
                date=self.date,
                reference=unique_reference(self.transfer_number),
                description=f"تحويل مالي ({self.source_name} ⬅️ {self.destination_name}) - {self.description or ''}",
                status=JournalEntry.POSTED
            )

            # Debit: Destination Account
            JournalItem.objects.create(entry=entry, account=dst_acc, debit=self.amount, credit=0)
            # Credit: Source Account
            JournalItem.objects.create(entry=entry, account=src_acc, debit=0, credit=self.amount)

            entry.post()

            self.status = self.POSTED
            self.journal_entry = entry
            self.save()

    def cancel_transfer(self, user=None):
        """
        Cancels the transfer. Its posted journal entry is kept and a reversal entry is
        posted against it (audit trail); ``journal_entry`` is cleared.
        """
        from django.db import transaction
        from django.core.exceptions import ValidationError
        from apps.tenant.services.journal_service import reverse_entry_if_active

        with transaction.atomic():
            locked = InternalTransfer.objects.select_for_update().select_related('pos_session', 'journal_entry').get(pk=self.pk)
            if locked.status == self.CANCELLED:
                raise ValidationError("التحويل ملغي بالفعل.")
            if locked.status == self.POSTED:
                _ensure_pos_session_open(locked.pos_session, 'إلغاء')

            if locked.journal_entry_id:
                reverse_entry_if_active(
                    locked.journal_entry, reason=f'إلغاء التحويل {locked.transfer_number}', user=user
                )

            self.journal_entry = None
            self.status = self.CANCELLED
            self.save(update_fields=['status', 'journal_entry', 'updated_at'])


# ---------------------------------------------------------------------------
# Cheque Management (أوراق القبض وأوراق الدفع)
# ---------------------------------------------------------------------------

class ChequeBook(models.Model):
    """
    دفاتر الشيكات الخاصة بحسابات بنوك الشركة (لشيكات الدفع الصادرة).
    """
    bank_account = models.ForeignKey(
        'BankAccount', on_delete=models.CASCADE, related_name='cheque_books',
        verbose_name='حساب بنك الشركة'
    )
    book_number = models.CharField(max_length=50, verbose_name='رقم / اسم الدفتر')
    series_prefix = models.CharField(max_length=15, blank=True, verbose_name='بادئة الترقيم (إن وجدت)')
    start_number = models.PositiveIntegerField(verbose_name='رقم أول شيك')
    end_number = models.PositiveIntegerField(verbose_name='رقم آخر شيك')
    current_number = models.PositiveIntegerField(verbose_name='الرقم الحالي التالي')
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'دفتر شيكات'
        verbose_name_plural = 'دفاتر الشيكات'
        ordering = ['bank_account', 'book_number']

    def __str__(self):
        return f"{self.book_number} ({self.bank_account.name}) [{self.start_number} - {self.end_number}]"

    def get_next_number(self):
        """Returns next formatted cheque number or None if exhausted."""
        if self.current_number > self.end_number:
            return None
        prefix = self.series_prefix or ''
        return f"{prefix}{self.current_number}"

    def advance_number(self):
        """Increments current number if within range."""
        if self.current_number <= self.end_number:
            self.current_number += 1
            self.save(update_fields=['current_number'])


class ChequeType(models.TextChoices):
    RECEIVABLE = 'RECEIVABLE', 'شيك قبض (ورقة قبض من عميل)'
    PAYABLE = 'PAYABLE', 'شيك صرف (ورقة دفع لمورد)'


class ChequeStatus(models.TextChoices):
    # شيكات القبض (Receivable)
    UNDER_COLLECTION = 'UNDER_COLLECTION', 'تحت التحصيل'
    COLLECTED = 'COLLECTED', 'تم التحصيل'
    BOUNCED = 'BOUNCED', 'مرتد (مرفوض)'
    IN_PORTFOLIO = 'IN_PORTFOLIO', 'تحت التحصيل'  # للتوافقية السابقة

    # شيكات الصرف (Payable)
    ISSUED = 'ISSUED', 'صادر (تحت الصرف)'
    CLEARED = 'CLEARED', 'تم الصرف من البنك'
    CANCELLED = 'CANCELLED', 'ملغي'


class Cheque(models.Model):
    """
    نموذج الشيك التجاري (ورقة قبض أو ورقة دفع).
    يدير دورة الحياة المحاسبية الكاملة للشيك وفق المعايير المحاسبية عبر حساب وسيط.
    """
    cheque_type = models.CharField(
        max_length=20, choices=ChequeType.choices, verbose_name='نوع الشيك', db_index=True
    )
    cheque_number = models.CharField(max_length=50, verbose_name='رقم الشيك', db_index=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name='المبلغ (ج.م)')
    
    received_date = models.DateField(default=datetime.date.today, verbose_name='تاريخ الاستلام / التحرير')
    due_date = models.DateField(verbose_name='تاريخ الاستحقاق', db_index=True)
    clearance_date = models.DateField(null=True, blank=True, verbose_name='تاريخ التحصيل / الصرف الفعلي')
    
    partner = models.ForeignKey(
        'partners.Partner', on_delete=models.PROTECT, related_name='cheques',
        verbose_name='الشريك (العميل / المورد)'
    )
    payee_name = models.CharField(max_length=200, blank=True, verbose_name='المستفيد المكتوب بالشيك')
    
    # تفاصيل شيك الصرف (Payable)
    bank_account = models.ForeignKey(
        'BankAccount', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='issued_cheques', verbose_name='حساب بنك الشركة (المسحوب منه)'
    )
    cheque_book = models.ForeignKey(
        ChequeBook, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheques', verbose_name='دفتر الشيكات'
    )
    
    # تفاصيل شيك القبض (Receivable)
    drawer_bank_name = models.CharField(max_length=150, blank=True, verbose_name='بنك العميل المسحوب عليه')
    drawer_branch = models.CharField(max_length=100, blank=True, verbose_name='فرع بنك العميل')
    treasury = models.ForeignKey(
        'Treasury', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='held_cheques', verbose_name='خزينة / حافظة الحفظ'
    )
    collecting_bank = models.ForeignKey(
        'BankAccount', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='collecting_cheques', verbose_name='بنك الإيداع والتحصيل'
    )
    bank_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name='عمولة ومصاريف التحصيل البنكية'
    )
    endorsed_to_partner = models.ForeignKey(
        'partners.Partner', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='endorsed_cheques', verbose_name='المورد المظهر له الشيك'
    )
    bounce_reason = models.TextField(blank=True, verbose_name='سبب الارتداد / الرفض')
    
    status = models.CharField(
        max_length=25, choices=ChequeStatus.choices, default=ChequeStatus.UNDER_COLLECTION,
        verbose_name='حالة الشيك', db_index=True
    )
    
    # القيود المحاسبية المرتبطة بمراحل الشيك
    entry_receive_issue = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_receive_issue_entries', verbose_name='قيد الاستلام / التحرير'
    )
    entry_deposit = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_deposit_entries', verbose_name='قيد الإيداع للتحصيل'
    )
    entry_clearance = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_clearance_entries', verbose_name='قيد التحصيل / الصرف'
    )
    entry_bounce = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_bounce_entries', verbose_name='قيد الارتداد'
    )
    entry_endorse = models.ForeignKey(
        'JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cheque_endorse_entries', verbose_name='قيد التظهير'
    )

    attachment = models.ImageField(upload_to='cheques/', null=True, blank=True, verbose_name='صورة الشيك')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='أنشئ بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'شيك'
        verbose_name_plural = 'الشيكات وأوراق القبض والدفع'
        ordering = ['-received_date', '-created_at']

    def __str__(self):
        return f"{self.get_cheque_type_display()} #{self.cheque_number} - {self.amount} ج.م ({self.partner.name})"

    # ── مراجع القيود (فريدة لكل شيك عبر الـ pk) ─────────────────
    def _lock_and_refresh(self):
        """Lock this cheque row (SELECT ... FOR UPDATE) and refresh its status/entry links."""
        if not self.pk:
            return
        locked = Cheque.objects.select_for_update().get(pk=self.pk)
        for f in ('status', 'entry_receive_issue_id', 'entry_deposit_id', 'entry_clearance_id',
                  'entry_bounce_id', 'entry_endorse_id'):
            setattr(self, f, getattr(locked, f))

    def _linked_entry_ids(self):
        return {eid for eid in (self.entry_receive_issue_id, self.entry_deposit_id, self.entry_clearance_id,
                                self.entry_bounce_id, self.entry_endorse_id) if eid}

    def _stage_reference(self, stage, legacy_ref=None, linked_entry_id=None):
        """
        Journal reference for one lifecycle stage of THIS cheque: CHQ-<pk>-<STAGE>.
        The old format (CHK-REC-<cheque_number>) was shared by different partners' cheques
        carrying the same number, and the old code deleted entries by that bare reference.
        Entries are never deleted here; a stage that is already booked for this cheque raises.
        A legacy-format entry is only considered when it is linked to this same cheque.
        """
        already = ValidationError('تم إثبات القيد المحاسبي لهذه المرحلة على هذا الشيك مسبقاً.')
        if linked_entry_id:
            raise already
        ref = f"CHQ-{self.pk}-{stage}"
        if JournalEntry.objects.filter(reference=ref).exists():
            raise already
        if legacy_ref:
            legacy = JournalEntry.objects.filter(reference=legacy_ref).values_list('id', flat=True).first()
            if legacy and legacy in self._linked_entry_ids():
                raise already
        return ref

    # ── Helpers للحسابات المحاسبية ─────────────────────────────
    @staticmethod
    def get_notes_receivable_account():
        """1180 - أوراق قبض (حافظة الشيكات)"""
        acc = Account.objects.filter(code='1180').first()
        if not acc:
            curr_assets = Account.objects.filter(code='1100').first()
            acc = Account.objects.create(code='1180', name='أوراق قبض (حافظة الشيكات)', account_type=Account.ASSET, parent=curr_assets, allow_reconcile=True)
        return acc

    @staticmethod
    def get_under_collection_account():
        """1185 - شيكات برسم التحصيل (الحساب الوسيط بالبنك)"""
        acc = Account.objects.filter(code='1185').first()
        if not acc:
            curr_assets = Account.objects.filter(code='1100').first()
            acc = Account.objects.create(code='1185', name='شيكات برسم التحصيل', account_type=Account.ASSET, parent=curr_assets, allow_reconcile=True)
        return acc

    @staticmethod
    def get_notes_payable_account():
        """2170 - أوراق دفع (شيكات صادرة برسم السداد)"""
        acc = Account.objects.filter(code='2170').first()
        if not acc:
            curr_liab = Account.objects.filter(code='2100').first()
            acc = Account.objects.create(code='2170', name='أوراق دفع (شيكات برسم السداد)', account_type=Account.LIABILITY, parent=curr_liab, allow_reconcile=True)
        return acc

    @staticmethod
    def get_bank_expense_account():
        """5305 - عمولات ماكينات الدفع والمحافظ والبنوك (تحت 5300 - مصاريف إدارية وعمولات بنكية)"""
        from apps.tenant.services.journal_service import _get_system_account, PAYMENT_FEES_CODE
        if not Account.objects.filter(code='5300').exists():
            exp = Account.objects.filter(code='5000').first()
            Account.objects.create(code='5300', name='مصاريف إدارية وعمولات بنكية', account_type=Account.EXPENSE, parent=exp)
        return _get_system_account(PAYMENT_FEES_CODE)

    def get_partner_account(self):
        """تحديد حساب الشريك المناسب (مدين للعملاء أو دائن للموردين أو الموحد)"""
        p = self.partner
        if self.cheque_type == ChequeType.RECEIVABLE:
            if p.receivable_account:
                return p.receivable_account
            if p.account:
                return p.account
            # افتراضي ذمم مدينة
            acc = Account.objects.filter(code='1130').first()
            return acc
        else: # PAYABLE
            if p.payable_account:
                return p.payable_account
            if p.account:
                return p.account
            # افتراضي ذمم دائنة
            acc = Account.objects.filter(code='2110').first()
            return acc

    def log_action(self, action, from_status, to_status, user=None, notes=''):
        ChequeActionLog.objects.create(
            cheque=self,
            action=action,
            from_status=from_status,
            to_status=to_status,
            user=user,
            notes=notes
        )

    # ── دورة حياة شيك القبض (RECEIVABLE) ────────────────────────
    def action_receive(self, user=None):
        """
        1. استلام شيك القبض وإثباته مباشرة في الحساب الوسيط (1180 - أوراق قبض / تحت التحصيل):
        من حـ/ أوراق قبض - وسيط (1180)
        إلى حـ/ العملاء - ذمم مدينة (1130 / حساب الشريك)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.RECEIVABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات القبض فقط.")

            notes_rec_acc = self.get_notes_receivable_account()
            partner_acc = self.get_partner_account()
            if not partner_acc:
                raise ValidationError("لا يوجد حساب محاسبي مرتبط بالشريك.")

            # إنشاء قيد الاستلام بالحساب الوسيط
            ref = self._stage_reference('REC', legacy_ref=f"CHK-REC-{self.cheque_number}", linked_entry_id=self.entry_receive_issue_id)

            entry = JournalEntry.objects.create(
                date=self.received_date,
                reference=ref,
                description=f"استلام شيك قبض رقم {self.cheque_number} من العميل {self.partner.name} (حساب وسيط) - استحقاق {self.due_date}",
                status=JournalEntry.POSTED
            )

            # مدين: أوراق قبض (الحساب الوسيط)
            JournalItem.objects.create(
                entry=entry, account=notes_rec_acc, debit=self.amount, credit=0,
                description=f"ورقة قبض شيك {self.cheque_number} - {self.partner.name}"
            )
            # دائن: العميل (تخفيض المديونية)
            JournalItem.objects.create(
                entry=entry, account=partner_acc, debit=0, credit=self.amount,
                description=f"سداد بشيك رقم {self.cheque_number} - استحقاق {self.due_date}"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.UNDER_COLLECTION
            self.entry_receive_issue = entry
            self.save()
            self.log_action("استلام الشيك وإيداعه بالحساب الوسيط (تحت التحصيل)", old_status, self.status, user=user)

    def action_confirm_collection(self, destination=None, clearance_date=None, bank_fee=Decimal('0.00'), user=None):
        """
        2. التحصيل الفعلي لشيك القبض (في البنك أو الخزينة) وإقفال الحساب الوسيط:
        من حـ/ البنك أو الخزينة (بالمبلغ الصافي)
        من حـ/ عمولات ماكينات الدفع والمحافظ والبنوك (5305) بمبلغ العمولة (إن وجدت)
        إلى حـ/ أوراق قبض - وسيط (1180) بإجمالي مبلغ الشيك
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.RECEIVABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات القبض فقط.")
            if self.status not in (ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO):
                raise ValidationError("لا يمكن تحصيل الشيك إلا وهو في حالة تحت التحصيل.")

            # تحديد حساب جهة التحصيل (بنك أو خزينة)
            dest_acc = None
            dest_name = ""
            if destination:
                if hasattr(destination, 'ensure_account'):
                    dest_acc = destination.ensure_account()
                elif hasattr(destination, 'account'):
                    dest_acc = destination.account
                dest_name = getattr(destination, 'name', str(destination))
                if isinstance(destination, BankAccount):
                    self.collecting_bank = destination
                elif isinstance(destination, Treasury):
                    self.treasury = destination
            elif self.collecting_bank:
                dest_acc = self.collecting_bank.ensure_account()
                dest_name = self.collecting_bank.name
            elif self.treasury:
                dest_acc = self.treasury.ensure_account()
                dest_name = self.treasury.name

            if not dest_acc:
                raise ValidationError("يجب تحديد البنك أو الخزينة المراد تحصيل وإيداع الشيك بها.")

            clr_date = clearance_date or datetime.date.today()
            notes_rec_acc = self.get_notes_receivable_account()
            fee = Decimal(str(bank_fee or 0))
            net_amount = self.amount - fee

            if net_amount < 0:
                raise ValidationError("عمولة التحصيل لا يمكن أن تكون أكبر من قيمة الشيك.")

            ref = self._stage_reference('CLR', legacy_ref=f"CHK-CLR-{self.cheque_number}", linked_entry_id=self.entry_clearance_id)

            entry = JournalEntry.objects.create(
                date=clr_date,
                reference=ref,
                description=f"تحصيل شيك قبض رقم {self.cheque_number} وإيداعه في {dest_name}",
                status=JournalEntry.POSTED
            )

            # مدين: جهة التحصيل (البنك أو الخزينة) بالصافي
            JournalItem.objects.create(
                entry=entry, account=dest_acc, debit=net_amount, credit=0,
                description=f"إيداع صافي تحصيل شيك {self.cheque_number} في {dest_name}"
            )
            # مدين: مصاريف وعمولات بنكية (إن وجدت)
            if fee > 0:
                fee_acc = self.get_bank_expense_account()
                JournalItem.objects.create(
                    entry=entry, account=fee_acc, debit=fee, credit=0,
                    description=f"عمولة ومصاريف تحصيل شيك {self.cheque_number}"
                )
            # دائن: أوراق قبض (الحساب الوسيط 1180)
            JournalItem.objects.create(
                entry=entry, account=notes_rec_acc, debit=0, credit=self.amount,
                description=f"إقفال ورقة قبض شيك {self.cheque_number} بعد التحصيل"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.COLLECTED
            self.clearance_date = clr_date
            self.bank_fee = fee
            self.entry_clearance = entry
            self.save()
            self.log_action(f"تحصيل الشيك في {dest_name} (صافي: {net_amount:,.2f} ج.م)", old_status, self.status, user=user)

    def action_bounce(self, bounce_reason, bounce_date=None, user=None):
        """
        3. ارتداد / رفض الشيك (إعادة فتح مديونية العميل وإلغاء الحساب الوسيط):
        من حـ/ العميل (1130 / حساب الشريك)
        إلى حـ/ أوراق قبض - وسيط (1180)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.RECEIVABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات القبض فقط.")
            if self.status not in (ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO):
                raise ValidationError("لا يمكن إثبات ارتداد الشيك إلا وهو في حالة تحت التحصيل.")

            bnc_date = bounce_date or datetime.date.today()
            partner_acc = self.get_partner_account()
            notes_rec_acc = self.get_notes_receivable_account()

            ref = self._stage_reference('BNC', legacy_ref=f"CHK-BNC-{self.cheque_number}", linked_entry_id=self.entry_bounce_id)

            entry = JournalEntry.objects.create(
                date=bnc_date,
                reference=ref,
                description=f"ارتداد ورفض شيك قبض رقم {self.cheque_number} للعميل {self.partner.name} - السبب: {bounce_reason}",
                status=JournalEntry.POSTED
            )

            # مدين: العميل (إعادة مديونيته)
            JournalItem.objects.create(
                entry=entry, account=partner_acc, debit=self.amount, credit=0,
                description=f"إعادة مديونية شيك مرتد رقم {self.cheque_number} - {bounce_reason}"
            )
            # دائن: أوراق قبض (الحساب الوسيط)
            JournalItem.objects.create(
                entry=entry, account=notes_rec_acc, debit=0, credit=self.amount,
                description=f"إلغاء ورقة قبض شيك مرتد {self.cheque_number}"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.BOUNCED
            self.bounce_reason = bounce_reason
            self.entry_bounce = entry
            self.save()
            self.log_action(f"ارتداد الشيك: {bounce_reason}", old_status, self.status, user=user)

    # ── دورة حياة شيك الصرف (PAYABLE) ───────────────────────────
    def action_issue(self, user=None):
        """
        1. تحرير وتسليم شيك دفع لمورد:
        من حـ/ الموردين - ذمم دائنة (2110 / حساب الشريك)
        إلى حـ/ أوراق دفع (2170)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.PAYABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات الصرف فقط.")
            if not self.bank_account:
                raise ValidationError("يجب تحديد حساب بنك الشركة المسحوب منه الشيك.")

            supplier_acc = self.get_partner_account()
            notes_pay_acc = self.get_notes_payable_account()

            ref = self._stage_reference('PAY', legacy_ref=f"CHK-PAY-{self.cheque_number}", linked_entry_id=self.entry_receive_issue_id)

            entry = JournalEntry.objects.create(
                date=self.received_date,
                reference=ref,
                description=f"تحرير شيك دفع رقم {self.cheque_number} للمورد {self.partner.name} مسحوب على {self.bank_account.name} - استحقاق {self.due_date}",
                status=JournalEntry.POSTED
            )

            # مدين: المورد (تخفيض التزامه)
            JournalItem.objects.create(
                entry=entry, account=supplier_acc, debit=self.amount, credit=0,
                description=f"سداد بشيك دفع رقم {self.cheque_number} - مسحوب على {self.bank_account.name}"
            )
            # دائن: أوراق دفع
            JournalItem.objects.create(
                entry=entry, account=notes_pay_acc, debit=0, credit=self.amount,
                description=f"ورقة دفع شيك رقم {self.cheque_number} - استحقاق {self.due_date}"
            )
            entry.post()

            # تقدم رقم دفتر الشيكات إذا كان مسجلاً بالدفتر
            if self.cheque_book:
                self.cheque_book.advance_number()

            old_status = self.status
            self.status = ChequeStatus.ISSUED
            self.entry_receive_issue = entry
            self.save()
            self.log_action(f"تحرير وتسليم الشيك للمورد {self.partner.name}", old_status, self.status, user=user)

    def action_clear_payable(self, clearance_date=None, user=None):
        """
        2. صرف شيك الدفع وخصمه فعلياً من حساب بنك الشركة:
        من حـ/ أوراق دفع (2170)
        إلى حـ/ بنك الشركة (1120)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.PAYABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات الصرف فقط.")
            if self.status != ChequeStatus.ISSUED:
                raise ValidationError("لا يمكن صرف الشيك إلا إذا كان في حالة محرر ومُسلّم.")
            if not self.bank_account:
                raise ValidationError("لم يتم تحديد حساب بنك الشركة المسحوب منه.")

            clr_date = clearance_date or datetime.date.today()
            notes_pay_acc = self.get_notes_payable_account()
            bank_acc = self.bank_account.ensure_account()

            ref = self._stage_reference('CLRPAY', legacy_ref=f"CHK-CLR-PAY-{self.cheque_number}", linked_entry_id=self.entry_clearance_id)

            entry = JournalEntry.objects.create(
                date=clr_date,
                reference=ref,
                description=f"صرف شيك دفع رقم {self.cheque_number} وخصمه من حساب {self.bank_account.name} (المستفيد: {self.partner.name})",
                status=JournalEntry.POSTED
            )

            # مدين: أوراق دفع (إقفال الالتزام)
            JournalItem.objects.create(
                entry=entry, account=notes_pay_acc, debit=self.amount, credit=0,
                description=f"صرف ورقة دفع شيك {self.cheque_number}"
            )
            # دائن: بنك الشركة (الخصم الفعلي من الرصيد البنكي)
            JournalItem.objects.create(
                entry=entry, account=bank_acc, debit=0, credit=self.amount,
                description=f"خصم قيمة شيك {self.cheque_number} من كشف حساب {self.bank_account.name}"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.CLEARED
            self.clearance_date = clr_date
            self.entry_clearance = entry
            self.save()
            self.log_action(f"صرف الشيك من حساب {self.bank_account.name}", old_status, self.status, user=user)

    def action_cancel_payable(self, cancel_reason, user=None):
        """
        3. إلغاء شيك دفع وإعادة مديونية المورد:
        من حـ/ أوراق دفع (2170)
        إلى حـ/ الموردين (2110 / حساب الشريك)
        """
        with transaction.atomic():
            self._lock_and_refresh()
            if self.cheque_type != ChequeType.PAYABLE:
                raise ValidationError("هذا الإجراء مخصص لشيكات الصرف فقط.")
            if self.status != ChequeStatus.ISSUED:
                raise ValidationError("لا يمكن إلغاء الشيك إلا إذا كان في حالة محرر ومُسلّم.")

            notes_pay_acc = self.get_notes_payable_account()
            supplier_acc = self.get_partner_account()

            ref = self._stage_reference('CNCPAY', legacy_ref=f"CHK-CNC-PAY-{self.cheque_number}", linked_entry_id=None)

            entry = JournalEntry.objects.create(
                date=datetime.date.today(),
                reference=ref,
                description=f"إلغاء شيك دفع رقم {self.cheque_number} للمورد {self.partner.name} - السبب: {cancel_reason}",
                status=JournalEntry.POSTED
            )

            # مدين: أوراق دفع
            JournalItem.objects.create(
                entry=entry, account=notes_pay_acc, debit=self.amount, credit=0,
                description=f"إلغاء ورقة دفع شيك {self.cheque_number}"
            )
            # دائن: المورد (إعادة استحقاقه)
            JournalItem.objects.create(
                entry=entry, account=supplier_acc, debit=0, credit=self.amount,
                description=f"إعادة مديونية لإلغاء شيك {self.cheque_number} - {cancel_reason}"
            )
            entry.post()

            old_status = self.status
            self.status = ChequeStatus.CANCELLED
            self.notes = f"{self.notes}\n[سبب الإلغاء]: {cancel_reason}".strip()
            self.save()
            self.log_action(f"إلغاء الشيك: {cancel_reason}", old_status, self.status, user=user)


class ChequeActionLog(models.Model):
    """سجل تدقيق ومتابعة جميع الحركات التي تمت على الشيك."""
    cheque = models.ForeignKey(
        Cheque, on_delete=models.CASCADE, related_name='action_logs', verbose_name='الشيك'
    )
    action = models.CharField(max_length=100, verbose_name='نوع الإجراء')
    from_status = models.CharField(max_length=30, blank=True, choices=ChequeStatus.choices, verbose_name='من حالة')
    to_status = models.CharField(max_length=30, choices=ChequeStatus.choices, verbose_name='إلى حالة')
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='المستخدم'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='وقت الإجراء')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')

    class Meta:
        verbose_name = 'سجل حركة شيك'
        verbose_name_plural = 'سجل حركات الشيكات'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.cheque.cheque_number} - {self.action} ({self.created_at.strftime('%Y-%m-%d %H:%M')})"

    @classmethod
    def get_status_label(cls, status_val):
        if not status_val or status_val == '-':
            return '-'
        status_map = {
            'UNDER_COLLECTION': 'تحت التحصيل',
            'IN_PORTFOLIO': 'تحت التحصيل',
            'COLLECTED': 'تم التحصيل',
            'BOUNCED': 'مرتد (مرفوض)',
            'ISSUED': 'صادر (تحت الصرف)',
            'CLEARED': 'تم الصرف من البنك',
            'CANCELLED': 'ملغي',
            'ENDORSED': 'مظهر لمورد',
            'DEPOSITED': 'مودع للتحصيل',
        }
        for k, v in ChequeStatus.choices:
            if k not in status_map:
                status_map[k] = v
        return status_map.get(status_val, status_val)

    @property
    def from_status_display(self):
        return self.get_status_label(self.from_status)

    @property
    def to_status_display(self):
        return self.get_status_label(self.to_status)

    def get_from_status_display(self):
        return self.from_status_display

    def get_to_status_display(self):
        return self.to_status_display


# ---------------------------------------------------------------------------
# Fixed Assets & Depreciation (الأصول الثابتة والإهلاك)
# ---------------------------------------------------------------------------

class FixedAsset(models.Model):
    """
    Fixed Asset card representing machinery, vehicles, POS equipment, fixtures, etc.
    Maintains cost, straight-line depreciation schedule, book value, and lifecycle.
    """
    class AssetCategory(models.TextChoices):
        EQUIPMENT = 'EQUIPMENT', 'أجهزة ومعدات ومبردات'
        VEHICLE = 'VEHICLE', 'سيارات ومركبات توصيل'
        ELECTRONICS = 'ELECTRONICS', 'أجهزة إلكترونية ونقاط بيع وكاشير'
        FURNITURE = 'FURNITURE', 'أثاث وتجهيزات وديكور'
        BUILDING = 'BUILDING', 'مباني وتحسينات وتجهيزات مقرات'
        OTHER = 'OTHER', 'أصول أخرى'

    class AssetStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', 'نشط ويعمل'
        FULLY_DEPRECIATED = 'FULLY_DEPRECIATED', 'مستهلك بالكامل دفترياً'
        DISPOSED = 'DISPOSED', 'مستبعد / تم بيعه أو تخريده'

    # Identification
    code = models.CharField(max_length=50, unique=True, verbose_name='كود الأصل')
    name = models.CharField(max_length=200, verbose_name='اسم الأصل')
    category = models.CharField(
        max_length=30, choices=AssetCategory.choices, default=AssetCategory.EQUIPMENT, verbose_name='تصنيف الأصل'
    )
    branch = models.ForeignKey('core.Branch', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='الفرع')
    location = models.CharField(max_length=150, blank=True, verbose_name='مكان التواجد / القسم')

    # Purchase Details
    purchase_date = models.DateField(verbose_name='تاريخ الشراء / بدء الاستخدام')
    purchase_cost = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='تكلفة الشراء الأصلية')
    salvage_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name='القيمة التخريدية (الخردة)'
    )

    # Opening Balance Handling (for assets purchased before system launch)
    is_opening_balance = models.BooleanField(
        default=False, verbose_name='أصل قائم (رصيد افتتاحي سابق)'
    )
    opening_accumulated_depreciation = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name='مجمع الإهلاك السابق (رصيد افتتاحي)'
    )

    # Depreciation Policy (Straight-Line)
    useful_life_years = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('5.00'), verbose_name='العمر الافتراضي (بالسنوات)'
    )
    depreciation_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('20.00'), verbose_name='نسبة الإهلاك السنوية %'
    )

    # Accounts
    asset_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='fixed_assets',
        limit_choices_to={'account_type': Account.ASSET},
        verbose_name='حساب الأصل'
    )
    accumulated_depreciation_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='fixed_assets_acc_dep',
        verbose_name='حساب مجمع الإهلاك'
    )
    depreciation_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='fixed_assets_dep_exp',
        limit_choices_to={'account_type': Account.EXPENSE},
        verbose_name='حساب مصروف الإهلاك'
    )

    # Acquisition Journal Entry
    purchase_entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='fixed_asset_purchase_entries', verbose_name='قيد الشراء / الإثبات'
    )

    # Lifecycle Status & Disposal
    status = models.CharField(
        max_length=20, choices=AssetStatus.choices, default=AssetStatus.ACTIVE, verbose_name='الحالة الدفترية'
    )
    disposal_date = models.DateField(null=True, blank=True, verbose_name='تاريخ الاستبعاد / البيع')
    disposal_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name='سعر البيع / الاستبعاد'
    )
    disposal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='fixed_asset_disposal_entries', verbose_name='قيد الاستبعاد'
    )
    disposal_notes = models.TextField(blank=True, verbose_name='ملاحظات الاستبعاد')

    notes = models.TextField(blank=True, verbose_name='ملاحظات عامة')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'أصل ثابت'
        verbose_name_plural = 'الأصول الثابتة'
        ordering = ['-purchase_date', '-id']

    def __str__(self):
        return f"{self.code} - {self.name} ({self.get_category_display()})"

    def save(self, *args, **kwargs):
        if not self.code:
            # race-safe; the surrounding view (FixedAssetCreateView) runs inside transaction.atomic()
            self.code = _next_doc_number('fixed_asset', FixedAsset, 'code', "AST-", width=4)

        # Automatic sync between useful_life_years and depreciation_rate
        if self.useful_life_years and self.useful_life_years > Decimal('0'):
            self.depreciation_rate = (Decimal('100.0') / self.useful_life_years).quantize(Decimal('0.01'))
        elif self.depreciation_rate and self.depreciation_rate > Decimal('0'):
            self.useful_life_years = (Decimal('100.0') / self.depreciation_rate).quantize(Decimal('0.01'))

        # Auto-update status if fully depreciated and still active
        if self.pk and self.status == self.AssetStatus.ACTIVE:
            if self.total_accumulated_depreciation >= self.depreciable_base and self.depreciable_base > Decimal('0'):
                self.status = self.AssetStatus.FULLY_DEPRECIATED

        super().save(*args, **kwargs)

    @property
    def depreciable_base(self):
        """القيمة القابلة للإهلاك = تكلفة الأصل - قيمة الخردة"""
        cost = self.purchase_cost or Decimal('0.00')
        salvage = self.salvage_value or Decimal('0.00')
        return max(cost - salvage, Decimal('0.00'))

    @property
    def monthly_depreciation_amount(self):
        """قسط الإهلاك الشهري بطريقة القسط الثابت"""
        if self.depreciable_base <= Decimal('0.00'):
            return Decimal('0.00')
        if self.useful_life_years and self.useful_life_years > Decimal('0'):
            total_months = self.useful_life_years * Decimal('12.0')
            return (self.depreciable_base / total_months).quantize(Decimal('0.01'))
        if self.depreciation_rate and self.depreciation_rate > Decimal('0'):
            annual = self.depreciable_base * (self.depreciation_rate / Decimal('100.0'))
            return (annual / Decimal('12.0')).quantize(Decimal('0.01'))
        return Decimal('0.00')

    @property
    def total_accumulated_depreciation(self):
        """إجمالي مجمع الإهلاك حتى اليوم (افتتاحي + أقساط مسجلة)"""
        opening = self.opening_accumulated_depreciation or Decimal('0.00')
        logged = Decimal('0.00')
        if self.pk:
            logged = self.depreciation_logs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        total = opening + logged
        return min(total, self.depreciable_base)

    @property
    def current_book_value(self):
        """صافي القيمة الدفترية = التكلفة - مجمع الإهلاك"""
        cost = self.purchase_cost or Decimal('0.00')
        salvage = self.salvage_value or Decimal('0.00')
        return max(cost - self.total_accumulated_depreciation, salvage)

    @property
    def depreciation_progress_percent(self):
        """نسبة استهلاك الأصل حتى اليوم"""
        if self.depreciable_base <= Decimal('0.00'):
            return 100.0
        pct = (self.total_accumulated_depreciation / self.depreciable_base) * Decimal('100.0')
        return float(min(round(pct, 1), Decimal('100.0')))

    @property
    def is_depreciable(self):
        """هل الأصل مؤهل لحساب إهلاك عليه؟"""
        return (
            self.status == self.AssetStatus.ACTIVE and
            self.total_accumulated_depreciation < self.depreciable_base and
            self.current_book_value > (self.salvage_value or Decimal('0.00'))
        )


class AssetDepreciationLog(models.Model):
    """
    Periodic depreciation run entry for an asset, linking to the double-entry JournalEntry.
    """
    asset = models.ForeignKey(
        FixedAsset, on_delete=models.CASCADE, related_name='depreciation_logs', verbose_name='الأصل'
    )
    year = models.IntegerField(verbose_name='السنة')
    month = models.IntegerField(verbose_name='الشهر')
    date = models.DateField(verbose_name='تاريخ احتساب الإهلاك')
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='قسط الإهلاك')
    book_value_after = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='القيمة الدفترية بعد الإهلاك')
    entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='fixed_asset_depreciation_entries', verbose_name='قيد اليومية'
    )
    notes = models.CharField(max_length=255, blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='وقت الاحتساب')
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='المستخدم'
    )

    class Meta:
        verbose_name = 'سجل إهلاك أصل'
        verbose_name_plural = 'سجل إهلاكات الأصول'
        unique_together = ('asset', 'year', 'month')
        ordering = ['-year', '-month', '-id']

    def __str__(self):
        return f"{self.asset.name} - {self.year}/{self.month:02d} ({self.amount})"


# ---------------------------------------------------------------------------
# Loans & Financing (القروض والتسهيلات البنكية وجداول الأقساط)
# ---------------------------------------------------------------------------

class Loan(models.Model):
    """
    Bank loans, Islamic financing, overdraft facilities, or partner loans.
    Tracks principal, interest, repayment installments schedule, and ledger balances.
    """
    class LoanType(models.TextChoices):
        BANK_LOAN = 'BANK_LOAN', 'قرض بنكي تجاري'
        ISLAMIC = 'ISLAMIC', 'تمويل / مرابحة إسلامية'
        PARTNER_LOAN = 'PARTNER_LOAN', 'تمويل شخصي / قرض شريك (بدون فوائد)'
        CREDIT_FACILITY = 'CREDIT_FACILITY', 'تسهيل ائتماني / جاري مدين'

    class LoanStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', 'قائم وجاري السداد'
        FULLY_PAID = 'FULLY_PAID', 'مسدد بالكامل'
        DEFAULTED = 'DEFAULTED', 'متعثر'

    # Identification
    code = models.CharField(max_length=50, unique=True, verbose_name='رقم/كود القرض')
    name = models.CharField(max_length=200, verbose_name='اسم/بيان القرض')
    loan_type = models.CharField(
        max_length=30, choices=LoanType.choices, default=LoanType.BANK_LOAN, verbose_name='نوع القرض والتمويل'
    )
    lender_name = models.CharField(max_length=150, verbose_name='جهة التمويل / اسم البنك أو المقرض')
    contract_number = models.CharField(max_length=100, blank=True, verbose_name='رقم العقد / المعاملة')
    branch = models.ForeignKey('core.Branch', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='الفرع')

    # Financial details
    principal_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='أصل مبلغ القرض المقترض')
    total_interest_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name='إجمالي الفوائد / أرباح التمويل'
    )
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name='إجمالي التزام القرض (أصل + فوائد)'
    )

    start_date = models.DateField(verbose_name='تاريخ بدء / استلام القرض')
    installments_count = models.PositiveIntegerField(default=12, verbose_name='عدد الأقساط')
    first_due_date = models.DateField(verbose_name='تاريخ استحقاق أول قسط')

    # Accounts
    loan_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='loans',
        limit_choices_to={'account_type': Account.LIABILITY},
        verbose_name='حساب التزام القرض (خصوم)'
    )
    interest_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='loans_interest_exp',
        limit_choices_to={'account_type': Account.EXPENSE},
        verbose_name='حساب مصروف الفوائد (مصروفات)'
    )

    # Initial receipt journal entry
    disbursement_entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='loan_disbursement_entries', verbose_name='قيد استلام وصرف القرض'
    )

    status = models.CharField(
        max_length=20, choices=LoanStatus.choices, default=LoanStatus.ACTIVE, verbose_name='حالة القرض'
    )
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'قرض / تمويل'
        verbose_name_plural = 'القروض والتسهيلات'
        ordering = ['-start_date', '-id']

    def __str__(self):
        return f"{self.code} - {self.name} ({self.lender_name})"

    def save(self, *args, **kwargs):
        if not self.code:
            # race-safe; the surrounding view (LoanCreateView) runs inside transaction.atomic()
            self.code = _next_doc_number('loan', Loan, 'code', "LOAN-", width=4)

        # Automatic calculation of total commitment
        principal = self.principal_amount or Decimal('0.00')
        interest = self.total_interest_amount or Decimal('0.00')
        self.total_amount = principal + interest

        # Auto-update status if all installments are paid
        if self.pk and self.status == self.LoanStatus.ACTIVE:
            unpaid = self.installments.filter(is_paid=False).exists()
            if not unpaid and self.installments.exists():
                self.status = self.LoanStatus.FULLY_PAID

        super().save(*args, **kwargs)

    @property
    def interest_rate(self):
        """معدل الفائدة التقديري السنوي"""
        if not self.principal_amount or self.principal_amount <= Decimal('0.00'):
            return Decimal('0.00')
        years = Decimal(self.installments_count or 12) / Decimal('12.0')
        if years <= Decimal('0.00'):
            years = Decimal('1.0')
        rate = (self.total_interest_amount / (self.principal_amount * years)) * Decimal('100.0')
        return round(rate, 2)

    @property
    def paid_principal(self):
        """إجمالي أصل القرض المسدد حتى اليوم"""
        if not self.pk:
            return Decimal('0.00')
        return self.installments.filter(is_paid=True).aggregate(s=Sum('principal_amount'))['s'] or Decimal('0.00')

    @property
    def paid_interest(self):
        """إجمالي الفوائد المسددة حتى اليوم"""
        if not self.pk:
            return Decimal('0.00')
        return self.installments.filter(is_paid=True).aggregate(s=Sum('interest_amount'))['s'] or Decimal('0.00')

    @property
    def total_paid(self):
        """إجمالي المبالغ المسددة (أصل + فوائد)"""
        return self.paid_principal + self.paid_interest

    @property
    def remaining_principal(self):
        """المتبقي من أصل القرض (الالتزام الحقيقي بالميزانية)"""
        return max(self.principal_amount - self.paid_principal, Decimal('0.00'))

    @property
    def remaining_total(self):
        """المتبقي الكلي من القرض شامل الفوائد القادمة"""
        return max(self.total_amount - self.total_paid, Decimal('0.00'))

    @property
    def repayment_progress_percent(self):
        """نسبة السداد المكتملة"""
        if not self.total_amount or self.total_amount <= Decimal('0.00'):
            return 100.0
        pct = (self.total_paid / self.total_amount) * Decimal('100.0')
        return float(min(round(pct, 1), Decimal('100.0')))

    @property
    def overdue_count(self):
        """عدد الأقساط المتأخرة"""
        if not self.pk:
            return 0
        return self.installments.filter(is_paid=False, due_date__lt=datetime.date.today()).count()

    @property
    def next_pending_installment(self):
        """القسط القادم المستحق"""
        if not self.pk:
            return None
        return self.installments.filter(is_paid=False).order_by('due_date').first()

    def generate_installments_schedule(self):
        """توليد جدول الأقساط الشهرية تلقائياً"""
        if not self.installments_count or self.installments_count <= 0:
            return

        # Delete any unpaid installments before regenerating
        self.installments.filter(is_paid=False).delete()

        n = self.installments_count
        base_p = (self.principal_amount / Decimal(n)).quantize(Decimal('0.01'))
        base_int = (self.total_interest_amount / Decimal(n)).quantize(Decimal('0.01'))

        current_date = self.first_due_date
        accumulated_p = Decimal('0.00')
        accumulated_int = Decimal('0.00')

        for i in range(1, n + 1):
            if i == n:
                # Adjust last installment for pennies rounding difference
                p_amt = self.principal_amount - accumulated_p
                int_amt = self.total_interest_amount - accumulated_int
            else:
                p_amt = base_p
                int_amt = base_int
                accumulated_p += p_amt
                accumulated_int += int_amt

            tot_amt = p_amt + int_amt

            LoanInstallment.objects.create(
                loan=self,
                installment_number=i,
                due_date=current_date,
                principal_amount=p_amt,
                interest_amount=int_amt,
                total_amount=tot_amt
            )

            # Advance by 1 month
            y = current_date.year
            m = current_date.month + 1
            if m > 12:
                y += 1
                m = 1
            d = min(current_date.day, 28)
            current_date = datetime.date(y, m, d)


class LoanInstallment(models.Model):
    """
    Individual scheduled installment for a loan.
    """
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='installments', verbose_name='القرض')
    installment_number = models.PositiveIntegerField(verbose_name='رقم القسط')
    due_date = models.DateField(verbose_name='تاريخ الاستحقاق')

    principal_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='أصل القرض في القسط')
    interest_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name='فائدة القسط')
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='إجمالي مبلغ القسط')

    # Payment details
    is_paid = models.BooleanField(default=False, verbose_name='تم السداد')
    payment_date = models.DateField(null=True, blank=True, verbose_name='تاريخ السداد الفعلي')
    payment_entry = models.ForeignKey(
        JournalEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='loan_installment_entries', verbose_name='قيد سداد القسط'
    )
    paid_treasury = models.ForeignKey(
        Treasury, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='الخزينة المسدد منها'
    )
    paid_bank = models.ForeignKey(
        BankAccount, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='البنك المسدد منه'
    )
    payment_notes = models.CharField(max_length=255, blank=True, verbose_name='ملاحظات السداد')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'قسط قرض'
        verbose_name_plural = 'أقساط القروض'
        ordering = ['due_date', 'installment_number']
        unique_together = ('loan', 'installment_number')

    def __str__(self):
        return f"{self.loan.name} - قسط #{self.installment_number} ({self.total_amount})"

    @property
    def is_overdue(self):
        return not self.is_paid and self.due_date < datetime.date.today()



