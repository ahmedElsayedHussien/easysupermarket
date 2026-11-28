"""
Invoicing models: Invoice and InvoiceLine.

Invoice is the master document for both sales (POS/credit) and purchases.
InvoiceLine auto-calculates subtotal and tax on save().

Lifecycle:
  DRAFT → confirm_invoice() → POSTED (triggers FIFO + accounting)
  DRAFT → CANCELLED (no financial effects)
"""
from django.db import models
from apps.tenant.core.money import MoneyField, line_amounts
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from decimal import Decimal
import datetime
from apps.tenant.core.usernames import display_username


# ---------------------------------------------------------------------------
# Auto-generate invoice numbers
# ---------------------------------------------------------------------------

def _generate_invoice_number(invoice_type: str, year: int = None) -> str:
    """
    Generates a sequential invoice number per type and year.
    Format: SALE-2024-00001, PUR-2024-00001, etc.

    Race-safe: uses the locked DocumentSequence counter (Invoice.save() calls this
    inside the same transaction as the INSERT). Seeded from the highest numeric
    suffix already used, so numbering keeps working past 99999 (no text sort).
    """
    from apps.tenant.core.sequences import next_sequence, max_numeric_suffix

    prefix_map = {
        'SALE': 'SALE',
        'PURCHASE': 'PUR',
        'RETURN_SALE': 'RSAL',
        'RETURN_PURCHASE': 'RPUR',
    }
    prefix = prefix_map.get(invoice_type, 'INV')
    year = year or datetime.date.today().year
    full_prefix = f'{prefix}-{year}-'
    num = next_sequence(
        f'INVOICE:{full_prefix}',
        seed=lambda: max_numeric_suffix(Invoice.objects.all(), 'invoice_number', full_prefix),
    )
    return f'{full_prefix}{num:05d}'


# ---------------------------------------------------------------------------
# POS Session (Cashier Shift)
# ---------------------------------------------------------------------------

class POSSession(models.Model):
    """
    Represents a Cashier Shift.
    A cashier opens a session with an initial cash amount in a specific treasury,
    does sales, and then closes the session with a final cash count.
    """
    OPEN = 'OPEN'
    CLOSED = 'CLOSED'
    STATUS_CHOICES = [
        (OPEN, 'مفتوحة'),
        (CLOSED, 'مغلقة'),
    ]

    branch = models.ForeignKey(
        'core.Branch', on_delete=models.PROTECT, related_name='pos_sessions', verbose_name='الفرع'
    )
    treasury = models.ForeignKey(
        'accounting.Treasury', on_delete=models.PROTECT, related_name='pos_sessions', verbose_name='الخزينة/الدرج'
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='pos_sessions', verbose_name='الكاشير'
    )
    start_time = models.DateTimeField(auto_now_add=True, verbose_name='وقت الفتح')
    end_time = models.DateTimeField(null=True, blank=True, verbose_name='وقت الإغلاق')
    
    opening_balance = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name='رصيد الافتتاح (العهدة)'
    )
    closing_balance_expected = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name='الرصيد المتوقع'
    )
    closing_balance_actual = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name='الرصيد الفعلي'
    )
    difference = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name='العجز / الزيادة'
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=OPEN, verbose_name='الحالة'
    )
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    # Ledger entry of the closing difference (shift_services.post_shift_variance)
    variance_entry = models.ForeignKey(
        'accounting.JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='قيد فرق الدرج'
    )
    variance_charged_to = models.CharField(
        max_length=10, blank=True, default='',
        choices=[('EXPENSE', 'مصروف على المحل'), ('CASHIER', 'محمّل على الكاشير')],
        verbose_name='تحميل الفرق'
    )

    class Meta:
        verbose_name = 'وردية نقاط البيع'
        verbose_name_plural = 'ورديات نقاط البيع'
        ordering = ['-start_time']

    def __str__(self):
        return f"وردية {display_username(self.user)} - {self.get_status_display()} ({self.start_time.strftime('%Y-%m-%d %H:%M')})"


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------

