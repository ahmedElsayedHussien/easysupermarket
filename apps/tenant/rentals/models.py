import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.tenant.partners.models import Partner
from apps.tenant.accounting.models import Treasury, EWallet, JournalEntry

ZERO = Decimal('0')


def _get_or_create_account(code, name, account_type, parent_code=None):
    """
    Return the account with this code, seeding the standard chart first (via
    _get_system_account) and creating it under parent_code as a last resort.
    """
    from apps.tenant.accounting.models import Account
    acc = Account.objects.filter(code=code).first()
    if acc:
        return acc
    try:
        from apps.tenant.services.journal_service import _get_system_account
        with transaction.atomic():  # savepoint: a failing seed must not abort the outer transaction
            acc = _get_system_account(code)
        if acc:
            return acc
    except Exception:
        pass
    parent = None
    if parent_code:
        parent = Account.objects.filter(code=parent_code).first()
        if parent is None:
            try:
                from apps.tenant.services.journal_service import _get_system_account
                with transaction.atomic():
                    parent = _get_system_account(parent_code)
            except Exception:
                parent = None
    acc, _ = Account.objects.get_or_create(
        code=code,
        defaults={'name': name, 'account_type': account_type, 'parent': parent, 'is_active': True},
    )
    return acc


def _money_account(treasury, ewallet):
    """The ledger account of the cash box / e-wallet that receives or pays (created if missing)."""
    holder = treasury or ewallet
    acc = holder.ensure_account() if holder is not None else None
    if acc is None:
        raise ValidationError('الخزينة أو المحفظة مش مربوطة بحساب في الشجرة.')
    return acc


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
    purchase_cost = models.DecimalField(max_digits=18, decimal_places=2, default=0, verbose_name='تكلفة المعدة (رصيد افتتاحي)')
    daily_rate = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='سعر الإيجار اليومي')
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
        from apps.tenant.accounting.models import Account
        from apps.tenant.accounting.posting import post_entry

        cost = Decimal(str(self.purchase_cost or 0))
        if cost <= 0:
            return
        with transaction.atomic():
            # locked re-check: two saves at once never book the opening balance twice
            locked = Equipment.objects.select_for_update(of=('self',)).get(pk=self.pk)
            if locked.opening_journal_entry_id:
                return
            # 1. Rental equipment is a FIXED asset → 1215 under 1200 (أصول ثابتة).
            #    Older versions created '1110-EQ' under 1110 (cash) by mistake: if it
            #    exists, keep its balance but move it under fixed assets.
            fixed_assets_parent = _get_or_create_account('1200', 'أصول ثابتة', Account.ASSET, '1000')
            from apps.tenant.accounting.account_mapping import mapped_account
            asset_account = mapped_account('1215') or Account.objects.filter(code='1215').first()
            if not asset_account:
                legacy = Account.objects.filter(code='1110-EQ').first()
                if legacy:
                    if legacy.parent_id != fixed_assets_parent.id:
                        legacy.parent = fixed_assets_parent
                        legacy.save()
                    asset_account = legacy
                else:
                    asset_account = _get_or_create_account('1215', 'معدات التأجير', Account.ASSET, '1200')
            # 2. 3999 = Opening balances & capital adjustments (standard chart)
            equity_account = _get_or_create_account('3999', 'أرصدة افتتاحية وتسويات رأس المال', Account.EQUITY, '3000')
            if not asset_account or not equity_account:
                return  # Can't post
            desc = f'رصيد افتتاحي لمعدة الإيجار: {self.name} ({self.code})'
            entry = post_entry(date=timezone.localdate(), reference=f'OPENING-EQ-{self.pk}', description=desc,
                               lines=[(asset_account, cost, ZERO, desc), (equity_account, ZERO, cost, desc)],
                               user=user, source=self)
            self.opening_journal_entry = entry
            Equipment.objects.filter(pk=self.pk).update(opening_journal_entry=entry)


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

    daily_rate = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='سعر اليوم (وقت الإيجار)')
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='إجمالي المبلغ', blank=True, null=True)
    refund_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0, verbose_name='المبلغ المسترد')
    penalty_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0, verbose_name='غرامة تأخير / تلف')

    payment_method = models.CharField(max_length=10, choices=PAYMENT_CHOICES, default=PAYMENT_CASH, verbose_name='طريقة الدفع')
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, blank=True, null=True, verbose_name='الخزينة (للكاش)')
    ewallet = models.ForeignKey(EWallet, on_delete=models.PROTECT, blank=True, null=True, verbose_name='المحفظة (للكاش)')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE, verbose_name='حالة العقد')

    accrual_journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name='rental_accrual', blank=True, null=True, verbose_name='قيد الاستحقاق')
    payment_journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name='rental_payment', blank=True, null=True, verbose_name='قيد السداد')
    penalty_journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name='rental_penalty', blank=True, null=True, verbose_name='قيد الغرامة')
    refund_journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name='rental_refund', blank=True, null=True, verbose_name='قيد المرتجع')
    settlement_journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name='rental_settlement', blank=True, null=True, verbose_name='قيد التسوية النقدية')

    pos_session = models.ForeignKey('invoicing.POSSession', on_delete=models.SET_NULL, null=True, blank=True, related_name='rentals', verbose_name='وردية نقطة البيع')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name='بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # figures fixed by the accrual entry: never changed once it is posted (fix by a new contract)
    POSTED_FIELDS = ('equipment_id', 'customer_id', 'start_date', 'days_rented', 'daily_rate', 'total_amount',
                     'payment_method', 'treasury_id', 'ewallet_id')

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = Rental.objects.filter(pk=self.pk).values('accrual_journal_entry_id', *self.POSTED_FIELDS).first()
            if old and old['accrual_journal_entry_id']:
                if any(old[f] != getattr(self, f) for f in self.POSTED_FIELDS):
                    raise ValidationError('العقد اترحّل — مبلغه وبياناته المالية متتعدّلش.')
                return super().save(*args, **kwargs)
        if not self.daily_rate:
            self.daily_rate = self.equipment.daily_rate
        if self.start_date and self.days_rented:
            self.end_date = self.start_date + datetime.timedelta(days=self.days_rented)
            self.total_amount = self.daily_rate * self.days_rented
        super().save(*args, **kwargs)

    def get_rental_revenue_account(self):
        from apps.tenant.accounting.models import Account
        acc = Account.objects.filter(name='إيراد إيجار معدات').first()
        if acc:
            return acc
        # Fixed code 4140 under revenue root 4000
        return _get_or_create_account('4140', 'إيرادات تأجير المعدات', Account.REVENUE, '4000')

    def _customer_account(self):
        acc = self.customer.receivable_account or self.customer.account
        if not acc:
            raise ValidationError('العميل ليس لديه حساب ذمم مربوط. يُرجى تهيئة الشريك مالياً أولاً من شاشة العملاء.')
        return acc

    def _post(self, suffix, description, lines, user, date=None):
        from apps.tenant.accounting.posting import post_entry
        return post_entry(date=date or timezone.localdate(), reference=f'RNT-{self.pk}-{suffix}',
                          description=description, lines=lines, user=user or self.created_by, source=self)

    def post_rental(self, user=None):
        """Book the contract: Dr customer / Cr rental revenue, and for cash Dr cash box / Cr customer.
        One posting per contract; the equipment must be available (both rows locked)."""
        with transaction.atomic():
            locked = Rental.objects.select_for_update(of=('self',)).get(pk=self.pk)
            if locked.accrual_journal_entry_id:
                return  # Already posted
            equipment = Equipment.objects.select_for_update(of=('self',)).get(pk=self.equipment_id)
            if equipment.status != Equipment.STATUS_AVAILABLE or not equipment.is_active:
                raise ValidationError(f'المعدة ({equipment.name}) مش متاحة للتأجير دلوقتي.')
            if not self.days_rented or self.days_rented <= 0:
                raise ValidationError('عدد أيام الإيجار لازم يكون يوم أو أكتر.')
            total = Decimal(str(self.total_amount or 0))
            if total <= 0:
                raise ValidationError('إجمالي العقد لازم يكون أكبر من صفر.')
            customer_account = self._customer_account()
            revenue_account = self.get_rental_revenue_account()
            if not revenue_account:
                raise ValidationError('تعذر إنشاء أو العثور على حساب إيرادات التأجير في شجرة الحسابات.')
            payment_account = None
            if self.payment_method == self.PAYMENT_CASH:
                if not self.treasury_id and not self.ewallet_id:
                    raise ValidationError('يجب تحديد خزينة أو محفظة للدفع الكاش.')
                payment_account = _money_account(self.treasury, self.ewallet)

            desc = f'إثبات إيراد تأجير المعدة ({equipment.name}) للعميل {self.customer.name}'
            self.accrual_journal_entry = self._post('ACR', desc, [(customer_account, total, ZERO, desc),
                                                                 (revenue_account, ZERO, total, desc)],
                                                    user, date=self.start_date)
            if payment_account is not None:
                desc = f'سداد إيجار المعدة ({equipment.name}) نقداً من العميل {self.customer.name}'
                self.payment_journal_entry = self._post('PAY', desc, [(payment_account, total, ZERO, desc),
                                                                     (customer_account, ZERO, total, desc)],
                                                        user, date=self.start_date)
            equipment.status = Equipment.STATUS_RENTED
            equipment.save(update_fields=['status', 'updated_at'])
            self.equipment = equipment
            Rental.objects.filter(pk=self.pk).update(accrual_journal_entry=self.accrual_journal_entry,
                                                     payment_journal_entry=self.payment_journal_entry)

    def return_equipment(self, refund_amount=0, refund_method='none', treasury=None, ewallet=None, penalty_amount=0,
                         user=None):
        """The equipment comes back: optional penalty (Dr customer / Cr 4220), optional refund of unused days
        (Dr revenue / Cr customer), and the net paid / collected in cash. Booked under ``user`` (who received it)."""
        if self.status == self.STATUS_RETURNED:
            raise ValidationError('هذه المعدة تم إرجاعها مسبقاً.')
        try:
            refund_amount = Decimal(str(refund_amount if refund_amount not in (None, '') else 0))
            penalty_amount = Decimal(str(penalty_amount if penalty_amount not in (None, '') else 0))
        except (InvalidOperation, ValueError, TypeError):
            raise ValidationError('قيمة المرتجع أو الغرامة غير صالحة.')
        if not refund_amount.is_finite() or not penalty_amount.is_finite():
            raise ValidationError('قيمة المرتجع أو الغرامة غير صالحة.')
        if penalty_amount < 0:
            raise ValidationError('مبلغ الغرامة لا يمكن أن يكون سالباً.')
        if refund_amount < 0:
            raise ValidationError('مبلغ المرتجع لا يمكن أن يكون سالباً.')
        if refund_method not in ('none', 'cash', 'account_balance'):
            raise ValidationError('طريقة التسوية غير صالحة.')
        from apps.tenant.accounting.models import Account

        with transaction.atomic():
            # Lock the contract row and re-check status to avoid double posting on double submit
            locked = Rental.objects.select_for_update(of=('self',)).get(pk=self.pk)
            if locked.status != self.STATUS_ACTIVE:
                raise ValidationError('هذه المعدة تم إرجاعها مسبقاً.')
            total_amount = Decimal(str(locked.total_amount or 0))
            if refund_amount > total_amount:
                raise ValidationError(f'مبلغ المرتجع ({refund_amount}) لا يمكن أن يتجاوز إجمالي العقد ({total_amount}).')
            net_amount = refund_amount - penalty_amount
            payment_account = None
            if refund_method == 'cash' and net_amount != 0:
                if not treasury and not ewallet:
                    raise ValidationError('يجب تحديد خزينة أو محفظة للسداد النقدي.')
                payment_account = _money_account(treasury, ewallet)

            customer_account = self._customer_account()
            revenue_account = self.get_rental_revenue_account()
            name = self.equipment.name
            fields = {'status': self.STATUS_RETURNED, 'refund_amount': refund_amount, 'penalty_amount': penalty_amount}

            if penalty_amount > 0:
                penalty_revenue_acc = _get_or_create_account('4220', 'إيرادات غرامات وتأخيرات', Account.REVENUE, '4000')
                desc = f'غرامة تأخير/تلف معدة ({name}) على العميل {self.customer.name} | مبلغ الغرامة: {penalty_amount} ج.م'
                fields['penalty_journal_entry'] = self._post('PEN', desc, [(customer_account, penalty_amount, ZERO, desc),
                                                                          (penalty_revenue_acc, ZERO, penalty_amount, desc)],
                                                             user)
            if refund_amount > 0:
                desc = (f'تسوية إيراد مبكر وإرجاع معدة ({name}) للعميل {self.customer.name} | '
                        f'مبلغ المرتجع: {refund_amount} ج.م')
                fields['refund_journal_entry'] = self._post('REF', desc, [(revenue_account, refund_amount, ZERO, desc),
                                                                         (customer_account, ZERO, refund_amount, desc)],
                                                            user)
            if payment_account is not None:
                if net_amount > 0:
                    desc = f'سداد صافي مستحقات إرجاع معدة ({name}) للعميل {self.customer.name} | الصافي: {net_amount} ج.م'
                    lines = [(customer_account, net_amount, ZERO, desc), (payment_account, ZERO, net_amount, desc)]
                else:
                    desc = (f'تحصيل صافي غرامة إرجاع معدة ({name}) من العميل {self.customer.name} | '
                            f'الصافي: {abs(net_amount)} ج.م')
                    lines = [(payment_account, abs(net_amount), ZERO, desc), (customer_account, ZERO, abs(net_amount), desc)]
                fields['settlement_journal_entry'] = self._post('SET', desc, lines, user)

            Equipment.objects.filter(pk=self.equipment_id).update(status=Equipment.STATUS_AVAILABLE,
                                                                  updated_at=timezone.now())
            self.equipment.status = Equipment.STATUS_AVAILABLE
            Rental.objects.filter(pk=self.pk).update(updated_at=timezone.now(), **fields)
            for k, v in fields.items():
                setattr(self, k, v)

    def __str__(self):
        return f"عقد إيجار {self.id} - {self.equipment.name}"
