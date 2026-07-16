from django.db import models
from apps.tenant.partners.models import Partner
from apps.tenant.accounting.models import Treasury, EWallet, JournalEntry
import datetime

class Equipment(models.Model):
    STATUS_AVAILABLE = 'AVAILABLE'
    STATUS_RENTED = 'RENTED'
    STATUS_MAINTENANCE = 'MAINTENANCE'
    STATUS_CHOICES = [
        (STATUS_AVAILABLE, 'متاحة'),
        (STATUS_RENTED, 'مؤجرة'),
        (STATUS_MAINTENANCE, 'في الصيانة'),
    ]

    name = models.CharField(max_length=200, verbose_name='اسم المعدة')
    code = models.CharField(max_length=50, unique=True, verbose_name='كود المعدة')
    purchase_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='تكلفة المعدة (رصيد افتتاحي)')
    daily_rate = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='سعر الإيجار اليومي')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_AVAILABLE, verbose_name='حالة المعدة')
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    
    opening_journal_entry = models.OneToOneField(
        'accounting.JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='equipment_opening', verbose_name='قيد الرصيد الافتتاحي'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.code})"

    def post_opening_balance(self, user=None):
        from apps.tenant.accounting.models import Account, JournalEntry, JournalItem
        from apps.tenant.services.journal_service import _get_system_account
        from django.db import transaction
        from decimal import Decimal
        import datetime

        cost = Decimal(str(self.purchase_cost))
        if cost <= 0 or self.opening_journal_entry:
            return

        with transaction.atomic():
            # 1. Get or Create Asset Account for Rental Equipment
            try:
                asset_parent = _get_system_account('1110') # Assuming 1110 is Fixed Assets
            except Exception:
                asset_parent = Account.objects.filter(account_type=Account.ASSET).first()
                
            acc_code = f"{asset_parent.code if asset_parent else '1110'}-EQ"
            asset_account = Account.objects.filter(code=acc_code).first()
            if not asset_account and asset_parent:
                asset_account = Account.objects.create(
                    code=acc_code,
                    name='أصول ثابتة - معدات تأجير',
                    account_type=Account.ASSET,
                    parent=asset_parent,
                    is_active=True
                )
            elif not asset_account:
                # Fallback
                asset_account = Account.objects.filter(account_type=Account.ASSET).first()

            # 2. Get Opening Balance Equity Account
            equity_account = None
            try:
                equity_account = _get_system_account('3120') # Usually Retained Earnings or Opening Balances
            except Exception:
                equity_account = Account.objects.filter(account_type=Account.EQUITY, name__icontains='افتتاحي').first()
                if not equity_account:
                    equity_account = Account.objects.filter(account_type=Account.EQUITY).first()

            if not asset_account or not equity_account:
                return # Can't post

            # 3. Create Entry
            entry = JournalEntry.objects.create(
                date=datetime.date.today(),
                description=f'رصيد افتتاحي لمعدة الإيجار: {self.name} ({self.code})',
                created_by=user,
                status=JournalEntry.DRAFT
            )
            
            JournalItem.objects.create(entry=entry, account=asset_account, debit=cost, credit=0)
            JournalItem.objects.create(entry=entry, account=equity_account, debit=0, credit=cost)
            
            entry.post()
            self.opening_journal_entry = entry
            self.save(update_fields=['opening_journal_entry'])

class Rental(models.Model):
    PAYMENT_CASH = 'CASH'
    PAYMENT_CREDIT = 'CREDIT'
    PAYMENT_CHOICES = [
        (PAYMENT_CASH, 'كاش'),
        (PAYMENT_CREDIT, 'آجل'),
    ]

    STATUS_ACTIVE = 'ACTIVE'
    STATUS_RETURNED = 'RETURNED'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'نشط (مؤجرة)'),
        (STATUS_RETURNED, 'تم الإرجاع'),
    ]

    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name='rentals', verbose_name='المعدة')
    customer = models.ForeignKey(Partner, on_delete=models.PROTECT, limit_choices_to={'partner_type__in': [Partner.CUSTOMER, Partner.BOTH]}, related_name='rentals', verbose_name='العميل')
    
    start_date = models.DateField(default=datetime.date.today, verbose_name='تاريخ بداية الإيجار')
    days_rented = models.PositiveIntegerField(verbose_name='عدد أيام الإيجار', default=1)
    end_date = models.DateField(verbose_name='تاريخ العودة المتوقع', blank=True, null=True)
    
    daily_rate = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='سعر اليوم (وقت الإيجار)')
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='إجمالي المبلغ', blank=True, null=True)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='المبلغ المسترد')
    
    payment_method = models.CharField(max_length=10, choices=PAYMENT_CHOICES, default=PAYMENT_CASH, verbose_name='طريقة الدفع')
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, blank=True, null=True, verbose_name='الخزينة (للكاش)')
    ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, blank=True, null=True, verbose_name='المحفظة (للكاش)')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE, verbose_name='حالة العقد')
    
    accrual_journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name='rental_accrual', blank=True, null=True, verbose_name='قيد الاستحقاق')
    payment_journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name='rental_payment', blank=True, null=True, verbose_name='قيد السداد')

    from django.conf import settings
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name='بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.daily_rate:
            self.daily_rate = self.equipment.daily_rate
        if self.start_date and self.days_rented:
            self.end_date = self.start_date + datetime.timedelta(days=self.days_rented)
            self.total_amount = self.daily_rate * self.days_rented
        super().save(*args, **kwargs)

    def get_rental_revenue_account(self):
        from apps.tenant.accounting.models import Account
        # Try to find 'إيراد إيجار معدات'
        acc = Account.objects.filter(name='إيراد إيجار معدات').first()
        if acc:
            return acc
            
        # If not, create it under "إيرادات تشغيلية أخرى"
        parent = Account.objects.filter(name__icontains='إيرادات تشغيلية أخرى').first()
        if not parent:
            parent = Account.objects.filter(account_type=Account.REVENUE).first() # Fallback
            
        if parent:
            # Generate a code
            last_child = parent.get_children().order_by('-code').first()
            if last_child:
                try:
                    new_code = str(int(last_child.code) + 1)
                except ValueError:
                    new_code = f"{parent.code}99"
            else:
                new_code = f"{parent.code}1"
            
            acc = Account.objects.create(
                name='إيراد إيجار معدات',
                code=new_code,
                account_type=Account.REVENUE,
                parent=parent,
                is_active=True
            )
            return acc
        return None

    def post_rental(self):
        from apps.tenant.accounting.models import JournalItem, JournalEntry
        from django.db import transaction as db_transaction
        from django.core.exceptions import ValidationError
        
        if self.accrual_journal_entry:
            return # Already posted
            
        customer_account = self.customer.receivable_account or self.customer.account
        if not customer_account:
            raise ValidationError('العميل ليس لديه حساب ذمم مربوط. يُرجى تهيئة الشريك مالياً أولاً من شاشة العملاء.')
            
        revenue_account = self.get_rental_revenue_account()
        if not revenue_account:
            raise ValidationError('تعذر إنشاء أو العثور على حساب إيرادات التأجير في شجرة الحسابات.')

        with db_transaction.atomic():
            # 1. Accrual Entry: Dr Customer | Cr Equipment Rental Revenue
            accrual_entry = JournalEntry.objects.create(
                date=self.start_date,
                description=f'إثبات إيراد تأجير المعدة ({self.equipment.name}) للعميل {self.customer.name}',
                created_by=self.created_by,
                status=JournalEntry.DRAFT
            )
            JournalItem.objects.create(entry=accrual_entry, account=customer_account, debit=self.total_amount, credit=0)
            JournalItem.objects.create(entry=accrual_entry, account=revenue_account, debit=0, credit=self.total_amount)
            accrual_entry.post()
            self.accrual_journal_entry = accrual_entry
            
            # 2. Payment Entry if CASH
            if self.payment_method == self.PAYMENT_CASH:
                if not self.treasury and not self.ewallet:
                    raise ValidationError('يجب تحديد خزينة أو محفظة للدفع الكاش.')
                    
                payment_account = self.treasury.account if self.treasury else self.ewallet.account
                
                payment_entry = JournalEntry.objects.create(
                    date=self.start_date,
                    description=f'سداد إيجار المعدة ({self.equipment.name}) نقداً من العميل {self.customer.name}',
                    created_by=self.created_by,
                    status=JournalEntry.DRAFT
                )
                JournalItem.objects.create(entry=payment_entry, account=payment_account, debit=self.total_amount, credit=0)
                JournalItem.objects.create(entry=payment_entry, account=customer_account, debit=0, credit=self.total_amount)
                payment_entry.post()
                self.payment_journal_entry = payment_entry
            
            # Mark equipment as rented
            self.equipment.status = Equipment.STATUS_RENTED
            self.equipment.save()
            
            self.save()

    def return_equipment(self, refund_amount=0, refund_method='none', treasury=None, ewallet=None):
        from django.core.exceptions import ValidationError
        from django.db import transaction as db_transaction
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        from decimal import Decimal
        import datetime
        
        if self.status == self.STATUS_RETURNED:
            raise ValidationError('هذه المعدة تم إرجاعها مسبقاً.')
        
        with db_transaction.atomic():
            self.status = self.STATUS_RETURNED
            self.equipment.status = Equipment.STATUS_AVAILABLE
            self.equipment.save()
            
            refund_amount = Decimal(str(refund_amount))
            self.refund_amount = refund_amount
            
            if refund_amount > 0 and refund_method != 'none':
                customer_account = self.customer.receivable_account or self.customer.account
                revenue_account = self.get_rental_revenue_account()
                
                # 1. Reverse Revenue Entry
                reverse_entry = JournalEntry.objects.create(
                    date=datetime.date.today(),
                    description=f'تسوية إيراد مبكر وإرجاع معدة ({self.equipment.name}) للعميل {self.customer.name}',
                    created_by=self.created_by,
                    status=JournalEntry.DRAFT
                )
                JournalItem.objects.create(entry=reverse_entry, account=revenue_account, debit=refund_amount, credit=0)
                JournalItem.objects.create(entry=reverse_entry, account=customer_account, debit=0, credit=refund_amount)
                reverse_entry.post()
                
                # 2. Refund Cash Entry
                if refund_method == 'cash':
                    if not treasury and not ewallet:
                        raise ValidationError('يجب تحديد خزينة أو محفظة للدفع الكاش.')
                    payment_account = treasury.account if treasury else ewallet.account
                    
                    refund_entry = JournalEntry.objects.create(
                        date=datetime.date.today(),
                        description=f'رد مبلغ نقدي استرداد إيجار معدة ({self.equipment.name}) للعميل {self.customer.name}',
                        created_by=self.created_by,
                        status=JournalEntry.DRAFT
                    )
                    JournalItem.objects.create(entry=refund_entry, account=customer_account, debit=refund_amount, credit=0)
                    JournalItem.objects.create(entry=refund_entry, account=payment_account, debit=0, credit=refund_amount)
                    refund_entry.post()

            self.save()

    def __str__(self):
        return f"عقد إيجار {self.id} - {self.equipment.name}"