class Invoice(models.Model):
    """
    Master document for a sale or purchase transaction.

    For SALE invoices: Confirms → FIFO consumption + COGS entry + revenue entry
    For PURCHASE invoices: Confirms → FIFO batch creation + payable entry
    """
    SALE = 'SALE'
    PURCHASE = 'PURCHASE'
    RETURN_SALE = 'RETURN_SALE'
    RETURN_PURCHASE = 'RETURN_PURCHASE'

    INVOICE_TYPE_CHOICES = [
        (SALE, 'فاتورة مبيعات'),
        (PURCHASE, 'فاتورة مشتريات'),
        (RETURN_SALE, 'مرتجع مبيعات'),
        (RETURN_PURCHASE, 'مرتجع مشتريات'),
    ]

    CASH = 'CASH'
    CREDIT = 'CREDIT'
    CARD = 'CARD'
    EWALLET = 'EWALLET'
    BANK_TRANSFER = 'BANK_TRANSFER'
    SPLIT = 'SPLIT'

    PAYMENT_TYPE_CHOICES = [
        (CASH, 'نقدي'),
        (CREDIT, 'آجل'),
        (CARD, 'بطاقة'),
        (EWALLET, 'محفظة إلكترونية'),
        (BANK_TRANSFER, 'تحويل بنكي'),
        (SPLIT, 'دفع متعدد/مركب'),
    ]

    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    CANCELLED = 'CANCELLED'

    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحّلة'),
        (CANCELLED, 'ملغاة'),
    ]

    invoice_number = models.CharField(
        max_length=30, unique=True, blank=True, verbose_name='رقم الفاتورة'
    )
    e_invoice_number = models.CharField(
        max_length=100, blank=True, null=True, verbose_name='رقم الفاتورة الإلكترونية'
    )
    invoice_type = models.CharField(
        max_length=20, choices=INVOICE_TYPE_CHOICES, verbose_name='نوع الفاتورة'
    )
    partner = models.ForeignKey(
        'partners.Partner',
        on_delete=models.PROTECT,
        related_name='invoices',
        verbose_name='الشريك (عميل/مورد)'
    )
    treasury = models.ForeignKey(
        'accounting.Treasury',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='invoices',
        verbose_name='الخزينة',
        help_text='تستخدم فقط في الفواتير النقدية'
    )
    ewallet = models.ForeignKey(
        'accounting.EWallet',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='invoices',
        verbose_name='المحفظة'
    )
    bank_account = models.ForeignKey(
        'accounting.BankAccount',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='invoices',
        verbose_name='الحساب البنكي'
    )
    insta_bank_account = models.ForeignKey(
        'accounting.BankAccount',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='insta_invoices',
        verbose_name='حساب إنستاباي / التحويل البنكي'
    )
    pos_machine = models.ForeignKey(
        'accounting.POSMachine',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='invoices',
        verbose_name='ماكينة الدفع الإلكتروني',
        help_text='تستخدم فقط في فواتير الدفع بالبطاقة'
    )
    branch = models.ForeignKey(
        'core.Branch',
        on_delete=models.PROTECT,
        related_name='invoices',
        verbose_name='الفرع'
    )
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.PROTECT,
        related_name='invoices',
        verbose_name='المستودع'
    )
    date = models.DateField(verbose_name='تاريخ الفاتورة')
    due_date = models.DateField(null=True, blank=True, verbose_name='تاريخ الاستحقاق')
    payment_type = models.CharField(
        max_length=20, choices=PAYMENT_TYPE_CHOICES, default=CASH,
        verbose_name='طريقة الدفع'
    )
    paid_cash = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='المبلغ النقدي')
    paid_card = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ البطاقة')
    paid_ewallet = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ المحفظة/إنستا باي')
    paid_bank = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='مبلغ التحويل البنكي')
    paid_credit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='المبلغ الآجل')
    # POS cash rounding (SystemSetting.pos_cash_rounding): rounded cash due - exact cash due
    # (+ = collected more than due, - = less). total_amount stays exact; paid_cash is what
    # was actually collected; the difference is posted to account 5115.
    rounding_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'),
                                          verbose_name='فرق تقريب النقدية')

    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة'
    )
    cashier = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cashier_invoices',
        verbose_name='الكاشير'
    )
    pos_session = models.ForeignKey(
        POSSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoices',
        verbose_name='وردية نقطة البيع'
    )
    eta_uuid = models.CharField(max_length=100, blank=True, null=True)
    eta_status = models.CharField(max_length=50, blank=True, null=True)

    sales_rep = models.ForeignKey(
        'invoicing.SalesRep', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='invoices', verbose_name='المندوب'
    )
    purchase_order = models.ForeignKey(
        'invoicing.PurchaseOrder', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='invoices', verbose_name='أمر الشراء'
    )
    parent_invoice = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='return_invoices',
        verbose_name='الفاتورة الأصلية (للمرتجعات)'
    )

    # Financial totals (auto-calculated from lines)
    subtotal = MoneyField(
        default=Decimal('0'),
        verbose_name='إجمالي قبل الضريبة'
    )
    discount_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة الخصم الإجمالي (%)'
    )
    discount_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة الخصم'
    )
    tax_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='ضريبة القيمة المضافة'
    )
    vat_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة ضريبة القيمة المضافة (%)'
    )
    wht_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة ضريبة الخصم والإضافة (%)'
    )
    wht_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة الخصم والإضافة'
    )
    total_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='الإجمالي النهائي'
    )

    # Accounting link
    journal_entry = models.ForeignKey(
        'accounting.JournalEntry',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices',
        verbose_name='القيد المحاسبي'
    )
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    idempotency_key = models.UUIDField(null=True, blank=True, editable=False, verbose_name='مفتاح منع التكرار')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'فاتورة'
        verbose_name_plural = 'الفواتير'
        ordering = ['-date', '-created_at']
        permissions = [
            ('add_purchase_invoice', 'إنشاء فواتير ومرتجعات الشراء'),
            ('add_sales_return', 'عمل مرتجع مبيعات'),
            ('add_sales_return_no_invoice', 'مرتجع مبيعات بدون فاتورة (رصيد أول المدة)'),
            ('cancel_invoice', 'إلغاء فاتورة مرحلة'),
        ]
        indexes = [
            models.Index(fields=['-date', '-created_at'], name='inv_date_created_idx'),
            models.Index(fields=['invoice_type', '-date'], name='inv_type_date_idx'),
            models.Index(fields=['status', '-date'], name='inv_status_date_idx'),
            models.Index(fields=['payment_type', '-date'], name='inv_pay_date_idx'),
            models.Index(fields=['branch', '-date'], name='inv_branch_date_idx'),
            models.Index(fields=['pos_session', 'status'], name='inv_pos_session_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['idempotency_key'],
                condition=models.Q(idempotency_key__isnull=False),
                name='uniq_invoice_idempotency_key',
            ),
        ]

    def __str__(self):
        return f'{self.invoice_number} | {self.get_invoice_type_display()} | {self.total_amount}'

    # figures and parties of a posted invoice: changed only by cancelling it (reversal) or a return
    LOCKED_WHEN_POSTED = ('invoice_type', 'partner_id', 'branch_id', 'warehouse_id', 'date', 'subtotal',
                          'discount_amount', 'tax_amount', 'wht_amount', 'total_amount')
    POSTED_LOCKED_MSG = 'الفاتورة مرحّلة ومينفعش تتعدّل — الغيها أو اعمل مرتجع.'

    def _check_posted_unchanged(self, update_fields=None):
        fields = [f for f in self.LOCKED_WHEN_POSTED
                  if update_fields is None or f in update_fields or f.removesuffix('_id') in update_fields]
        if not fields:
            return
        orig = Invoice.objects.filter(pk=self.pk).values('status', *fields).first()
        if orig and orig['status'] == self.POSTED and any(orig[f] != getattr(self, f) for f in fields):
            raise ValidationError(self.POSTED_LOCKED_MSG)

    def save(self, *args, **kwargs):
        """Auto-generate invoice number if not set."""
        if self.pk and not self._state.adding:
            self._check_posted_unchanged(kwargs.get('update_fields'))

        if not self.invoice_number:
            from django.db import transaction
            # Allocate the number and INSERT in one transaction so the sequence
            # row lock is held until the invoice is committed.
            with transaction.atomic():
                self.invoice_number = _generate_invoice_number(
                    self.invoice_type, (self.date or datetime.date.today()).year)
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.POSTED:
            raise ValidationError('لا يمكن حذف فاتورة مرحّلة.')
        return super().delete(*args, **kwargs)

    @property
    def gross_subtotal(self):
        """Returns gross total before discounts."""
        return (self.subtotal or Decimal('0')) + (self.discount_amount or Decimal('0'))

    @property
    def is_fully_returned(self):
        if self.invoice_type in [self.RETURN_SALE, self.RETURN_PURCHASE]:
            return False
        lines = list(self.lines.all())
        if not lines:
            return False
        return all(line.quantity <= line.returned_quantity for line in lines)

    @property
    def is_partially_returned(self):
        if self.invoice_type in [self.RETURN_SALE, self.RETURN_PURCHASE]:
            return False
        lines = list(self.lines.all())
        if not lines:
            return False
        has_any = any(line.returned_quantity > 0 for line in lines)
        return has_any and not self.is_fully_returned

    def recalculate_totals(self):
        """
        Recalculates and updates subtotal, discount, tax_amount, wht_amount and total_amount
        from all lines.
        """
        from django.db.models import Sum
        
        # Calculate totals from lines
        agg = self.lines.aggregate(
            gross_sum=Sum('subtotal'),
            discount_sum=Sum('discount_amount'),
            tax_sum=Sum('tax_amount'),
            wht_sum=Sum('wht_amount')
        )
        # the header is the sum of the lines, each already money (2 places): exact, nothing to round
        self.subtotal = agg['gross_sum'] or Decimal('0')
        self.discount_amount = agg['discount_sum'] or Decimal('0')
        self.tax_amount = agg['tax_sum'] or Decimal('0')
        self.wht_amount = agg['wht_sum'] or Decimal('0')
        
        self.total_amount = self.subtotal + self.tax_amount - self.wht_amount
        self.save(update_fields=['subtotal', 'discount_amount', 'tax_amount', 'wht_amount', 'total_amount', 'updated_at'])

    def clean(self):
        if self.pk:
            try:
                orig = Invoice.objects.get(pk=self.pk)
                if orig.status == self.POSTED:
                    raise ValidationError('لا يمكن تعديل فاتورة مرحّلة.')
            except Invoice.DoesNotExist:
                pass

        if self.status == self.POSTED:
            if not self.pk or not self.lines.exists():
                raise ValidationError('لا يمكن ترحيل فاتورة بدون بنود.')

        if self.payment_type == self.CREDIT and self.partner_id and getattr(self.partner, 'credit_limit', 0) > 0:
            if self.invoice_type == self.SALE:
                new_balance = self.partner.outstanding_balance + self.total_amount
                if new_balance > self.partner.credit_limit:
                    from apps.tenant.core.decorators import is_admin_user
                    is_manager = bool(self.cashier) and is_admin_user(self.cashier)
                    if not is_manager:
                        raise ValidationError(f'الفاتورة تتجاوز حد الائتمان للعميل. الحد: {self.partner.credit_limit}، الرصيد مع الفاتورة: {new_balance}')


