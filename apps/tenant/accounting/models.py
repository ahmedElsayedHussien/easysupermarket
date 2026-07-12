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
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='رصيد أول المدة')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, verbose_name='حساب الدليل')

    class Meta:
        verbose_name = 'حساب بنكي'
        verbose_name_plural = 'حسابات البنوك'

    def __str__(self):
        return self.name

class EWallet(models.Model):
    name = models.CharField(max_length=100, verbose_name='اسم المحفظة')
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
        if not self.expense_number:
            year = datetime.date.today().year
            prefix = f"EXP-{year}-"
            last = Expense.objects.filter(expense_number__startswith=prefix).order_by('-expense_number').first()
            if last:
                try:
                    last_num = int(last.expense_number.split('-')[-1])
                except ValueError:
                    last_num = 0
            else:
                last_num = 0
            self.expense_number = f"{prefix}{last_num + 1:05d}"
            
        # Calculate amounts
        self.vat_amount = (self.amount * (self.vat_percent / Decimal('100.0'))).quantize(Decimal('0.01'))
        self.withholding_tax_amount = (self.amount * (self.withholding_tax_percent / Decimal('100.0'))).quantize(Decimal('0.01'))
        self.total_amount = self.amount + self.vat_amount - self.withholding_tax_amount
        
        super().save(*args, **kwargs)

    def confirm_expense(self):
        """Generates the accounting journal entry and marks as POSTED."""
        from django.db import transaction
        
        with transaction.atomic():
            if self.status != self.DRAFT:
                from django.core.exceptions import ValidationError
                raise ValidationError("لا يمكن ترحيل إلا مسودة المصروف.")
                
            from apps.tenant.services.journal_service import _get_system_account
            
            # Clean up any orphaned journal entry from previous failed attempts
            JournalEntry.objects.filter(reference=self.expense_number).delete()
            
            entry = JournalEntry.objects.create(
                date=self.date,
                reference=self.expense_number,
                description=f"سداد مصروف: {self.description}"
            )
            
            # Dr: Expense Account
            JournalItem.objects.create(
                entry=entry,
                account=self.expense_account,
                debit=self.amount,
                credit=0
            )
            
            # Dr: VAT Account
            if self.vat_amount > 0:
                vat_account = _get_system_account('2130')  # 2130 is Input VAT
                if vat_account:
                    JournalItem.objects.create(
                        entry=entry,
                        account=vat_account,
                        debit=self.vat_amount,
                        credit=0
                    )
                    
            # Cr: Withholding Tax Account
            if self.withholding_tax_amount > 0:
                wh_account = _get_system_account('2140')
                if not wh_account:
                    # Fallback to create it
                    liability_parent = Account.objects.filter(code__startswith='2').first()
                    if liability_parent:
                        wh_account = Account.objects.create(code='2140', name='ضريبة الخصم والإضافة', account_type=Account.LIABILITY, parent=liability_parent)
                if wh_account:
                    JournalItem.objects.create(
                        entry=entry,
                        account=wh_account,
                        debit=0,
                        credit=self.withholding_tax_amount
                    )
                    
            # Cr: Payment Account (Cash/Bank)
            JournalItem.objects.create(
                entry=entry,
                account=self.payment_account,
                debit=0,
                credit=self.total_amount
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
    BANK_TRANSFER = 'BANK_TRANSFER'
    EWALLET = 'EWALLET'
    PAYMENT_METHOD_CHOICES = [
        (CASH, 'نقدي'),
        (BANK_TRANSFER, 'تحويل بنكي'),
        (EWALLET, 'محفظة إلكترونية')
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
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, null=True, blank=True, verbose_name='الحساب البنكي')
    ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, null=True, blank=True, verbose_name='المحفظة الإلكترونية')
    
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
        if not self.voucher_number:
            prefix = 'REC' if self.voucher_type == self.RECEIPT else 'PAY'
            year = datetime.date.today().year
            last_voucher = Voucher.objects.filter(voucher_number__startswith=f"{prefix}-{year}-").order_by('-voucher_number').first()
            if last_voucher:
                try:
                    last_num = int(last_voucher.voucher_number.split('-')[-1])
                except ValueError:
                    last_num = 0
            else:
                last_num = 0
            self.voucher_number = f"{prefix}-{year}-{(last_num + 1):05d}"
            
        super().save(*args, **kwargs)

    @property
    def payment_account(self):
        if self.payment_method == self.CASH and self.treasury:
            return self.treasury.account
        elif self.payment_method == self.BANK_TRANSFER and self.bank_account:
            return self.bank_account.account
        elif self.payment_method == self.EWALLET and self.ewallet:
            return self.ewallet.account
        return None

    def confirm_voucher(self):
        """Generates the accounting journal entry and marks as POSTED."""
        from django.db import transaction
        from django.core.exceptions import ValidationError
        
        with transaction.atomic():
            if self.status != self.DRAFT:
                raise ValidationError("لا يمكن ترحيل إلا مسودة السند.")
            
            if not self.payment_account:
                raise ValidationError("يجب تحديد حساب الدفع (الخزينة/البنك/المحفظة) المرتبط بطريقة الدفع.")
                
            # Clean up any orphaned journal entry
            JournalEntry.objects.filter(reference=self.voucher_number).delete()
            
            entry = JournalEntry.objects.create(
                date=self.date,
                reference=self.voucher_number,
                description=f"{self.get_voucher_type_display()} - {self.description or ''}"
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
        from apps.tenant.services.journal_service import _get_system_account
        from django.utils import timezone
        capital_acc = _get_system_account('3100')
        if capital_acc:
            entry = JournalEntry.objects.create(
                date=timezone.now().date(),
                reference=f'OPENING-PM-{instance.id}',
                description=f'رصيد افتتاحي - {instance.name}'
            )
            JournalItem.objects.create(entry=entry, account=instance.account, debit=instance.opening_balance, credit=0)
            JournalItem.objects.create(entry=entry, account=capital_acc, debit=0, credit=instance.opening_balance)
            entry.post()


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
            prefix = f'ESVC-{year}-'
            last = EServiceTransaction.objects.filter(
                transaction_number__startswith=prefix
            ).order_by('-transaction_number').first()
            last_num = 0
            if last:
                try:
                    last_num = int(last.transaction_number.split('-')[-1])
                except ValueError:
                    pass
            self.transaction_number = f'{prefix}{last_num + 1:05d}'
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
            if self.status == self.POSTED:
                raise ValidationError('هذه العملية مرحلة بالفعل.')

            source_account = self.get_source_account()
            if not source_account:
                raise ValidationError('يجب تحديد ماكينة أو محفظة للعملية.')
            if not self.treasury or not self.treasury.account:
                raise ValidationError('يجب تحديد خزينة/درج صحيح.')

            treasury_account = self.treasury.account
            revenue_account = _get_system_account('4200') or _get_system_account('4100')
            expense_account = _get_system_account('5200') or _get_system_account('5100')

            JournalEntry.objects.filter(reference=self.transaction_number).delete()

            entry = JournalEntry.objects.create(
                date=self.date,
                reference=self.transaction_number,
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
        """Creates a POSTED sales invoice for commission revenue."""
        try:
            from apps.tenant.invoicing.models import Invoice
            from apps.tenant.partners.models import Partner
            import datetime as dt

            cash_partner = Partner.objects.filter(name='عميل نقدي').first() or Partner.objects.first()
            if not cash_partner:
                return

            year = dt.date.today().year
            inv_prefix = f'SVC-{year}-'
            last_inv = Invoice.objects.filter(invoice_number__startswith=inv_prefix).order_by('-invoice_number').first()
            last_num = 0
            if last_inv:
                try:
                    last_num = int(last_inv.invoice_number.split('-')[-1])
                except ValueError:
                    pass
            inv_number = f'{inv_prefix}{last_num + 1:05d}'

            branch = self.treasury.branch
            warehouse = branch.warehouses.first()
            if not warehouse:
                return

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
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'Error creating service invoice: {e}')
