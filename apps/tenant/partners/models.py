"""
Partners models: Partner (Customer/Supplier) and Payment.

Partner is a unified model for both customers and suppliers.
Payment tracks cash/card/bank transactions and links to the journal.
"""
from django.db import models
from django.db.models import Sum
from decimal import Decimal
import datetime


# ---------------------------------------------------------------------------
# Partner
# ---------------------------------------------------------------------------

class Partner(models.Model):
    """
    A business entity that is either a Customer, Supplier, or both.
    Links to accounting accounts for A/R and A/P tracking.
    """
    CUSTOMER = 'CUSTOMER'
    SUPPLIER = 'SUPPLIER'
    BOTH = 'BOTH'

    PARTNER_TYPE_CHOICES = [
        (CUSTOMER, 'عميل'),
        (SUPPLIER, 'مورد'),
        (BOTH, 'عميل ومورد'),
    ]

    name = models.CharField(max_length=300, verbose_name='الاسم')
    partner_type = models.CharField(
        max_length=10, choices=PARTNER_TYPE_CHOICES, default=CUSTOMER,
        verbose_name='نوع الشريك'
    )
    phone = models.CharField(max_length=30, blank=True, verbose_name='الهاتف')
    mobile = models.CharField(max_length=30, blank=True, verbose_name='الموبايل')
    email = models.EmailField(null=True, blank=True, verbose_name='البريد الإلكتروني')
    national_id = models.CharField(max_length=20, null=True, blank=True, verbose_name='الرقم القومي')
    tax_id = models.CharField(
        max_length=50, blank=True, verbose_name='الرقم الضريبي'
    )
    subject_to_withholding_tax = models.BooleanField(
        default=False, verbose_name='يخضع لضريبة الخصم والإضافة'
    )
    address = models.TextField(blank=True, verbose_name='العنوان')

    # Accounting accounts (optional - if set, used for automated journal entries)
    receivable_account = models.ForeignKey(
        'accounting.Account',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='receivable_partners',
        verbose_name='حساب الذمم المدينة',
        help_text='تُستخدم لتسجيل المبيعات الآجلة'
    )
    payable_account = models.ForeignKey(
        'accounting.Account',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='payable_partners',
        verbose_name='حساب الذمم الدائنة',
        help_text='تُستخدم لتسجيل المشتريات الآجلة'
    )
    account = models.ForeignKey(
        'accounting.Account',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='general_partners',
        verbose_name='الحساب الموحد'
    )

    credit_limit = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='حد الائتمان'
    )
    payment_terms_days = models.IntegerField(
        default=30, verbose_name='مدة السداد (أيام)'
    )
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'شريك'
        verbose_name_plural = 'الشركاء'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} [{self.get_partner_type_display()}]'

    @property
    def outstanding_balance(self):
        """
        Calculates net outstanding balance for this partner from CREDIT invoices and Payments.
        
        For customers (receivable): positive means money owed TO us.
        For suppliers (payable): positive means money we OWE them.
        """
        from decimal import Decimal
        from django.db.models import Sum
        
        balance = Decimal('0')
        
        # Sales (Customer owes us)
        sales = self.invoices.filter(invoice_type='SALE', payment_type='CREDIT', status='POSTED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        returns = self.invoices.filter(invoice_type='RETURN_SALE', payment_type='CREDIT', status='POSTED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        balance += (sales - returns)
        
        # Purchases (We owe Supplier)
        purchases = self.invoices.filter(invoice_type='PURCHASE', payment_type='CREDIT', status='POSTED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        pur_returns = self.invoices.filter(invoice_type='RETURN_PURCHASE', payment_type='CREDIT', status='POSTED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        balance -= (purchases - pur_returns)
        
        # Payments
        receipts = self.payments.filter(payment_type='RECEIPT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Decimal('0')
        payments = self.payments.filter(payment_type='PAYMENT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Decimal('0')
        
        balance -= receipts
        balance += payments
        
        return balance

    @property
    def is_customer(self):
        return self.partner_type in (self.CUSTOMER, self.BOTH)

    @property
    def is_supplier(self):
        return self.partner_type in (self.SUPPLIER, self.BOTH)


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

def _generate_payment_reference():
    """
    Generates a unique payment reference like PAY-2024-00001.
    """
    year = datetime.date.today().year
    prefix = f'PAY-{year}-'
    last = Payment.objects.filter(reference__startswith=prefix).order_by('-reference').first()
    if last:
        try:
            last_num = int(last.reference.split('-')[-1])
        except (ValueError, IndexError):
            last_num = 0
    else:
        last_num = 0
    return f'{prefix}{last_num + 1:05d}'


class Payment(models.Model):
    """
    A financial payment or receipt transaction.

    RECEIPT: Money received from a customer.
    PAYMENT: Money paid to a supplier.

    Each Payment creates a corresponding JournalEntry when posted.
    """
    RECEIPT = 'RECEIPT'
    PAYMENT = 'PAYMENT'

    PAYMENT_TYPE_CHOICES = [
        (RECEIPT, 'تحصيل من عميل'),
        (PAYMENT, 'دفع لمورد'),
    ]

    CASH = 'CASH'
    CARD = 'CARD'
    BANK = 'BANK'

    METHOD_CHOICES = [
        (CASH, 'نقدي'),
        (CARD, 'بطاقة'),
        (BANK, 'تحويل بنكي'),
    ]

    DRAFT = 'DRAFT'
    POSTED = 'POSTED'

    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحّل'),
    ]

    payment_type = models.CharField(
        max_length=10, choices=PAYMENT_TYPE_CHOICES, verbose_name='نوع العملية'
    )
    partner = models.ForeignKey(
        Partner,
        on_delete=models.PROTECT,
        related_name='payments',
        verbose_name='الشريك'
    )
    branch = models.ForeignKey(
        'core.Branch',
        on_delete=models.PROTECT,
        related_name='payments',
        verbose_name='الفرع'
    )
    amount = models.DecimalField(
        max_digits=15, decimal_places=4, verbose_name='المبلغ'
    )
    date = models.DateField(verbose_name='التاريخ')
    method = models.CharField(
        max_length=10, choices=METHOD_CHOICES, default=CASH, verbose_name='طريقة الدفع'
    )
    reference = models.CharField(
        max_length=30, unique=True, blank=True, verbose_name='رقم المرجع'
    )
    journal_entry = models.ForeignKey(
        'accounting.JournalEntry',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments',
        verbose_name='القيد المحاسبي'
    )
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'دفعة'
        verbose_name_plural = 'الدفعات'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.reference} - {self.partner.name} - {self.amount}'

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = _generate_payment_reference()
        super().save(*args, **kwargs)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.amount <= 0:
            raise ValidationError('المبلغ يجب أن يكون أكبر من صفر.')


from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=Partner)
def create_partner_account(sender, instance, created, **kwargs):
    from apps.tenant.accounting.models import Account
    from apps.tenant.services.journal_service import _get_system_account
    
    updated = False
    
    # Check if receivable account is missing OR is pointing to the root 1210 account
    has_valid_receivable = instance.receivable_account and getattr(instance.receivable_account, 'code', None) != '1210'
    if instance.is_customer and not has_valid_receivable:
        try:
            parent_acc = _get_system_account('1210')
            acc = Account.objects.create(
                code=f"{parent_acc.code}-{instance.id}",
                name=f"{instance.name}",
                account_type=parent_acc.account_type,
                parent=parent_acc
            )
            instance.receivable_account = acc
            if not instance.account or getattr(instance.account, 'code', None) == '1210':
                instance.account = acc
            updated = True
        except Exception:
            pass
            
    # Check if payable account is missing OR is pointing to the root 2110 account
    has_valid_payable = instance.payable_account and getattr(instance.payable_account, 'code', None) != '2110'
    if instance.is_supplier and not has_valid_payable:
        try:
            parent_acc = _get_system_account('2110')
            acc = Account.objects.create(
                code=f"{parent_acc.code}-{instance.id}",
                name=f"{instance.name}",
                account_type=parent_acc.account_type,
                parent=parent_acc
            )
            instance.payable_account = acc
            if not instance.account or getattr(instance.account, 'code', None) == '2110':
                instance.account = acc
            updated = True
        except Exception:
            pass
            
    if updated:
        instance.save(update_fields=['receivable_account', 'payable_account', 'account'])