# ---------------------------------------------------------------------------
# InvoiceLine
# ---------------------------------------------------------------------------

class InvoiceLine(models.Model):
    """
    One line item on an Invoice.

    Auto-calculates on save():
      subtotal = quantity * unit_price * (1 - discount_pct/100)
      tax_amount = subtotal * tax_rate / 100

    cogs_amount is filled by the FIFO engine when confirming SALE invoices.
    """
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='lines',
        verbose_name='الفاتورة'
    )
    product = models.ForeignKey(
        'inventory.Product',
        on_delete=models.PROTECT,
        related_name='invoice_lines',
        verbose_name='المنتج'
    )
    serial_item = models.ForeignKey(
        'inventory.SerialItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_lines',
        verbose_name='السيريال (للمنتجات المسروِلة)'
    )
    quantity = models.DecimalField(
        max_digits=18, decimal_places=4, verbose_name='الكمية'
    )
    returned_quantity = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        verbose_name='الكمية المرتجعة مسبقاً'
    )
    original_line = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='return_lines',
        verbose_name='سطر الفاتورة الأصلية (للمرتجعات)'
    )
    unit_price = models.DecimalField(
        max_digits=18, decimal_places=4, verbose_name='سعر الوحدة'
    )
    discount_fixed = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
        verbose_name='خصم بمبلغ ثابت (لو متحدد بيتاخد بدل النسبة)'
    )
    discount_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة الخصم %'
    )
    uom = models.ForeignKey(
        'inventory.ProductUoM',
        null=True,
        on_delete=models.SET_NULL,
        verbose_name='وحدة القياس'
    )
    # the unit's factor when the line was written: returns / cancellations / reports use THIS one even if
    # the product's unit is changed or deleted later (null = old line, falls back to the unit)
    conversion_factor = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True,
                                            verbose_name='معامل التحويل وقت البيع')
    # Optional expiry date entered on the purchase screen (SystemSetting.enable_expiry_tracking);
    # copied onto the InventoryBatch created when the purchase is confirmed.
    expiry_date = models.DateField(null=True, blank=True, verbose_name='تاريخ الصلاحية')
    discount_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة الخصم'
    )

    subtotal = MoneyField(
        default=Decimal('0'),
        verbose_name='الإجمالي (بعد خصم، قبل ضريبة)'
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة الضريبة %'
    )
    tax_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة الضريبة'
    )
    wht_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة ضريبة الخصم والإضافة %'
    )
    wht_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة الخصم والإضافة'
    )
    total_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='الإجمالي النهائي'
    )

    # Filled by FIFO engine during invoice confirmation
    cogs_amount = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        verbose_name='تكلفة البضاعة المباعة'
    )

    class Meta:
        verbose_name = 'بند فاتورة'
        verbose_name_plural = 'بنود الفاتورة'
        indexes = [
            models.Index(fields=['invoice', 'returned_quantity'], name='invline_inv_ret_idx'),
        ]

    def __str__(self):
        return f'{self.product.name} × {self.quantity} @ {self.unit_price}'

    @property
    def factor(self):
        """Base units per one unit of this line (the factor saved on the line, else its unit's)."""
        if self.conversion_factor:
            return self.conversion_factor
        return (self.uom.conversion_factor or Decimal('1')) if self.uom_id and self.uom else Decimal('1')

    @property
    def base_quantity(self):
        return (self.quantity or Decimal('0')) * self.factor

    # after posting only these are written (COGS by the FIFO engine, returned qty by returns)
    POSTED_WRITABLE = frozenset({'cogs_amount', 'returned_quantity'})

    def _invoice_status(self):
        if not self.invoice_id:
            return None
        return Invoice.objects.filter(pk=self.invoice_id).values_list('status', flat=True).first()

    def _check_invoice_open(self, update_fields=None):
        if getattr(self, '_allow_posted_edit', False):
            return
        if self._invoice_status() != Invoice.POSTED:
            return
        if not self._state.adding and update_fields is not None and set(update_fields) <= self.POSTED_WRITABLE:
            return
        raise ValidationError(Invoice.POSTED_LOCKED_MSG)

    def delete(self, *args, **kwargs):
        if not getattr(self, '_allow_posted_edit', False) and self._invoice_status() == Invoice.POSTED:
            raise ValidationError(Invoice.POSTED_LOCKED_MSG)
        return super().delete(*args, **kwargs)

    @property
    def display_unit_name(self):
        if getattr(self, '_custom_display_unit_name', None):
            return self._custom_display_unit_name
        if self.uom and getattr(self.uom, 'uom', None):
            return self.uom.uom.name
        if self.product:
            return getattr(self.product, 'pos_unit_name', None) or self.product.get_unit_display() or ''
        return ''

    @display_unit_name.setter
    def display_unit_name(self, value):
        self._custom_display_unit_name = value

    def save(self, *args, **kwargs):
        """
        Auto-calculate financial figures before saving.

        subtotal = quantity × unit_price × (1 - discount_pct/100)
        discount_amount = quantity × unit_price × (discount_pct/100)
        tax_amount = subtotal × tax_rate/100
        wht_amount = subtotal × wht_rate/100
        total_amount = subtotal + tax_amount - wht_amount
        """
        update_fields = kwargs.get('update_fields')
        self._check_invoice_open(update_fields)
        if update_fields is not None:            # a partial write (cogs / returned qty): figures stay as stored
            return super().save(*args, **kwargs)
        self.conversion_factor = (self.uom.conversion_factor or Decimal('1')) if self.uom_id and self.uom \
            else Decimal('1')
        # money to 2 places, once, from exact inputs (core.money.line_amounts - the van cart uses the same);
        # a fixed discount (e.g. 50 EGP) wins over the %, which is then display only
        a = line_amounts(self.quantity, self.unit_price, discount_pct=self.discount_pct,
                         discount_fixed=self.discount_fixed, tax_rate=self.tax_rate, wht_rate=self.wht_rate)
        self.discount_amount, self.subtotal = a['discount'], a['subtotal']
        self.tax_amount, self.wht_amount, self.total_amount = a['tax'], a['wht'], a['total']
        super().save(*args, **kwargs)

    def clean(self):
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError('الكمية يجب أن تكون أكبر من صفر.')
        if self.unit_price is not None and self.unit_price < 0:
            raise ValidationError('سعر الوحدة لا يمكن أن يكون سالباً.')
        if self.discount_pct is not None and (self.discount_pct < 0 or self.discount_pct > 100):
            raise ValidationError('نسبة الخصم يجب أن تكون بين 0 و 100.')

        try:
            if self.invoice_id and self.product_id and self.quantity:
                if self.invoice.invoice_type in [Invoice.SALE, Invoice.RETURN_PURCHASE]:
                    from apps.tenant.core.models import SystemSetting
                    # Master Lock: Global setting strictly governs negative stock
                    allow_negative = (
                        getattr(SystemSetting.get_settings(), 'allow_negative_stock', False)
                        or getattr(self.product, 'allow_negative_stock', False)
                        or getattr(self.product, 'product_type', None) == 'SERVICE'
                    )
                    if not allow_negative:
                        available = self.product.get_stock(self.invoice.warehouse)
                        if self.base_quantity > available:       # stock is kept in base units
                            from apps.tenant.core.decorators import is_admin_user
                            is_manager = bool(self.invoice.cashier) and is_admin_user(self.invoice.cashier)
                            if not is_manager:
                                raise ValidationError(f'الكمية المطلوبة ({self.base_quantity}) تتجاوز المخزون المتاح ({available}) للمنتج {self.product.name} وغير مسموح بالسحب بالسالب.')
        except ValidationError:
            raise
        except (Invoice.DoesNotExist, AttributeError, TypeError) as exc:
            # Related invoice/warehouse not resolvable yet; the FIFO engine re-validates
            # stock on confirmation, so skipping here is safe.
            import logging
            logging.getLogger(__name__).debug('InvoiceLine.clean stock check skipped: %s', exc)


