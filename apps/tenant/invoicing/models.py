"""
Invoicing models: Invoice and InvoiceLine.

Invoice is the master document for both sales (POS/credit) and purchases.
InvoiceLine auto-calculates subtotal and tax on save().

Lifecycle:
  DRAFT → confirm_invoice() → POSTED (triggers FIFO + accounting)
  DRAFT → CANCELLED (no financial effects)
"""
from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from decimal import Decimal
import datetime


# ---------------------------------------------------------------------------
# Auto-generate invoice numbers
# ---------------------------------------------------------------------------

def _generate_invoice_number(invoice_type: str) -> str:
    """
    Generates a sequential invoice number per type.
    Format: SALE-2024-00001, PUR-2024-00001, etc.
    """
    prefix_map = {
        'SALE': 'SALE',
        'PURCHASE': 'PUR',
        'RETURN_SALE': 'RSAL',
        'RETURN_PURCHASE': 'RPUR',
    }
    prefix = prefix_map.get(invoice_type, 'INV')
    year = datetime.date.today().year
    full_prefix = f'{prefix}-{year}-'
    last = Invoice.objects.filter(
        invoice_number__startswith=full_prefix
    ).order_by('-invoice_number').first()
    if last:
        try:
            last_num = int(last.invoice_number.split('-')[-1])
        except (ValueError, IndexError):
            last_num = 0
    else:
        last_num = 0
    return f'{full_prefix}{last_num + 1:05d}'


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
        max_digits=15, decimal_places=4, default=Decimal('0'), verbose_name='رصيد الافتتاح (العهدة)'
    )
    closing_balance_expected = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'), verbose_name='الرصيد المتوقع'
    )
    closing_balance_actual = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'), verbose_name='الرصيد الفعلي'
    )
    difference = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'), verbose_name='العجز / الزيادة'
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=OPEN, verbose_name='الحالة'
    )
    notes = models.TextField(blank=True, verbose_name='ملاحظات')

    class Meta:
        verbose_name = 'وردية نقاط البيع'
        verbose_name_plural = 'ورديات نقاط البيع'
        ordering = ['-start_time']

    def __str__(self):
        return f"وردية {self.user.username} - {self.get_status_display()} ({self.start_time.strftime('%Y-%m-%d %H:%M')})"


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

    PAYMENT_TYPE_CHOICES = [
        (CASH, 'نقدي'),
        (CREDIT, 'آجل'),
        (CARD, 'بطاقة'),
        (EWALLET, 'محفظة إلكترونية'),
        (BANK_TRANSFER, 'تحويل بنكي'),
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

    # Financial totals (auto-calculated from lines)
    subtotal = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='إجمالي قبل الضريبة'
    )
    discount_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة الخصم الإجمالي (%)'
    )
    discount_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='قيمة الخصم'
    )
    tax_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
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
    wht_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='قيمة الخصم والإضافة'
    )
    total_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'فاتورة'
        verbose_name_plural = 'الفواتير'
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.invoice_number} | {self.get_invoice_type_display()} | {self.total_amount}'

    def save(self, *args, **kwargs):
        """Auto-generate invoice number if not set."""
        if self.pk:
            orig = Invoice.objects.get(pk=self.pk)
            if orig.status == self.POSTED and self.status == self.POSTED:
                pass

        if not self.invoice_number:
            self.invoice_number = _generate_invoice_number(self.invoice_type)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.POSTED:
            raise ValidationError('لا يمكن حذف فاتورة مرحّلة.')
        return super().delete(*args, **kwargs)

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
                    is_manager = self.cashier and (self.cashier.is_superuser or self.cashier.is_staff)
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
    quantity = models.DecimalField(
        max_digits=15, decimal_places=4, verbose_name='الكمية'
    )
    unit_price = models.DecimalField(
        max_digits=15, decimal_places=4, verbose_name='سعر الوحدة'
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
    discount_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='قيمة الخصم'
    )

    subtotal = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='الإجمالي (بعد خصم، قبل ضريبة)'
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة الضريبة %'
    )
    tax_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='قيمة الضريبة'
    )
    wht_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name='نسبة ضريبة الخصم والإضافة %'
    )
    wht_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='قيمة الخصم والإضافة'
    )
    total_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='الإجمالي النهائي'
    )

    # Filled by FIFO engine during invoice confirmation
    cogs_amount = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name='تكلفة البضاعة المباعة'
    )

    class Meta:
        verbose_name = 'بند فاتورة'
        verbose_name_plural = 'بنود الفاتورة'

    def __str__(self):
        return f'{self.product.name} × {self.quantity} @ {self.unit_price}'

    def save(self, *args, **kwargs):
        """
        Auto-calculate financial figures before saving.

        subtotal = quantity × unit_price × (1 - discount_pct/100)
        discount_amount = quantity × unit_price × (discount_pct/100)
        tax_amount = subtotal × tax_rate/100
        wht_amount = subtotal × wht_rate/100
        total_amount = subtotal + tax_amount - wht_amount
        """
        qty = self.quantity or Decimal('0')
        price = self.unit_price or Decimal('0')
        disc_pct = self.discount_pct or Decimal('0')

        gross = qty * price
        self.discount_amount = (gross * disc_pct / Decimal('100')).quantize(Decimal('0.0001'))
        self.subtotal = (gross - self.discount_amount).quantize(Decimal('0.0001'))
        
        # Calculate taxes
        self.tax_amount = (self.subtotal * (self.tax_rate / Decimal('100.0'))).quantize(Decimal('0.0001'))
        self.wht_amount = (self.subtotal * (self.wht_rate / Decimal('100.0'))).quantize(Decimal('0.0001'))
        
        self.total_amount = self.subtotal + self.tax_amount - self.wht_amount

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
                    if not self.product.allow_negative_stock:
                        available = self.product.get_stock(self.invoice.warehouse)
                        if self.quantity > available:
                            is_manager = self.invoice.cashier and (self.invoice.cashier.is_superuser or self.invoice.cashier.is_staff)
                            if not is_manager:
                                raise ValidationError(f'الكمية المطلوبة ({self.quantity}) تتجاوز المخزون المتاح ({available}) للمنتج {self.product.name} وغير مسموح بالسحب بالسالب.')
        except Exception:
            pass
