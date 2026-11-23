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
    sector = models.ForeignKey(
        'pricing.CustomerSector',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='partners',
        verbose_name='قطاع العميل',
        help_text='اختياري: لتطبيق قائمة أسعار القطاع على العميل تلقائياً'
    )

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

    # الرصيد الافتتاحي (أول مدة)
    OPENING_DEBIT = 'DEBIT'
    OPENING_CREDIT = 'CREDIT'
    OPENING_TYPE_CHOICES = [
        (OPENING_DEBIT, 'مدين (عليه مديونية لنا)'),
        (OPENING_CREDIT, 'دائن (له مستحقات علينا)'),
    ]

    opening_balance = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='رصيد أول مدة'
    )
    opening_balance_type = models.CharField(
        max_length=10, choices=OPENING_TYPE_CHOICES, default=OPENING_DEBIT,
        verbose_name='طبيعة رصيد أول مدة'
    )
    opening_balance_date = models.DateField(
        null=True, blank=True,
        verbose_name='تاريخ رصيد أول مدة'
    )
    opening_journal_entry = models.OneToOneField(
        'accounting.JournalEntry',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='partner_opening_balance',
        verbose_name='قيد الرصيد الافتتاحي'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'شريك'
        verbose_name_plural = 'الشركاء'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} [{self.get_partner_type_display()}]'

    @property
    def customer_balance(self):
        """
        صافي رصيد العميل (مديونية العميل لنا):
        رصيد أول مدة (مدين بالموجب، دائن بالسالب)
        + المبيعات الآجلة + الجزء الآجل من السداد المقسم - المرتجعات الآجلة - مقبوضات السندات والدفعات.
        موجب: العميل مدين لنا بمبلغ.
        سالب: العميل دائن (له رصيد لدينا أو دفع مقدماً).
        """
        if hasattr(self, '_cached_customer_balance'):
            return self._cached_customer_balance

        from decimal import Decimal
        from django.db.models import Sum

        net_opening = Decimal('0')
        if self.is_customer and self.opening_balance:
            if self.partner_type == self.CUSTOMER or self.opening_balance_type == self.OPENING_DEBIT:
                if self.opening_balance_type == self.OPENING_DEBIT:
                    net_opening = self.opening_balance
                else:
                    net_opening = -self.opening_balance

        sales = self.invoices.filter(invoice_type='SALE', payment_type='CREDIT', status='POSTED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        split_sales = self.invoices.filter(invoice_type='SALE', payment_type='SPLIT', status='POSTED').aggregate(s=Sum('paid_credit'))['s'] or Decimal('0')

        returns = self.invoices.filter(invoice_type='RETURN_SALE', payment_type='CREDIT', status='POSTED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        split_returns = self.invoices.filter(invoice_type='RETURN_SALE', payment_type='SPLIT', status='POSTED').aggregate(s=Sum('paid_credit'))['s'] or Decimal('0')

        receipts = self.payments.filter(payment_type='RECEIPT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Decimal('0')
        voucher_receipts = self.vouchers.filter(voucher_type='RECEIPT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Decimal('0')

        # شيكات القبض المستلمة من العميل (غير المرتدة والملغاة)
        chq_receipts = self.cheques.filter(
            cheque_type='RECEIVABLE'
        ).exclude(
            status__in=['BOUNCED', 'RETURNED_TO_DRAWER']
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0')

        res = net_opening + (sales + split_sales) - (returns + split_returns) - (receipts + voucher_receipts + chq_receipts)
        self._cached_customer_balance = res
        return res

    @property
    def supplier_balance(self):
        """
        صافي رصيد المورد (مستحقات المورد علينا):
        رصيد أول مدة (دائن بالموجب، مدين بالسالب)
        + المشتريات الآجلة + الجزء الآجل من السداد المقسم - مرتجعات المشتريات الآجلة - مدفوعات السندات والدفعات وشيكات الدفع.
        موجب: نحن مدينون للمورد (المورد دائن لنا).
        سالب: نحن دائنون للمورد (دفعنا له بالزيادة).
        """
        if hasattr(self, '_cached_supplier_balance'):
            return self._cached_supplier_balance

        from decimal import Decimal
        from django.db.models import Sum

        net_opening = Decimal('0')
        if self.is_supplier and self.opening_balance:
            if self.partner_type == self.SUPPLIER or self.opening_balance_type == self.OPENING_CREDIT:
                if self.opening_balance_type == self.OPENING_CREDIT:
                    net_opening = self.opening_balance
                else:
                    net_opening = -self.opening_balance

        purchases = self.invoices.filter(invoice_type='PURCHASE', payment_type='CREDIT', status='POSTED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        split_purchases = self.invoices.filter(invoice_type='PURCHASE', payment_type='SPLIT', status='POSTED').aggregate(s=Sum('paid_credit'))['s'] or Decimal('0')

        pur_returns = self.invoices.filter(invoice_type='RETURN_PURCHASE', payment_type='CREDIT', status='POSTED').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        split_pur_returns = self.invoices.filter(invoice_type='RETURN_PURCHASE', payment_type='SPLIT', status='POSTED').aggregate(s=Sum('paid_credit'))['s'] or Decimal('0')

        payments = self.payments.filter(payment_type='PAYMENT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Decimal('0')
        voucher_payments = self.vouchers.filter(voucher_type='PAYMENT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Decimal('0')

        # شيكات الدفع الصادرة للمورد (غير الملغاة)
        chq_payments = self.cheques.filter(
            cheque_type='PAYABLE', status__in=['ISSUED', 'CLEARED']
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0')

        res = net_opening + (purchases + split_purchases) - (pur_returns + split_pur_returns) - (payments + voucher_payments + chq_payments)
        self._cached_supplier_balance = res
        return res

    @property
    def outstanding_balance(self):
        """
        الرصيد المعلق للشريك:
        - للعميل: customer_balance (موجب = مدين لنا).
        - للمورد: supplier_balance (موجب = مستحق له علينا).
        - للعميل والمورد معاً (BOTH): صافي الرصيد العام (customer_balance - supplier_balance).
        """
        if self.partner_type == self.CUSTOMER:
            return self.customer_balance
        elif self.partner_type == self.SUPPLIER:
            return self.supplier_balance
        else:
            return self.customer_balance - self.supplier_balance

    @property
    def is_customer(self):
        return self.partner_type in (self.CUSTOMER, self.BOTH)

    @property
    def is_supplier(self):
        return self.partner_type in (self.SUPPLIER, self.BOTH)

    def sync_opening_journal_entry(self):
        """
        إنشاء أو تحديث قيد اليومية الافتتاحي للشريك مع حساب الأرصدة الافتتاحية (3999).
        """
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        from apps.tenant.services.journal_service import _get_system_account
        from django.utils import timezone
        from decimal import Decimal

        partner_acc = None
        if self.opening_balance_type == self.OPENING_DEBIT:
            partner_acc = self.receivable_account or self.account
        else:
            partner_acc = self.payable_account or self.account

        if not self.opening_balance or self.opening_balance <= Decimal('0') or not partner_acc:
            if self.opening_journal_entry:
                old_entry = self.opening_journal_entry
                Partner.objects.filter(id=self.id).update(opening_journal_entry=None)
                self.opening_journal_entry = None
                old_entry.delete()
            return

        equity_acc = _get_system_account('3999') or _get_system_account('3100')
        if not equity_acc:
            return

        op_date = self.opening_balance_date or (self.created_at.date() if self.created_at else timezone.now().date())
        ref = f"OPENING-PRT-{self.id}"

        entry = self.opening_journal_entry
        if not entry:
            entry = JournalEntry.objects.filter(reference=ref).first()

        if not entry:
            entry = JournalEntry.objects.create(
                date=op_date,
                reference=ref,
                description=f"رصيد افتتاحي - {self.name} ({self.get_partner_type_display()})",
                status=JournalEntry.POSTED
            )
        else:
            entry.date = op_date
            entry.description = f"رصيد افتتاحي - {self.name} ({self.get_partner_type_display()})"
            entry.status = JournalEntry.POSTED
            entry.save()
            entry.items.all().delete()

        amt = self.opening_balance
        if self.opening_balance_type == self.OPENING_DEBIT:
            # Dr Partner Account | Cr Equity Account
            JournalItem.objects.create(entry=entry, account=partner_acc, debit=amt, credit=0, description=f"رصيد افتتاحي مدين - {self.name}")
            JournalItem.objects.create(entry=entry, account=equity_acc, debit=0, credit=amt, description=f"تسوية رصيد افتتاحي - {self.name}")
        else:
            # Dr Equity Account | Cr Partner Account
            JournalItem.objects.create(entry=entry, account=equity_acc, debit=amt, credit=0, description=f"تسوية رصيد افتتاحي - {self.name}")
            JournalItem.objects.create(entry=entry, account=partner_acc, debit=0, credit=amt, description=f"رصيد افتتاحي دائن - {self.name}")

        if self.opening_journal_entry_id != entry.id:
            Partner.objects.filter(id=self.id).update(opening_journal_entry=entry)
            self.opening_journal_entry = entry


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
import logging

partner_logger = logging.getLogger(__name__)

@receiver(post_save, sender=Partner)
def create_partner_account(sender, instance, created, **kwargs):
    from apps.tenant.accounting.models import Account
    from apps.tenant.services.journal_service import _get_system_account
    
    fields_to_update = {}
    
    # 1. Receivable account for customers (Parent: 1130 ذمم مدينة - عملاء)
    has_valid_receivable = instance.receivable_account and getattr(instance.receivable_account, 'code', None) not in ('1130', '1210')
    if instance.is_customer:
        if not has_valid_receivable:
            try:
                parent_acc = _get_system_account('1130')
                code = f"1130-{instance.id}"
                acc_name = f"{instance.name} (عميل)" if instance.partner_type == Partner.BOTH else instance.name
                acc, _ = Account.objects.get_or_create(
                    code=code,
                    defaults={
                        'name': acc_name,
                        'account_type': parent_acc.account_type,
                        'parent': parent_acc,
                        'allow_reconcile': True
                    }
                )
                fields_to_update['receivable_account'] = acc
                if not instance.account:
                    fields_to_update['account'] = acc
            except Exception as e:
                partner_logger.error(f"Error creating receivable account for {instance.name}: {e}")
        else:
            # Sync account name if partner was renamed
            expected_name = f"{instance.name} (عميل)" if instance.partner_type == Partner.BOTH else instance.name
            if instance.receivable_account and instance.receivable_account.name != expected_name:
                instance.receivable_account.name = expected_name
                instance.receivable_account.save(update_fields=['name'])

    # 2. Payable account for suppliers (Parent: 2110 ذمم دائنة - موردون)
    has_valid_payable = instance.payable_account and getattr(instance.payable_account, 'code', None) != '2110'
    if instance.is_supplier:
        if not has_valid_payable:
            try:
                parent_acc = _get_system_account('2110')
                code = f"2110-{instance.id}"
                acc_name = f"{instance.name} (مورد)" if instance.partner_type == Partner.BOTH else instance.name
                acc, _ = Account.objects.get_or_create(
                    code=code,
                    defaults={
                        'name': acc_name,
                        'account_type': parent_acc.account_type,
                        'parent': parent_acc,
                        'allow_reconcile': True
                    }
                )
                fields_to_update['payable_account'] = acc
                if not instance.account or ('account' not in fields_to_update and instance.partner_type == Partner.SUPPLIER):
                    fields_to_update['account'] = acc
            except Exception as e:
                partner_logger.error(f"Error creating payable account for {instance.name}: {e}")
        else:
            # Sync account name if partner was renamed
            expected_name = f"{instance.name} (مورد)" if instance.partner_type == Partner.BOTH else instance.name
            if instance.payable_account and instance.payable_account.name != expected_name:
                instance.payable_account.name = expected_name
                instance.payable_account.save(update_fields=['name'])

    # Ensure general account is set
    if not instance.account or getattr(instance.account, 'code', None) in ('1130', '1210', '2110'):
        if 'receivable_account' in fields_to_update:
            fields_to_update['account'] = fields_to_update['receivable_account']
        elif instance.receivable_account:
            fields_to_update['account'] = instance.receivable_account
        elif 'payable_account' in fields_to_update:
            fields_to_update['account'] = fields_to_update['payable_account']
        elif instance.payable_account:
            fields_to_update['account'] = instance.payable_account

    if fields_to_update:
        Partner.objects.filter(id=instance.id).update(**fields_to_update)
        for f, val in fields_to_update.items():
            setattr(instance, f, val)

    try:
        instance.sync_opening_journal_entry()
    except Exception as e:
        partner_logger.error(f"Error syncing opening journal entry for {instance.name}: {e}")