# ---------------------------------------------------------------------------
# Quotation & QuotationLine (عروض الأسعار)
# ---------------------------------------------------------------------------

def _generate_quotation_number() -> str:
    """
    Generates a sequential quotation number (race-safe, see core.sequences).
    Format: QUOTE-2026-00001
    """
    from apps.tenant.core.sequences import next_sequence, max_numeric_suffix

    year = datetime.date.today().year
    full_prefix = f'QUOTE-{year}-'
    num = next_sequence(
        f'QUOTATION:{full_prefix}',
        seed=lambda: max_numeric_suffix(Quotation.objects.all(), 'quotation_number', full_prefix),
    )
    return f'{full_prefix}{num:05d}'


# Notes prefix written by the legacy confirm_quotation() (which deducted stock on confirm)
LEGACY_QUOTATION_OUT_NOTE = 'ترحيل عرض سعر رقم'
LEGACY_QUOTATION_RESTORE_NOTE = 'استرجاع مخزون عرض سعر'


class Quotation(models.Model):
    """
    Price Offer / Quotation Model.
    Supports tax dimension (VAT, WHT), optional expiry date, stock deduction on posting for non-services,
    editing, deletion, and conversion into a Sales Invoice.
    """
    DRAFT = 'DRAFT'
    POSTED = 'POSTED'
    CONVERTED = 'CONVERTED'
    CANCELLED = 'CANCELLED'

    STATUS_CHOICES = [
        (DRAFT, 'مسودة'),
        (POSTED, 'مرحّل (مؤكد)'),
        (CONVERTED, 'محوّل إلى فاتورة'),
        (CANCELLED, 'ملغى'),
    ]

    quotation_number = models.CharField(
        max_length=30, unique=True, blank=True, verbose_name='رقم عرض السعر'
    )
    partner = models.ForeignKey(
        'partners.Partner',
        on_delete=models.PROTECT,
        related_name='quotations',
        verbose_name='العميل'
    )
    branch = models.ForeignKey(
        'core.Branch',
        on_delete=models.PROTECT,
        related_name='quotations',
        verbose_name='الفرع'
    )
    warehouse = models.ForeignKey(
        'inventory.Warehouse',
        on_delete=models.PROTECT,
        related_name='quotations',
        verbose_name='المستودع'
    )
    date = models.DateField(default=datetime.date.today, verbose_name='تاريخ العرض')
    valid_until = models.DateField(null=True, blank=True, verbose_name='تاريخ انقضاء الصلاحية (اختياري)')
    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default=DRAFT, verbose_name='الحالة'
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='quotations',
        verbose_name='أنشئ بواسطة'
    )
    converted_invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='source_quotations',
        verbose_name='الفاتورة المحول إليها'
    )

    # Financial and Tax Totals
    subtotal = MoneyField(
        default=Decimal('0'),
        verbose_name='إجمالي قبل الضريبة'
    )
    discount_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة الخصم الإجمالي (%)'
    )
    discount_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة الخصم'
    )
    tax_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='ضريبة القيمة المضافة'
    )
    vat_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة ضريبة القيمة المضافة (%)'
    )
    wht_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة ضريبة الخصم والإضافة (%)'
    )
    wht_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة الخصم والإضافة'
    )
    total_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='الإجمالي النهائي'
    )

    notes = models.TextField(blank=True, verbose_name='ملاحظات وشروط العرض')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'عرض سعر'
        verbose_name_plural = 'عروض الأسعار'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.quotation_number} | {self.partner.name} | {self.total_amount}'

    def save(self, *args, **kwargs):
        if not self.quotation_number:
            from django.db import transaction
            with transaction.atomic():
                self.quotation_number = _generate_quotation_number()
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def recalculate_totals(self):
        """Recalculate financial figures and tax totals from lines."""
        from django.db.models import Sum
        agg = self.lines.aggregate(
            gross_sum=Sum('subtotal'),
            discount_sum=Sum('discount_amount'),
            tax_sum=Sum('tax_amount'),
            wht_sum=Sum('wht_amount')
        )
        # the header is the sum of the lines, each already money (2 places): exact, nothing to round
        self.subtotal = agg['gross_sum'] or Decimal('0')
        self.discount_amount = agg['discount_sum'] or Decimal('0')
        self.tax_amount = agg['tax_sum'] or Decimal('0')
        self.wht_amount = agg['wht_sum'] or Decimal('0')

        self.total_amount = self.subtotal + self.tax_amount - self.wht_amount
        self.save(update_fields=['subtotal', 'discount_amount', 'tax_amount', 'wht_amount', 'total_amount', 'updated_at'])

    def confirm_quotation(self):
        """
        Confirms the quotation (DRAFT -> POSTED).

        A quotation is a price offer only: confirming it does NOT move stock and
        does NOT create any accounting entry. Stock is deducted (with COGS) only
        when the quotation is converted into a SALE invoice and that invoice is posted.
        """
        if self.status == self.POSTED:
            return
        if self.status != self.DRAFT:
            raise ValidationError(
                f'لا يمكن تأكيد عرض السعر لأنه في حالة "{self.get_status_display()}".'
            )
        if not self.lines.exists():
            raise ValidationError('لا يمكن تأكيد عرض سعر بدون بنود.')
        self.status = self.POSTED
        self.save(update_fields=['status', 'updated_at'])

    # ------------------------------------------------------------------
    # Legacy stock handling (quotations confirmed by the old logic deducted stock)
    # ------------------------------------------------------------------
    def legacy_stock_movements(self):
        """OUT movements created by the old confirm_quotation() for this quotation."""
        from apps.tenant.inventory.models import StockMovement
        if not self.quotation_number:
            return StockMovement.objects.none()
        return StockMovement.objects.filter(
            reference=self.quotation_number,
            movement_type='OUT',
            notes__startswith=LEGACY_QUOTATION_OUT_NOTE,
        )

    def has_unrestored_legacy_stock(self):
        from apps.tenant.inventory.models import StockMovement
        if not self.legacy_stock_movements().exists():
            return False
        return not StockMovement.objects.filter(
            reference=self.quotation_number,
            notes__startswith=LEGACY_QUOTATION_RESTORE_NOTE,
        ).exists()

    def restore_legacy_stock(self):
        """
        Puts back stock that the old confirm_quotation() deducted (same product,
        warehouse, quantity and unit cost of every consumed batch). Idempotent.
        Returns the list of (product, warehouse, qty, unit_cost) restored.
        """
        from django.db import transaction
        from apps.tenant.services.fifo_engine import add_inventory_batch

        restored = []
        with transaction.atomic():
            # Lock the quotation row so two restores cannot run concurrently
            Quotation.objects.select_for_update(of=('self',)).filter(pk=self.pk).first()
            if not self.has_unrestored_legacy_stock():
                return restored
            for mv in self.legacy_stock_movements().select_related('product', 'warehouse', 'batch'):
                if mv.quantity <= 0:
                    continue
                add_inventory_batch(
                    product=mv.product,
                    warehouse=mv.warehouse,
                    quantity=mv.quantity,
                    unit_cost=mv.unit_cost or Decimal('0'),
                    expiry_date=getattr(mv.batch, 'expiry_date', None) if mv.batch_id else None,
                    batch_number=f'QUOTE-RESTORE-{self.quotation_number}'[:100],
                    reference=self.quotation_number,
                    movement_type='RETURN_IN',
                    update_last_purchase_price=False,
                    notes=f'{LEGACY_QUOTATION_RESTORE_NOTE} {self.quotation_number}',
                )
                restored.append((mv.product, mv.warehouse, mv.quantity, mv.unit_cost))
        return restored

    def convert_to_invoice(self, user=None):
        """
        Converts the quotation into a DRAFT Sales Invoice with full tax preservation.
        The caller posts it with confirm_invoice() (which consumes FIFO + COGS once).
        """
        from django.db import transaction

        with transaction.atomic():
            locked = Quotation.objects.select_for_update(of=('self',)).get(pk=self.pk)
            if locked.status == self.CONVERTED and locked.converted_invoice_id:
                return locked.converted_invoice
            if locked.status == self.CANCELLED:
                raise ValidationError('لا يمكن تحويل عرض سعر ملغى.')

            # Quotations confirmed under the old logic already deducted stock:
            # put it back first so the SALE invoice deducts it exactly once (with COGS).
            self.restore_legacy_stock()

            invoice = Invoice.objects.create(
                invoice_type=Invoice.SALE,
                partner=self.partner,
                branch=self.branch,
                warehouse=self.warehouse,
                date=datetime.date.today(),
                payment_type=Invoice.CREDIT,
                status=Invoice.DRAFT,
                cashier=user or self.created_by,
                discount_percentage=self.discount_percentage,
                discount_amount=self.discount_amount,
                vat_percentage=self.vat_percentage,
                tax_amount=self.tax_amount,
                wht_percentage=self.wht_percentage,
                wht_amount=self.wht_amount,
                subtotal=self.subtotal,
                total_amount=self.total_amount,
                notes=f'تم إنشاء الفاتورة من عرض السعر رقم {self.quotation_number}\n{self.notes}'
            )

            for qline in self.lines.all():
                InvoiceLine.objects.create(
                    invoice=invoice,
                    product=qline.product,
                    quantity=qline.quantity,
                    unit_price=qline.unit_price,
                    discount_pct=qline.discount_pct,
                    discount_amount=qline.discount_amount,
                    uom=qline.uom,
                    subtotal=qline.subtotal,
                    tax_rate=qline.tax_rate,
                    tax_amount=qline.tax_amount,
                    wht_rate=qline.wht_rate,
                    wht_amount=qline.wht_amount,
                    total_amount=qline.total_amount
                )

            invoice.recalculate_totals()
            invoice.paid_credit = invoice.total_amount
            invoice.save(update_fields=['paid_credit', 'updated_at'])

            self.status = self.CONVERTED
            self.converted_invoice = invoice
            self.save(update_fields=['status', 'converted_invoice', 'updated_at'])
            return invoice


