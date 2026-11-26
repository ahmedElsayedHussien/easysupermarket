"""
Partners models: Partner (Customer/Supplier) and Payment.

Partner is a unified model for both customers and suppliers.
Payment tracks cash/card/bank transactions and links to the journal.
"""
from django.db import models, transaction
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
    # The customer decides the taxes of his invoices (a sales rep can't switch them off or on).
    subject_to_vat = models.BooleanField(default=True, verbose_name='يخضع لضريبة القيمة المضافة')
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('14'),
                                   verbose_name='نسبة القيمة المضافة %')
    subject_to_withholding_tax = models.BooleanField(
        default=False, verbose_name='يخضع لضريبة الخصم والإضافة'
    )
    wht_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('1'),
                                   verbose_name='نسبة الخصم والإضافة %')
    address = models.TextField(blank=True, verbose_name='العنوان (الشارع)')
    eta_governate = models.CharField(max_length=100, blank=True, default='', verbose_name='المحافظة')
    eta_region_city = models.CharField(max_length=100, blank=True, default='', verbose_name='المدينة / المنطقة')
    eta_building_number = models.CharField(max_length=50, blank=True, default='', verbose_name='رقم المبنى')
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

    @staticmethod
    def clean_identity(tax_id, national_id):
        """(tax_id, national_id) without spaces / dashes; ValidationError when not 9 / 14 digits."""
        from django.core.exceptions import ValidationError
        tax_id = ''.join(ch for ch in (tax_id or '') if ch not in ' -')
        national_id = ''.join(ch for ch in (national_id or '') if ch not in ' -')
        if tax_id and not (tax_id.isdigit() and len(tax_id) == 9):
            raise ValidationError('الرقم الضريبي لازم يكون 9 أرقام.')
        if national_id and not (national_id.isdigit() and len(national_id) == 14):
            raise ValidationError('الرقم القومي لازم يكون 14 رقم.')
        return tax_id, national_id

    def save(self, *args, **kwargs):
        created = self._state.adding
        super().save(*args, **kwargs)
        # explicit (no signal): sub-accounts under 1130 / 2110 + the opening-balance entry
        create_partner_account(Partner, self, created)

    @property
    def customer_balance(self):
        """رصيد العميل من دفتر الأستاذ (حسابه 1130-x): مدين − دائن. موجب = عليه لنا، سالب = له رصيد عندنا.
        قيد يدوي على حسابه (إعدام دين، خصم مسموح به، مقاصة…) بيتحسب — زي ميزان المراجعة بالظبط."""
        if not hasattr(self, '_cached_customer_balance'):
            from .balances import ledger_balance_map
            self._cached_customer_balance, self._cached_supplier_balance = ledger_balance_map([self])[self.pk]
        return self._cached_customer_balance

    @property
    def supplier_balance(self):
        """رصيد المورد من دفتر الأستاذ (حسابه 2110-x): دائن − مدين. موجب = له علينا، سالب = دفعنا له زيادة."""
        if not hasattr(self, '_cached_supplier_balance'):
            from .balances import ledger_balance_map
            self._cached_customer_balance, self._cached_supplier_balance = ledger_balance_map([self])[self.pk]
        return self._cached_supplier_balance

    def document_balances(self):
        """(customer, supplier) balance re-added from the documents (the old way) — only to CHECK the ledger
        (manage.py partner_ledger_check). The ledger is the reference."""
        from django.db.models import Sum
        from apps.tenant.accounting.models import EServiceTransaction
        Z = Decimal('0')
        cust = supp = Z
        if self.is_customer:
            opening = Z
            if self.opening_balance and (self.partner_type == self.CUSTOMER
                                         or self.opening_balance_type == self.OPENING_DEBIT):
                opening = self.opening_balance if self.opening_balance_type == self.OPENING_DEBIT else -self.opening_balance
            inv = _credit_invoice_sums(self, 'SALE', 'RETURN_SALE')
            receipts = (self.payments.filter(payment_type='RECEIPT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Z) \
                + (self.vouchers.filter(voucher_type='RECEIPT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Z) \
                + (self.cheques.filter(cheque_type='RECEIVABLE').exclude(status__in=['BOUNCED', 'CANCELLED'])
                   .aggregate(s=Sum('amount'))['s'] or Z)
            cust = opening + inv['doc'] + inv['doc_split'] - inv['ret'] - inv['ret_split'] - receipts
        if self.is_supplier:
            opening = Z
            if self.opening_balance and (self.partner_type == self.SUPPLIER
                                         or self.opening_balance_type == self.OPENING_CREDIT):
                opening = self.opening_balance if self.opening_balance_type == self.OPENING_CREDIT else -self.opening_balance
            inv = _credit_invoice_sums(self, 'PURCHASE', 'RETURN_PURCHASE')
            paid = (self.payments.filter(payment_type='PAYMENT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Z) \
                + (self.vouchers.filter(voucher_type='PAYMENT', status='POSTED').aggregate(s=Sum('amount'))['s'] or Z) \
                + (self.cheques.filter(cheque_type='PAYABLE', status__in=['ISSUED', 'CLEARED'])
                   .aggregate(s=Sum('amount'))['s'] or Z)
            es = self.eservice_recharges.filter(transaction_type=EServiceTransaction.MERCHANT_RECHARGE,
                                                paid_on_credit=True, status=EServiceTransaction.POSTED).aggregate(
                p=Sum('principal_amount'), f=Sum('commission_expense'))
            supp = opening + inv['doc'] + inv['doc_split'] + (es['p'] or Z) + (es['f'] or Z) \
                - inv['ret'] - inv['ret_split'] - paid
        return cust, supp

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

        with transaction.atomic():
            if self.pk:
                # lock the partner row and re-read its entry link: two saves at once can't book two entries
                Partner.objects.select_for_update(of=('self',)).filter(pk=self.pk).first()
                self.refresh_from_db(fields=['opening_journal_entry'])
            self._sync_opening_journal_entry_locked(partner_acc)

    def _sync_opening_journal_entry_locked(self, partner_acc):
        """Keep the opening-balance entry in step with the partner card. A posted entry is never rewritten
        or deleted: when the opening balance changes the old entry is reversed and a new one is posted."""
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        from apps.tenant.services.journal_service import _get_system_account, reverse_entry_if_active, unique_reference
        from django.utils import timezone

        amt = self.opening_balance or Decimal('0')
        wanted = amt > Decimal('0') and partner_acc is not None
        equity_acc = (_get_system_account('3999') or _get_system_account('3100')) if wanted else None
        if wanted and not equity_acc:
            return
        op_date = self.opening_balance_date or (self.created_at.date() if self.created_at else timezone.now().date())
        if self.opening_balance_type == self.OPENING_DEBIT:
            lines = [(partner_acc, amt, Decimal('0'), f"رصيد افتتاحي مدين - {self.name}"),
                     (equity_acc, Decimal('0'), amt, f"تسوية رصيد افتتاحي - {self.name}")]
        else:
            lines = [(equity_acc, amt, Decimal('0'), f"تسوية رصيد افتتاحي - {self.name}"),
                     (partner_acc, Decimal('0'), amt, f"رصيد افتتاحي دائن - {self.name}")]

        old = self.opening_journal_entry
        if old is not None and wanted and old.status == JournalEntry.POSTED and old.date == op_date:
            have = sorted((i.account_id, i.debit, i.credit) for i in old.items.all())
            if have == sorted((a.id, d, c) for a, d, c, _ in lines):
                return                                           # unchanged: nothing to book
        if old is not None:
            reverse_entry_if_active(old, reason=f'تعديل رصيد أول المدة - {self.name}')
            Partner.objects.filter(id=self.id).update(opening_journal_entry=None)
            self.opening_journal_entry = None
        if not wanted:
            return
        entry = JournalEntry.objects.create(
            date=op_date, reference=unique_reference(f"OPENING-PRT-{self.id}"),
            description=f"رصيد افتتاحي - {self.name} ({self.get_partner_type_display()})", status=JournalEntry.DRAFT)
        for account, debit, credit, desc in lines:
            JournalItem.objects.create(entry=entry, account=account, debit=debit, credit=credit, description=desc)
        entry.post()
        Partner.objects.filter(id=self.id).update(opening_journal_entry=entry)
        self.opening_journal_entry = entry


# ---------------------------------------------------------------------------
# Payment (legacy: receipts / payments are accounting.Voucher now — kept for old rows)
# ---------------------------------------------------------------------------

def _generate_payment_reference(year=None):
    """
    Generates a unique payment reference like PAY-2024-00001 (race-safe).
    Must be called inside the transaction that saves the Payment (see Payment.save).
    """
    from apps.tenant.core.sequences import next_sequence, max_numeric_suffix
    year = year or datetime.date.today().year
    prefix = f'PAY-{year}-'
    num = next_sequence(f'partner_payment:{prefix}', seed=lambda: max_numeric_suffix(Payment.objects.all(), 'reference', prefix))
    return f'{prefix}{num:05d}'


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

    LOCKED_MSG = 'الدفعة مرحّلة ومينفعش تتعدّل أو تتمسح.'

    def save(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        if self.pk and not self._state.adding:
            old = Payment.objects.filter(pk=self.pk).values('status', 'amount', 'partner_id', 'date',
                                                             'payment_type').first()
            if old and old['status'] == self.POSTED and any(
                    old[f] != getattr(self, f) for f in ('status', 'amount', 'partner_id', 'date', 'payment_type')):
                raise ValidationError(self.LOCKED_MSG)
        if not self.reference:
            from django.db import transaction
            with transaction.atomic():
                self.reference = _generate_payment_reference((self.date or datetime.date.today()).year)
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        if Payment.objects.filter(pk=self.pk, status=self.POSTED).exists():
            raise ValidationError(self.LOCKED_MSG)
        return super().delete(*args, **kwargs)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.amount <= 0:
            raise ValidationError('المبلغ يجب أن يكون أكبر من صفر.')


import logging

partner_logger = logging.getLogger(__name__)

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
    except Exception:
        # sync_opening_journal_entry is atomic: on failure nothing is left half-written
        partner_logger.exception('Error syncing opening journal entry for partner %s (%s)', instance.pk, instance.name)


def _credit_invoice_sums(partner, doc_type, return_type):
    """On-account parts of the partner's posted invoices in ONE query: the full total of
    CREDIT invoices and the credit leg (paid_credit) of SPLIT ones, for the document type
    and its return type."""
    from decimal import Decimal
    from django.db.models import Q, Sum
    posted = partner.invoices.filter(status='POSTED', invoice_type__in=[doc_type, return_type])
    agg = posted.aggregate(
        doc=Sum('total_amount', filter=Q(invoice_type=doc_type, payment_type='CREDIT')),
        doc_split=Sum('paid_credit', filter=Q(invoice_type=doc_type, payment_type='SPLIT')),
        ret=Sum('total_amount', filter=Q(invoice_type=return_type, payment_type='CREDIT')),
        ret_split=Sum('paid_credit', filter=Q(invoice_type=return_type, payment_type='SPLIT')),
    )
    return {k: v or Decimal('0') for k, v in agg.items()}
