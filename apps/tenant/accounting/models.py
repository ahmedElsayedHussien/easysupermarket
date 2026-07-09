"""
Accounting models - Chart of Accounts, Journal Entries, Journal Items.
Implements full double-entry bookkeeping with MPTT account hierarchy.

Rules enforced:
  - Every JournalEntry must have sum(debit) == sum(credit) before posting.
  - A JournalItem may not have both debit > 0 AND credit > 0 simultaneously.
  - Accounts use MPTT for nested tree structure (Assets → Current Assets → Cash).
"""
from django.db import models
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import Sum, Q
from mptt.models import MPTTModel, TreeForeignKey
from decimal import Decimal
import datetime


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
    description = models.TextField(blank=True, verbose_name='وصف')
    created_at = models.DateTimeField(auto_now_add=True)

    class MPTTMeta:
        order_insertion_by = ['code']

    class Meta:
        verbose_name = 'حساب'
        verbose_name_plural = 'الحسابات'

    def __str__(self):
        return f'{self.code} - {self.name}'

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
        """
        items = JournalItem.objects.filter(
            account__in=self.get_descendants(include_self=True),
            entry__status=JournalEntry.POSTED
        )
        total_debit = items.aggregate(s=Sum('debit'))['s'] or Decimal('0')
        total_credit = items.aggregate(s=Sum('credit'))['s'] or Decimal('0')
        if self.normal_balance == 'DEBIT':
            return total_debit - total_credit
        return total_credit - total_debit

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

    class Meta:
        verbose_name = 'قيد يومية'
        verbose_name_plural = 'قيود اليومية'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.reference} - {self.date} [{self.get_status_display()}]'

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


class Tax(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم الضريبة')
    rate = models.DecimalField(max_digits=5, decimal_places=2, verbose_name='النسبة (%)')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, verbose_name='حساب الضريبة')
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    
    class Meta:
        verbose_name = 'ضريبة'
        verbose_name_plural = 'الضرائب'
        
    def __str__(self):
        return f"{self.name} ({self.rate_percent}%)"


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


from django.db.models.signals import post_save
from django.dispatch import receiver

class BankAccount(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم الحساب البنكي')
    branch = models.ForeignKey('core.Branch', on_delete=models.PROTECT, null=True, blank=True, verbose_name='الفرع')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الدليل')

    class Meta:
        verbose_name = 'حساب بنكي'
        verbose_name_plural = 'حسابات البنوك'

    def __str__(self):
        return self.name

class EWallet(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم المحفظة')
    branch = models.ForeignKey('core.Branch', on_delete=models.PROTECT, null=True, blank=True, verbose_name='الفرع')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الدليل')

    class Meta:
        verbose_name = 'محفظة إلكترونية'
        verbose_name_plural = 'المحافظ الإلكترونية'

    def __str__(self):
        return self.name

@receiver(post_save, sender=Treasury)
def create_treasury_account(sender, instance, created, **kwargs):
    updated = False
    has_valid_account = instance.account and getattr(instance.account, 'code', None) not in ['1100', '1110', '1120']
    
    if not has_valid_account:
        from apps.tenant.services.journal_service import _get_system_account
        try:
            parent_code = '1120' if 'بنك' in instance.name else '1110'
            parent_acc = _get_system_account(parent_code)
            
            acc = Account.objects.create(
                code=f"{parent_acc.code}-{instance.id}",
                name=f"{instance.name}",
                account_type=parent_acc.account_type,
                parent=parent_acc
            )
            instance.account = acc
            updated = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error creating treasury account: {str(e)}")
            pass
            
    if updated:
        instance.save(update_fields=['account'])

    if created and instance.opening_balance > 0 and instance.account:
        from apps.tenant.services.journal_service import _get_system_account
        from django.utils import timezone
        capital_acc = _get_system_account('3100')
        if capital_acc:
            entry = JournalEntry.objects.create(
                date=timezone.now().date(),
                reference=f"OPENING-TR-{instance.id}",
                description=f"رصيد افتتاحي - {instance.name}"
            )
            from .models import JournalEntryLine
            JournalEntryLine.objects.create(entry=entry, account=instance.account, debit=instance.opening_balance, credit=0)
            JournalEntryLine.objects.create(entry=entry, account=capital_acc, debit=0, credit=instance.opening_balance)

@receiver(post_save, sender=BankAccount)
def create_bank_account(sender, instance, created, **kwargs):
    updated = False
    has_valid_account = instance.account and getattr(instance.account, 'code', None) not in ['1100', '1120']
    
    if not has_valid_account:
        from apps.tenant.services.journal_service import _get_system_account
        try:
            parent_acc = _get_system_account('1120')
            acc = Account.objects.create(
                code=f"{parent_acc.code}-{instance.id}",
                name=f"{instance.name}",
                account_type=parent_acc.account_type,
                parent=parent_acc
            )
            instance.account = acc
            updated = True
        except Exception as e:
            pass
            
    if updated:
        instance.save(update_fields=['account'])

    if created and instance.opening_balance > 0 and instance.account:
        from apps.tenant.services.journal_service import _get_system_account
        from django.utils import timezone
        capital_acc = _get_system_account('3100')
        if capital_acc:
            entry = JournalEntry.objects.create(
                date=timezone.now().date(),
                reference=f"OPENING-BK-{instance.id}",
                description=f"رصيد افتتاحي - {instance.name}"
            )
            from .models import JournalEntryLine
            JournalEntryLine.objects.create(entry=entry, account=instance.account, debit=instance.opening_balance, credit=0)
            JournalEntryLine.objects.create(entry=entry, account=capital_acc, debit=0, credit=instance.opening_balance)

@receiver(post_save, sender=EWallet)
def create_ewallet_account(sender, instance, created, **kwargs):
    updated = False
    has_valid_account = instance.account and getattr(instance.account, 'code', None) not in ['1100', '1160']
    
    if not has_valid_account:
        from apps.tenant.services.journal_service import _get_system_account
        try:
            parent_acc = _get_system_account('1160')
            acc = Account.objects.create(
                code=f"{parent_acc.code}-{instance.id}",
                name=f"{instance.name}",
                account_type=parent_acc.account_type,
                parent=parent_acc
            )
            instance.account = acc
            updated = True
        except Exception as e:
            pass
            
    if updated:
        instance.save(update_fields=['account'])

    if created and instance.opening_balance > 0 and instance.account:
        from apps.tenant.services.journal_service import _get_system_account
        from django.utils import timezone
        capital_acc = _get_system_account('3100')
        if capital_acc:
            entry = JournalEntry.objects.create(
                date=timezone.now().date(),
                reference=f"OPENING-EW-{instance.id}",
                description=f"رصيد افتتاحي - {instance.name}"
            )
            from .models import JournalEntryLine
            JournalEntryLine.objects.create(entry=entry, account=instance.account, debit=instance.opening_balance, credit=0)
            JournalEntryLine.objects.create(entry=entry, account=capital_acc, debit=0, credit=instance.opening_balance)