class QuotationLine(models.Model):
    """
    Line item for a Quotation with full tax layer support.
    """
    quotation = models.ForeignKey(
        Quotation,
        on_delete=models.CASCADE,
        related_name='lines',
        verbose_name='عرض السعر'
    )
    product = models.ForeignKey(
        'inventory.Product',
        on_delete=models.PROTECT,
        related_name='quotation_lines',
        verbose_name='المنتج/الخدمة'
    )
    quantity = models.DecimalField(
        max_digits=18, decimal_places=4, verbose_name='الكمية'
    )
    unit_price = models.DecimalField(
        max_digits=18, decimal_places=4, verbose_name='سعر الوحدة'
    )
    discount_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة الخصم %'
    )
    uom = models.ForeignKey(
        'inventory.ProductUoM',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name='وحدة القياس'
    )
    discount_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة الخصم'
    )
    subtotal = MoneyField(
        default=Decimal('0'),
        verbose_name='الإجمالي (بعد الخصم، قبل الضريبة)'
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة ضريبة القيمة المضافة %'
    )
    tax_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة ضريبة القيمة المضافة'
    )
    wht_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة ضريبة الخصم والإضافة %'
    )
    wht_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='قيمة ضريبة الخصم والإضافة'
    )
    total_amount = MoneyField(
        default=Decimal('0'),
        verbose_name='الإجمالي النهائي'
    )

    class Meta:
        verbose_name = 'بند عرض سعر'
        verbose_name_plural = 'بنود عرض السعر'

    def __str__(self):
        return f'{self.product.name} × {self.quantity} @ {self.unit_price}'

    @property
    def unit_name(self):
        if self.uom and getattr(self.uom, 'uom', None):
            return self.uom.uom.name
        if self.product:
            return self.product.get_unit_display()
        return 'قطعة'

    @property
    def conversion_factor(self):
        if self.uom and getattr(self.uom, 'conversion_factor', None):
            return self.uom.conversion_factor
        return Decimal('1')

    @property
    def base_quantity(self):
        return (self.quantity or Decimal('0')) * self.conversion_factor

    def save(self, *args, **kwargs):
        a = line_amounts(self.quantity, self.unit_price, discount_pct=self.discount_pct,
                         tax_rate=self.tax_rate, wht_rate=self.wht_rate)          # same rule as InvoiceLine
        self.discount_amount, self.subtotal = a['discount'], a['subtotal']
        self.tax_amount, self.wht_amount, self.total_amount = a['tax'], a['wht'], a['total']
        super().save(*args, **kwargs)



class PurchaseOrder(models.Model):
    """أمر شراء لمورد. بيتحول لفاتورة شراء (كلها أو جزء)؛ الفاتورة المرتبطة بتحدّث الكميات المستلمة."""
    DRAFT = 'DRAFT'
    SENT = 'SENT'
    PARTIAL = 'PARTIAL'
    RECEIVED = 'RECEIVED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = ((DRAFT, 'مسودة'), (SENT, 'اتبعت للمورد'), (PARTIAL, 'مستلم جزئياً'), (RECEIVED, 'مستلم بالكامل'),
                      (CANCELLED, 'ملغي'))
    OPEN_STATUSES = (DRAFT, SENT, PARTIAL)

    number = models.CharField(max_length=30, unique=True, editable=False, verbose_name='رقم أمر الشراء')
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    supplier = models.ForeignKey('partners.Partner', on_delete=models.PROTECT, related_name='purchase_orders',
                                 verbose_name='المورد')
    branch = models.ForeignKey('core.Branch', on_delete=models.PROTECT, related_name='purchase_orders', verbose_name='الفرع')
    warehouse = models.ForeignKey('inventory.Warehouse', on_delete=models.PROTECT, related_name='purchase_orders',
                                  verbose_name='المخزن')
    expected_date = models.DateField(null=True, blank=True, verbose_name='ميعاد الاستلام المتوقع')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=DRAFT, db_index=True, verbose_name='الحالة')
    notes = models.TextField(blank=True, default='', verbose_name='ملاحظات')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='بواسطة')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'أمر شراء'
        verbose_name_plural = 'أوامر الشراء'
        ordering = ['-date', '-id']

    def __str__(self):
        return f'{self.number} — {self.supplier}'

    def save(self, *args, **kwargs):
        if not self.number:
            from django.db import transaction
            from apps.tenant.core.sequences import max_numeric_suffix, next_sequence
            prefix = f'PO-{(self.date or datetime.date.today()).year}-'
            with transaction.atomic():
                n = next_sequence(f'purchase_order:{prefix}',
                                  seed=lambda: max_numeric_suffix(PurchaseOrder.objects.all(), 'number', prefix))
                self.number = f'{prefix}{n:05d}'
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='lines', verbose_name='أمر الشراء')
    product = models.ForeignKey('inventory.Product', on_delete=models.PROTECT, related_name='purchase_order_lines',
                                verbose_name='الصنف')
    uom = models.ForeignKey('inventory.ProductUoM', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                            verbose_name='الوحدة')
    quantity = models.DecimalField(max_digits=14, decimal_places=3, verbose_name='الكمية المطلوبة')
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name='السعر المتوقع')
    received_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal('0'), verbose_name='المستلم')

    class Meta:
        verbose_name = 'بند أمر شراء'
        verbose_name_plural = 'بنود أوامر الشراء'

    @property
    def remaining(self):
        return max(self.quantity - self.received_quantity, Decimal('0'))


class SalesRep(models.Model):
    """مندوب مبيعات: موظف ليه مخزن عربية، ونسبة عمولة على صافي مبيعاته، وتارجت شهري.
    فواتير نقطة البيع اللي بيعملها بتتعلّم باسمه لوحدها (Invoice.sales_rep)."""
    employee = models.OneToOneField('core.Employee', on_delete=models.CASCADE, related_name='sales_rep',
                                    verbose_name='الموظف')
    van_warehouse = models.ForeignKey('inventory.Warehouse', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='sales_reps', verbose_name='مخزن العربية')
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'),
                                          verbose_name='نسبة العمولة %')
    monthly_target = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'),
                                         verbose_name='التارجت الشهري')
    treasury = models.ForeignKey('accounting.Treasury', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='sales_reps', verbose_name='خزنة المندوب')
    source_warehouse = models.ForeignKey('inventory.Warehouse', on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='+', verbose_name='المخزن اللي بيحمّل منه')
    is_active = models.BooleanField(default=True, verbose_name='نشط')

    class Meta:
        verbose_name = 'مندوب مبيعات'
        verbose_name_plural = 'مناديب المبيعات'
        permissions = [('use_rep_app', 'واجهة المندوب على الموبايل (بيع، تحصيل، طلب تحميل وتفريغ)')]

    def __str__(self):
        return self.employee.user.get_full_name() or self.employee.user.username


class RepCommission(models.Model):
    """عمولة مندوب عن شهر (بعد الاعتماد بتتضاف لمرتبه في المسير)."""
    rep = models.ForeignKey(SalesRep, on_delete=models.CASCADE, related_name='commissions', verbose_name='المندوب')
    year = models.PositiveIntegerField(verbose_name='السنة')
    month = models.PositiveSmallIntegerField(verbose_name='الشهر')
    net_sales = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='صافي المبيعات')
    rate = models.DecimalField(max_digits=5, decimal_places=2, verbose_name='النسبة %')
    amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='العمولة')
    approved_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                    verbose_name='اعتمدها')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'عمولة مندوب'
        verbose_name_plural = 'عمولات المناديب'
        unique_together = ('rep', 'year', 'month')


# ---------------------------------------------------------------------------
# Van sales: loading / unloading requests and the rep's cash hand-over
# (invoicing.van_services has the rules; invoicing.rep_app is the rep's mobile screen)
# ---------------------------------------------------------------------------

def _numbered_save(obj, model, prefix_code, *args, **kwargs):
    if not obj.number:
        from django.db import transaction
        from apps.tenant.core.sequences import max_numeric_suffix, next_sequence
        prefix = f'{prefix_code}-{(obj.date or datetime.date.today()).year}-'
        with transaction.atomic():
            n = next_sequence(f'{model._meta.model_name}:{prefix}',
                              seed=lambda: max_numeric_suffix(model.objects.all(), 'number', prefix))
            obj.number = f'{prefix}{n:05d}'
            return models.Model.save(obj, *args, **kwargs)
    return models.Model.save(obj, *args, **kwargs)


class LoadingOrder(models.Model):
    """طلب تحميل: المندوب بيطلب بضاعة من المخزن لعربيته ← اعتماد (بالكميات المعتمدة) ← صرف وترحيل
    (تحويل مخزني من المخزن لعربية المندوب)."""
    PENDING, APPROVED, ISSUED, CANCELLED = 'PENDING', 'APPROVED', 'ISSUED', 'CANCELLED'
    STATUS_CHOICES = [(PENDING, 'مستني الاعتماد'), (APPROVED, 'معتمد — مستني الصرف'),
                      (ISSUED, 'اتصرف واترحل'), (CANCELLED, 'ملغي')]

    number = models.CharField(max_length=30, unique=True, editable=False, verbose_name='رقم الطلب')
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    rep = models.ForeignKey('invoicing.SalesRep', on_delete=models.PROTECT, related_name='loading_orders',
                            verbose_name='المندوب')
    from_warehouse = models.ForeignKey('inventory.Warehouse', on_delete=models.PROTECT, related_name='+',
                                       verbose_name='من مخزن')
    to_warehouse = models.ForeignKey('inventory.Warehouse', on_delete=models.PROTECT, related_name='+',
                                     verbose_name='لعربية المندوب')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True,
                              verbose_name='الحالة')
    notes = models.TextField(blank=True, default='', verbose_name='ملاحظات')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                     verbose_name='طلبه')
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                    verbose_name='اعتمده')
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name='وقت الاعتماد')
    issued_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                  verbose_name='صرفه')
    issued_at = models.DateTimeField(null=True, blank=True, verbose_name='وقت الصرف')
    transfer = models.OneToOneField('inventory.StockTransfer', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='loading_order', verbose_name='التحويل المخزني')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'طلب تحميل'
        verbose_name_plural = 'طلبات التحميل'
        ordering = ['-date', '-id']
        permissions = [('approve_loadingorder', 'اعتماد طلب التحميل'),
                       ('issue_loadingorder', 'صرف طلب التحميل من المخزن وترحيله')]

    def __str__(self):
        return self.number

    def save(self, *args, **kwargs):
        return _numbered_save(self, LoadingOrder, 'LD', *args, **kwargs)


class LoadingOrderLine(models.Model):
    order = models.ForeignKey(LoadingOrder, on_delete=models.CASCADE, related_name='lines', verbose_name='الطلب')
    product = models.ForeignKey('inventory.Product', on_delete=models.PROTECT, related_name='+', verbose_name='الصنف')
    uom = models.ForeignKey('inventory.ProductUoM', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                            verbose_name='الوحدة')
    conversion_factor = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('1'),
                                            verbose_name='معامل التحويل')
    requested_qty = models.DecimalField(max_digits=18, decimal_places=4, verbose_name='الكمية المطلوبة')
    approved_qty = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True,
                                       verbose_name='الكمية المعتمدة')

    class Meta:
        verbose_name = 'بند طلب تحميل'
        verbose_name_plural = 'بنود طلبات التحميل'
        ordering = ['id']

    @property
    def uom_name(self):
        return self.uom.uom.name if self.uom_id else self.product.pos_unit_name

    @property
    def final_qty(self):
        return self.requested_qty if self.approved_qty is None else self.approved_qty


class UnloadingOrder(models.Model):
    """طلب تفريغ: المندوب بيرجّع بضاعة من العربية للمخزن (سليم للمخزن، والتالف لمخزن التوالف لو محدد)
    ← أمين المخزن بيستلم بالكميات الفعلية ويرحّل."""
    PENDING, RECEIVED, CANCELLED = 'PENDING', 'RECEIVED', 'CANCELLED'
    STATUS_CHOICES = [(PENDING, 'مستني الاستلام'), (RECEIVED, 'اتستلم واترحل'), (CANCELLED, 'ملغي')]

    number = models.CharField(max_length=30, unique=True, editable=False, verbose_name='رقم الطلب')
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    rep = models.ForeignKey('invoicing.SalesRep', on_delete=models.PROTECT, related_name='unloading_orders',
                            verbose_name='المندوب')
    from_warehouse = models.ForeignKey('inventory.Warehouse', on_delete=models.PROTECT, related_name='+',
                                       verbose_name='من عربية المندوب')
    to_warehouse = models.ForeignKey('inventory.Warehouse', on_delete=models.PROTECT, related_name='+',
                                     verbose_name='للمخزن')
    damaged_warehouse = models.ForeignKey('inventory.Warehouse', on_delete=models.PROTECT, null=True, blank=True,
                                          related_name='+', verbose_name='مخزن التوالف')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True,
                              verbose_name='الحالة')
    notes = models.TextField(blank=True, default='', verbose_name='ملاحظات')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                     verbose_name='طلبه')
    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                    verbose_name='استلمه')
    received_at = models.DateTimeField(null=True, blank=True, verbose_name='وقت الاستلام')
    transfer = models.OneToOneField('inventory.StockTransfer', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='unloading_order', verbose_name='تحويل السليم')
    damaged_transfer = models.OneToOneField('inventory.StockTransfer', on_delete=models.SET_NULL, null=True,
                                            blank=True, related_name='unloading_damaged', verbose_name='تحويل التالف')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'طلب تفريغ'
        verbose_name_plural = 'طلبات التفريغ'
        ordering = ['-date', '-id']
        permissions = [('receive_unloadingorder', 'استلام طلب التفريغ في المخزن وترحيله')]

    def __str__(self):
        return self.number

    def save(self, *args, **kwargs):
        return _numbered_save(self, UnloadingOrder, 'UL', *args, **kwargs)


class UnloadingOrderLine(models.Model):
    order = models.ForeignKey(UnloadingOrder, on_delete=models.CASCADE, related_name='lines', verbose_name='الطلب')
    product = models.ForeignKey('inventory.Product', on_delete=models.PROTECT, related_name='+', verbose_name='الصنف')
    sound_qty = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0'),
                                    verbose_name='سليم (بالوحدة الأساسية)')
    damaged_qty = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0'),
                                      verbose_name='تالف (بالوحدة الأساسية)')
    # what the rep asked for (sound/damaged above become the counted quantities on receipt)
    requested_sound = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True,
                                          verbose_name='سليم — طلب المندوب')
    requested_damaged = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True,
                                            verbose_name='تالف — طلب المندوب')

    class Meta:
        verbose_name = 'بند طلب تفريغ'
        verbose_name_plural = 'بنود طلبات التفريغ'
        ordering = ['id']


class RepCashHandover(models.Model):
    """توريد نقدية المندوب: فلوس خزنة المندوب بتتنقل للخزنة الرئيسية أو البنك
    (Dr الخزنة المستلمة / Cr خزنة المندوب). العجز ممكن يتحمّل على المندوب
    (Dr 1135 ذمم الموظفين / Cr خزنة المندوب) ويتخصم من مرتبه كسلفة عجز."""
    number = models.CharField(max_length=30, unique=True, editable=False, verbose_name='رقم التوريد')
    date = models.DateField(default=datetime.date.today, verbose_name='التاريخ')
    rep = models.ForeignKey('invoicing.SalesRep', on_delete=models.PROTECT, related_name='handovers',
                            verbose_name='المندوب')
    expected = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'),
                                   verbose_name='رصيد خزنة المندوب وقت التوريد')
    amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='المبلغ المورّد')
    shortage_charged = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'),
                                           verbose_name='عجز اتحمّل على المندوب')
    treasury = models.ForeignKey('accounting.Treasury', on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='+', verbose_name='الخزنة المستلمة')
    bank_account = models.ForeignKey('accounting.BankAccount', on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='+', verbose_name='البنك المستلم')
    intermediary = models.ForeignKey('accounting.IntermediaryCompany', on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='handovers', verbose_name='ورّد عن طريق شركة وسيطة')
    reference = models.CharField(max_length=100, blank=True, default='', verbose_name='رقم العملية (فوري / أمان…)')
    notes = models.CharField(max_length=255, blank=True, default='', verbose_name='ملاحظات')
    journal_entry = models.ForeignKey('accounting.JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+', verbose_name='القيد')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='استلمه')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'توريد نقدية مندوب'
        verbose_name_plural = 'توريدات المناديب'
        ordering = ['-date', '-id']
        constraints = [models.UniqueConstraint(   # one Fawry / Aman receipt = one hand-over
            fields=['intermediary', 'reference'], condition=models.Q(intermediary__isnull=False) & ~models.Q(reference=''),
            name='uniq_intermediary_handover_ref')]

    def __str__(self):
        return self.number

    def save(self, *args, **kwargs):
        return _numbered_save(self, RepCashHandover, 'RH', *args, **kwargs)


class ClosedShiftPaymentCorrection(models.Model):
    """A sale of a closed shift recorded with the wrong payment method, fixed by one entry — see
    invoicing.closed_shift_correction. Once per invoice, both ways:
      * recorded CASH, really card / wallet / transfer: Dr where the money went / Cr the shift's SHORTAGE account;
      * recorded card / wallet / transfer, really CASH: Dr the shift's OVERAGE account / Cr the bank / wallet
        the invoice was booked to."""
    CASH, CARD, EWALLET, BANK_TRANSFER = 'CASH', 'CARD', 'EWALLET', 'BANK_TRANSFER'
    METHOD_CHOICES = [(CASH, 'نقدي'), (CARD, 'فيزا'), (EWALLET, 'محفظة إلكترونية'),
                      (BANK_TRANSFER, 'تحويل بنكي / إنستاباي')]

    invoice = models.OneToOneField(Invoice, on_delete=models.PROTECT, related_name='closed_shift_correction',
                                   verbose_name='الفاتورة')
    session = models.ForeignKey(POSSession, on_delete=models.PROTECT, related_name='payment_corrections',
                                verbose_name='الوردية')
    recorded_method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=CASH,
                                       verbose_name='الطريقة اللي اتسجلت')
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, verbose_name='الطريقة الصح')
    bank_account = models.ForeignKey('accounting.BankAccount', on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='+', verbose_name='الحساب البنكي / ماكينة الفيزا')
    ewallet = models.ForeignKey('accounting.EWallet', on_delete=models.PROTECT, null=True, blank=True,
                                related_name='+', verbose_name='المحفظة')
    amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='المبلغ')
    variance_account = models.ForeignKey('accounting.Account', on_delete=models.PROTECT, related_name='+',
                                         verbose_name='حساب العجز / الزيادة (من قيد قفل الوردية)')
    reason = models.CharField(max_length=255, verbose_name='السبب')
    journal_entry = models.ForeignKey('accounting.JournalEntry', on_delete=models.PROTECT, related_name='+',
                                      verbose_name='القيد')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name='صحّحها')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'تصحيح طريقة سداد (وردية مقفولة)'
        verbose_name_plural = 'تصحيحات طريقة السداد (ورديات مقفولة)'
        ordering = ['-created_at']
        permissions = [('correct_closed_shift_payment', 'تصحيح طريقة سداد فاتورة في وردية مقفولة')]
