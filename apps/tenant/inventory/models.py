"""
Inventory models: Warehouse, Category, Product, InventoryBatch (FIFO), StockMovement.

Critical design decisions:
  - InventoryBatch.Meta.ordering = ['created_at'] → ensures FIFO consumption order.
  - Product.get_stock() aggregates quantity_remaining from all non-exhausted batches.
  - InventoryBatch tracks per-batch cost for accurate COGS calculation.
  - StockMovement provides a complete audit trail of every stock change.
"""
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.db.models import F, Sum
from mptt.models import MPTTModel, TreeForeignKey
from decimal import Decimal
import datetime
from django.conf import settings


# ---------------------------------------------------------------------------
# Warehouse
# ---------------------------------------------------------------------------

class Warehouse(models.Model):
    """
    A physical storage location within a Branch.
    Each Branch can have multiple Warehouses (e.g., Main Store, Cold Storage).
    """
    branch = models.ForeignKey(
        'core.Branch',
        on_delete=models.PROTECT,
        related_name='warehouses',
        verbose_name=_('الفرع')
    )
    name = models.CharField(max_length=200, verbose_name=_('اسم المستودع'))
    code = models.CharField(max_length=20, unique=True, verbose_name=_('كود المستودع'))
    is_cold_storage = models.BooleanField(
        default=False, 
        verbose_name=_('تبريد'),
        help_text=_('حدد هذا الخيار إذا كان المستودع مخصصاً للتبريد أو التجميد (مثل الثلاجات)')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('نشط'))
    description = models.TextField(blank=True, verbose_name=_('وصف'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('مستودع')
        verbose_name_plural = _('المستودعات')
        ordering = ['branch', 'name']

    def __str__(self):
        return f'{self.name} ({self.branch.name})'


# ---------------------------------------------------------------------------
# Category (MPTT tree)
# ---------------------------------------------------------------------------

class Category(MPTTModel):
    """
    Product category using MPTT for efficient tree queries.
    Example: Food → Dairy → Cheese
    """
    name = models.CharField(max_length=200, verbose_name=_('اسم الفئة'))
    parent = TreeForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children',
        verbose_name=_('الفئة الأم')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('نشطة'))
    description = models.TextField(blank=True, verbose_name=_('وصف'))
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Category-level EGS code: used by every product of the category (and of its
    # sub-categories) that has no GS1 / EGS code of its own (einvoicing.services.item_code).
    name_en = models.CharField(max_length=200, blank=True, default='', verbose_name=_('اسم الفئة بالإنجليزي'))
    gpc_code = models.CharField(max_length=20, blank=True, default='', verbose_name=_('كود GPC للفئة'))
    egs_code = models.CharField(max_length=50, blank=True, default='', verbose_name=_('كود EGS للفئة'),
                                help_text=_('يُسجَّل من: منظومة الضرائب ← تسجيل أكواد الأصناف. أو اكتب كوداً معتمداً لديك بالفعل.'))
    egs_status = models.CharField(max_length=10, blank=True, default='', verbose_name=_('حالة تسجيل EGS'),
                                  choices=[('REQUESTED', 'في انتظار موافقة الضرائب'),
                                           ('APPROVED', 'معتمد من الضرائب'), ('FAILED', 'رفضت الضرائب الطلب')])
    egs_requested_at = models.DateTimeField(null=True, blank=True, verbose_name=_('تاريخ طلب تسجيل EGS'))
    egs_message = models.CharField(max_length=500, blank=True, default='', verbose_name=_('رد الضرائب على طلب EGS'))

    class MPTTMeta:
        order_insertion_by = ['name']

    class Meta:
        verbose_name = _('فئة')
        verbose_name_plural = _('الفئات')

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------

class Product(models.Model):
    """
    A sellable/purchasable item in the supermarket.
    Stock is tracked via InventoryBatch (FIFO).
    """
    UNIT_CHOICES = [
        ('KG', 'كيلو'),
        ('PIECE', 'قطعة'),
        ('LITER', 'لتر'),
        ('BOX', 'كرتونة'),
        ('DOZEN', 'دستة'),
        ('GRAM', 'جرام'),
        ('METER', 'متر'),
        ('PACK', 'عبوة'),
    ]

    name = models.CharField(max_length=300, verbose_name=_('اسم المنتج'))
    name_en = models.CharField(max_length=300, blank=True, verbose_name=_('الاسم بالإنجليزية'))
    barcode = models.CharField(
        max_length=50, unique=True, null=True, blank=True, verbose_name=_('باركود')
    )
    sku = models.CharField(
        max_length=50, unique=True, null=True, blank=True, verbose_name=_('كود المنتج')
    )
    gs1_code = models.CharField(
        max_length=50, null=True, blank=True, verbose_name=_('كود GS1')
    )
    egs_code = models.CharField(
        max_length=50, null=True, blank=True, verbose_name=_('كود EGS')
    )
    # --- EGS code registration at the Tax Authority (einvoicing.egs) ---
    EGS_REQUESTED = 'REQUESTED'
    EGS_APPROVED = 'APPROVED'
    EGS_FAILED = 'FAILED'
    EGS_STATUS_CHOICES = [
        (EGS_REQUESTED, 'في انتظار موافقة الضرائب'),
        (EGS_APPROVED, 'معتمد من الضرائب'),
        (EGS_FAILED, 'رفضت الضرائب الطلب'),
    ]
    gpc_code = models.CharField(max_length=20, blank=True, default='', verbose_name=_('كود GPC (المستوى الرابع)'),
                                help_text=_('كود تصنيف المنتج العالمي — مطلوب لتسجيل كود EGS لدى الضرائب.'))
    egs_status = models.CharField(max_length=10, blank=True, default='', choices=EGS_STATUS_CHOICES,
                                  verbose_name=_('حالة تسجيل EGS'))
    egs_requested_at = models.DateTimeField(null=True, blank=True, verbose_name=_('تاريخ طلب تسجيل EGS'))
    egs_message = models.CharField(max_length=500, blank=True, default='', verbose_name=_('رد الضرائب على طلب EGS'))
    item_code = models.CharField(
        max_length=50, null=True, blank=True, verbose_name=_('كود الصنف الداخلي')
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name='products',
        verbose_name=_('الفئة')
    )
    unit = models.CharField(
        max_length=10, choices=UNIT_CHOICES, default='PIECE', verbose_name=_('وحدة القياس')
    )
    PRODUCT_TYPE_CHOICES = [
        ('PRODUCT', _('منتج مخزني')),
        ('SERVICE', _('خدمة / مصنعية')),
        ('SERIALIZED', _('منتج برقم تسلسلي (موبايل/أجهزة)')),
    ]
    product_type = models.CharField(
        max_length=15, choices=PRODUCT_TYPE_CHOICES, default='PRODUCT',
        verbose_name=_('نوع المنتج')
    )
    has_serial = models.BooleanField(
        default=False, verbose_name=_('له رقم تسلسلي (S/N)')
    )
    has_imei = models.BooleanField(
        default=False, verbose_name=_('له أرقام IMEI (موبايلات)')
    )
    is_open_price = models.BooleanField(
        default=False, verbose_name=_('سعر مفتوح في نقطة البيع')
    )
    sale_price = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        verbose_name=_('سعر البيع')
    )
    min_sale_price = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        verbose_name=_('أدنى سعر بيع')
    )
    last_purchase_price = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        verbose_name=_('آخر سعر شراء')
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('14'),
        verbose_name=_('نسبة الضريبة %')
    )
    withholding_tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name=_('نسبة ضريبة الخصم والإضافة %')
    )
    min_stock_level = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        verbose_name=_('حد أدنى للمخزون')
    )
    max_stock_level = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        verbose_name=_('حد أقصى للمخزون')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('نشط'))
    allow_negative_stock = models.BooleanField(
        default=False, verbose_name=_('السماح بمخزون سالب')
    )
    has_expiry = models.BooleanField(
        default=False, verbose_name=_('له تاريخ صلاحية'),
        help_text=_('فاتورة الشراء بتطلب تاريخ الصلاحية، والصرف بيطلّع الأقرب انتهاءً الأول (FEFO).'))
    ETA_SUBTYPE_CHOICES = [
        ('', 'تلقائي حسب النسبة (14% = V009 · 0% = V003)'),
        ('V001', 'V001 — تصدير للخارج'),
        ('V002', 'V002 — تصدير لمناطق حرة / أخرى'),
        ('V003', 'V003 — سلعة أو خدمة معفاة'),
        ('V004', 'V004 — سلعة أو خدمة غير خاضعة'),
        ('V005', 'V005 — إعفاءات دبلوماسية'),
        ('V006', 'V006 — إعفاءات دفاع وأمن قومي'),
        ('V007', 'V007 — إعفاءات اتفاقيات'),
        ('V008', 'V008 — إعفاءات خاصة وأخرى'),
        ('V009', 'V009 — سلع عامة (14%)'),
        ('V010', 'V010 — نسب ضريبة أخرى'),
    ]
    eta_tax_subtype = models.CharField(
        max_length=10, blank=True, default='', choices=ETA_SUBTYPE_CHOICES,
        verbose_name=_('الكود الفرعي للقيمة المضافة (الفاتورة الإلكترونية)'))
    description = models.TextField(blank=True, verbose_name=_('وصف'))
    image = models.ImageField(
        upload_to='products/', null=True, blank=True, verbose_name=_('صورة')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('منتج')
        verbose_name_plural = _('المنتجات')
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['is_active']),
        ]
        permissions = [
            ('sell_below_min_price', 'البيع بأقل من أدنى سعر بيع'),
            ('sell_expired_batch', 'بيع دفعات منتهية الصلاحية'),
        ]

    def __str__(self):
        return f'{self.name} [{self.get_unit_display()}]'

    def get_stock(self, warehouse=None):
        """
        Returns total available stock.
        For SERIALIZED products, counts unsold SerialItems.
        For TANGIBLE products, aggregates quantity_remaining from InventoryBatch.
        """
        if self.product_type == 'SERIALIZED':
            qs = self.serials.filter(is_sold=False)
            if warehouse is not None:
                qs = qs.filter(warehouse=warehouse)
            return Decimal(str(qs.count()))
            
        qs = InventoryBatch.objects.filter(
            product=self
        )
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)
        result = qs.aggregate(total=Sum('quantity_remaining'))['total']
        return result or Decimal('0')

    def get_average_cost(self, warehouse=None):
        """
        Returns weighted average cost per unit from available batches.
        """
        qs = InventoryBatch.objects.filter(
            product=self,
            quantity_remaining__gt=Decimal('0')
        )
        if warehouse:
            qs = qs.filter(warehouse=warehouse)
        # Weighted average sum(qty * cost) / sum(qty), both computed by the database
        agg = qs.aggregate(
            q=Sum('quantity_remaining'),
            v=Sum(F('quantity_remaining') * F('unit_cost'),
                  output_field=models.DecimalField(max_digits=30, decimal_places=8)),
        )
        total_qty = agg['q'] or Decimal('0')
        if total_qty == 0:
            return self.last_purchase_price
        return ((agg['v'] or Decimal('0')) / total_qty).quantize(Decimal('0.0001'))

    @property
    def is_low_stock(self):
        return self.get_stock() <= self.min_stock_level and self.min_stock_level > 0

    @property
    def pos_unit_name(self):
        # We loop to utilize prefetch_related cache if available
        for puom in self.uoms.all():
            if puom.is_base:
                return puom.uom.name
        return self.get_unit_display()

    def get_price_for_branch(self, branch):
        """Returns the branch-specific price if it exists, otherwise the global default."""
        if not branch: return self.sale_price
        pb = self.branch_prices.filter(branch=branch).first()
        return pb.sale_price if pb else self.sale_price

    @staticmethod
    def branch_price_map(product_ids, branch):
        """{product_id: branch sale price} for many products in ONE query (only products that
        have a branch price). Use ``prices.get(p.id, p.sale_price)`` instead of calling
        get_price_for_branch() in a loop."""
        if not branch or not product_ids:
            return {}
        return dict(ProductBranch.objects.filter(product_id__in=list(product_ids), branch=branch)
                    .values_list('product_id', 'sale_price'))



def is_official_gtin(code):
    """
    True for a manufacturer barcode that can be a GS1 code: GTIN-8/12/13/14 digits with a
    valid check digit, excluding the in-store / variable-weight ranges (EAN-13 starting
    with 2, UPC-A starting with 2) that a shop or a scale prints itself.
    """
    code = (code or '').strip()
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    if (len(code) == 13 and code[0] == '2') or (len(code) == 12 and code[0] == '2'):
        return False
    if len(code) == 14 and code[1] == '2':
        return False
    if set(code) == {'0'} or '00000000' in code:
        return False    # placeholder / generated numbers (e.g. 100000000007), not a manufacturer GTIN
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


# ---------------------------------------------------------------------------
# Unit of Measure Models
# ---------------------------------------------------------------------------

class UnitOfMeasure(models.Model):
    name = models.CharField(max_length=50, verbose_name=_('اسم الوحدة'))
    eta_code = models.CharField(max_length=10, blank=True, default='',
                                verbose_name=_('كود الوحدة في الفاتورة الإلكترونية'),
                                help_text=_('مثال: EA قطعة · KGM كيلو · LTR لتر · BX كرتونة · PK عبوة. فاضي = يتعرف من الاسم.'))

    class Meta:
        verbose_name = _('وحدة قياس')
        verbose_name_plural = _('وحدات القياس')

    def __str__(self):
        return self.name


class ProductUoM(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='uoms', verbose_name=_('المنتج')
    )
    uom = models.ForeignKey(
        UnitOfMeasure, on_delete=models.PROTECT, verbose_name=_('الوحدة')
    )
    is_base = models.BooleanField(default=False, verbose_name=_('وحدة أساسية'))
    conversion_factor = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal('1'), verbose_name=_('معامل التحويل')
    )
    barcode = models.CharField(
        max_length=100, unique=True, null=True, blank=True, verbose_name=_('باركود الوحدة')
    )

    class Meta:
        verbose_name = _('وحدة قياس المنتج')
        verbose_name_plural = _('وحدات قياس المنتج')

    def __str__(self):
        return f"{self.product.name} - {self.uom.name}"


class ProductBranch(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='branch_prices')
    branch = models.ForeignKey('core.Branch', on_delete=models.CASCADE, related_name='product_prices')
    sale_price = models.DecimalField(max_digits=18, decimal_places=4, verbose_name=_('سعر البيع بالفرع'))
    min_sale_price = models.DecimalField(max_digits=18, decimal_places=4, verbose_name=_('أقل سعر بيع بالفرع'))
    
    class Meta:
        unique_together = ('product', 'branch')
        verbose_name = _('سعر صنف بالفرع')
        verbose_name_plural = _('أسعار الأصناف بالفروع')


# ---------------------------------------------------------------------------
# InventoryBatch (FIFO lot)
# ---------------------------------------------------------------------------

class InventoryBatch(models.Model):
    """
    A single FIFO inventory batch / lot.
    Each purchase creates a new batch; sales consume the oldest batches first.

    CRITICAL: Meta.ordering = ['created_at'] ensures FIFO order for consumption.
    """
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='batches',
        verbose_name=_('المنتج')
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name='batches',
        verbose_name=_('المستودع')
    )
    quantity_original = models.DecimalField(
        max_digits=18, decimal_places=4, verbose_name=_('الكمية الأصلية')
    )
    quantity_remaining = models.DecimalField(
        max_digits=18, decimal_places=4, verbose_name=_('الكمية المتبقية')
    )
    unit_cost = models.DecimalField(
        max_digits=18, decimal_places=4, verbose_name=_('تكلفة الوحدة')
    )
    expiry_date = models.DateField(
        null=True, blank=True, verbose_name=_('تاريخ انتهاء الصلاحية')
    )
    batch_number = models.CharField(
        max_length=100, blank=True, verbose_name=_('رقم الدفعة')
    )
    # Link back to the purchase invoice line that created this batch
    source_invoice_line = models.ForeignKey(
        'invoicing.InvoiceLine',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_batches',
        verbose_name=_('بند الفاتورة المصدر')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('دفعة مخزون')
        verbose_name_plural = _('دفعات المخزون')
        # FEFO: the nearest expiry goes first; no / equal expiry → FIFO (oldest first)
        ordering = [F('expiry_date').asc(nulls_last=True), 'created_at', 'id']
        indexes = [
            models.Index(fields=['quantity_remaining', 'created_at']),
            models.Index(fields=['product', 'quantity_remaining']),
            models.Index(fields=['product', 'warehouse', 'quantity_remaining']),
        ]

    def __str__(self):
        return (
            f'{self.product.name} | {self.warehouse.name} | '
            f'متبقي: {self.quantity_remaining} | تكلفة: {self.unit_cost}'
        )

    @property
    def fifo_age_days(self):
        """Returns how many days old this batch is."""
        from django.utils import timezone
        now = timezone.now().date()
        return (now - self.created_at.date()).days

    @property
    def fifo_status(self):
        """
        Returns a status string based on how old the batch is.
        Used to highlight aging inventory in the UI.

        Returns:
            str: 'CRITICAL' (0-7 days), 'WARNING' (8-30 days),
                 'MODERATE' (31-180 days), 'OK' (180+ days)
        """
        age = self.fifo_age_days
        if age <= 7:
            return 'CRITICAL'
        elif age <= 30:
            return 'WARNING'
        elif age <= 180:
            return 'MODERATE'
        return 'OK'

    @property
    def is_expired(self):
        """Returns True if the batch has passed its expiry date."""
        if self.expiry_date is None:
            return False
        return self.expiry_date < datetime.date.today()

    @property
    def days_until_expiry(self):
        """Returns days until expiry (negative if already expired)."""
        if self.expiry_date is None:
            return None
        return (self.expiry_date - datetime.date.today()).days

    @property
    def total_value(self):
        """Total value of remaining stock in this batch."""
        return self.quantity_remaining * self.unit_cost

    def clean(self):
        from apps.tenant.core.models import SystemSetting
        # Master Lock: Global setting strictly governs negative stock
        allow_neg = getattr(SystemSetting.get_settings(), 'allow_negative_stock', False)
        if not allow_neg and self.quantity_remaining < 0:
            raise ValidationError('الكمية المتبقية لا يمكن أن تكون سالبة.')
        if self.quantity_original > Decimal('0') and self.quantity_remaining > self.quantity_original:
            raise ValidationError('الكمية المتبقية لا يمكن أن تتجاوز الكمية الأصلية.')


# ---------------------------------------------------------------------------
# StockMovement (audit trail)
# ---------------------------------------------------------------------------

class StockMovement(models.Model):
    """
    Immutable audit log of every inventory change.
    Created automatically by the FIFO engine and purchase service.
    """
    MOVEMENT_TYPES = [
        ('IN', 'وارد - شراء'),
        ('OUT', 'صادر - بيع'),
        ('TRANSFER_IN', 'تحويل وارد'),
        ('TRANSFER_OUT', 'تحويل صادر'),
        ('ADJUSTMENT_IN', 'تسوية زيادة'),
        ('ADJUSTMENT_OUT', 'تسوية نقص'),
        ('OPENING_BALANCE', 'رصيد افتتاحي'),
        ('RETURN_IN', 'مرتجع وارد'),
        ('RETURN_OUT', 'مرتجع صادر'),
        ('CANCEL_IN', 'إلغاء بيع — رجوع للدفعة الأصلية'),
        ('CANCEL_OUT', 'إلغاء مرتجع — خروج من دفعة المرتجع'),
        ('OPENING_ADJ_IN', 'تعديل رصيد أول المدة — زيادة'),
        ('OPENING_ADJ_OUT', 'تعديل رصيد أول المدة — نقص'),
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='movements',
        verbose_name=_('المنتج')
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name='movements',
        verbose_name=_('المستودع')
    )
    batch = models.ForeignKey(
        InventoryBatch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='movements',
        verbose_name=_('الدفعة')
    )
    movement_type = models.CharField(
        max_length=20, choices=MOVEMENT_TYPES, verbose_name=_('نوع الحركة')
    )
    quantity = models.DecimalField(
        max_digits=18, decimal_places=4, verbose_name=_('الكمية')
    )
    unit_cost = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'),
        verbose_name=_('تكلفة الوحدة')
    )
    reference = models.CharField(
        max_length=100, blank=True, verbose_name=_('المرجع')
    )
    notes = models.TextField(blank=True, verbose_name=_('ملاحظات'))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_('بواسطة'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('تاريخ الحركة'))

    def save(self, *args, **kwargs):
        if self.created_by_id is None and self._state.adding:
            from apps.tenant.core.audit import _current_user
            self.created_by = _current_user()          # the user of the current request (None for jobs)
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = _('حركة مخزون')
        verbose_name_plural = _('حركات المخزون')
        ordering = ['-created_at']

    def __str__(self):
        return (
            f'{self.get_movement_type_display()} | {self.product.name} | '
            f'{self.quantity} | {self.created_at.strftime("%Y-%m-%d")}'
        )


# ---------------------------------------------------------------------------
# StockAdjustment (Write-offs / Adjustments / Opening Balances)
# ---------------------------------------------------------------------------

class StockAdjustment(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'مسودة'),
        ('POSTED', 'مرحل'),
    ]
    ADJUSTMENT_TYPE_CHOICES = [
        ('OUT', 'إعدام / تسوية نقص'),
        ('IN', 'تسوية بالزيادة'),
        ('OPENING', 'رصيد افتتاحي'),
        ('STOCKTAKE', 'جرد فعلي وتسوية أرصدة'),
    ]

    adjustment_type = models.CharField(
        max_length=15, choices=ADJUSTMENT_TYPE_CHOICES, default='OUT', verbose_name=_('نوع العملية')
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name='adjustments',
        verbose_name=_('المستودع')
    )
    date = models.DateField(default=datetime.date.today, verbose_name=_('التاريخ'))
    reason = models.CharField(max_length=255, verbose_name=_('السبب / البيان'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='DRAFT', verbose_name=_('الحالة'))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('بواسطة')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('تسوية مخزون')
        verbose_name_plural = _('تسويات المخزون')
        ordering = ['-date', '-created_at']
        permissions = [
            ("perform_stocktake", "إجراء واعتماد الجرد الفعلي وتسوية الأرصدة"),
        ]

    def __str__(self):
        return f'تسوية #{self.id} - {self.warehouse.name}'


class StockAdjustmentLine(models.Model):
    adjustment = models.ForeignKey(
        StockAdjustment,
        on_delete=models.CASCADE,
        related_name='lines',
        verbose_name=_('التسوية')
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='adjustment_lines',
        verbose_name=_('المنتج')
    )
    uom = models.ForeignKey(
        'ProductUoM',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('الوحدة')
    )
    conversion_factor = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal('1.0'),
        null=True,
        blank=True,
        verbose_name=_('معامل التحويل')
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4, verbose_name=_('كمية التسوية / الفارق'))
    system_quantity = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name=_('الرصيد الدفتري')
    )
    physical_quantity = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name=_('الرصيد الفعلي (على الرف)')
    )
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name=_('تكلفة الوحدة (تقديرية)'))

    class Meta:
        verbose_name = _('بند تسوية')
        verbose_name_plural = _('بنود التسوية')

    def __str__(self):
        return f'{self.product.name} - {self.quantity}'

    @property
    def display_unit_name(self):
        if self.uom and getattr(self.uom, 'uom', None):
            return self.uom.uom.name
        return getattr(self.product, 'pos_unit_name', None) or ''

    @property
    def factor(self):
        if self.conversion_factor and self.conversion_factor > Decimal('0'):
            return self.conversion_factor
        if self.uom and self.uom.conversion_factor:
            return self.uom.conversion_factor
        return Decimal('1.0')

    @property
    def base_quantity(self):
        qty = self.quantity or Decimal('0')
        return (qty * self.factor).quantize(Decimal('0.0001'))

    @property
    def base_unit_cost(self):
        cost = self.unit_cost or Decimal('0')
        if cost <= Decimal('0'):
            return Decimal('0')
        f = self.factor
        if f > Decimal('0'):
            return (cost / f).quantize(Decimal('0.0001'))
        return cost

# ---------------------------------------------------------------------------
# Serial Item (For SERIALIZED products like Mobile Phones)
# ---------------------------------------------------------------------------
class SerialItem(models.Model):
    """
    سجل تتبع السيريالات/الأجهزة (للأصناف من نوع SERIALIZED)
    يُنشأ تلقائياً عند شراء الجهاز، ويُباع باختيار السيريال.
    """
    CONDITION_NEW = 'NEW'
    CONDITION_USED = 'USED'
    
    CONDITION_CHOICES = [
        (CONDITION_NEW, 'جديد'),
        (CONDITION_USED, 'مستعمل'),
    ]
    
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='serials', verbose_name=_('المنتج (الموديل)'))
    serial_number = models.CharField(max_length=100, null=True, blank=True, verbose_name=_('رقم السيريال (S/N)'))
    imei_1 = models.CharField(max_length=20, null=True, blank=True, verbose_name=_('IMEI 1'))
    imei_2 = models.CharField(max_length=20, null=True, blank=True, verbose_name=_('IMEI 2'))
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default=CONDITION_NEW, verbose_name=_('الحالة'))
    
    # تفاصيل الأجهزة المستعملة
    storage = models.CharField(max_length=50, blank=True, null=True, verbose_name=_('مساحة التخزين'))
    ram = models.CharField(max_length=50, blank=True, null=True, verbose_name=_('الرام'))
    notes = models.TextField(blank=True, null=True, verbose_name=_('ملاحظات'))
    
    # التتبع
    warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True, related_name='serial_items', verbose_name=_('مستودع التخزين'))
    is_sold = models.BooleanField(default=False, verbose_name=_('تم بيعه'))
    is_returned = models.BooleanField(default=False, verbose_name=_('مرتجع للمورد'))
    
    # التكلفة الخاصة بهذه القطعة تحديداً
    actual_cost = models.DecimalField(max_digits=18, decimal_places=4, verbose_name=_('التكلفة الفعلية لهذه القطعة'))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('منتج بسيريال')
        verbose_name_plural = _('المنتجات بالسيريال')
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['imei_1'], condition=models.Q(imei_1__isnull=False) & ~models.Q(imei_1=''),
                                    name='unique_non_empty_imei_1'),
            models.UniqueConstraint(fields=['serial_number'],
                                    condition=models.Q(serial_number__isnull=False) & ~models.Q(serial_number=''),
                                    name='unique_non_empty_serial'),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.serial_number} ({self.get_condition_display()})"


class StockTransfer(models.Model):
    """
    Multi-item stock transfer document between two warehouses (same or another
    branch). Posting moves every line with FIFO cost (fifo_engine.transfer_stock)
    in ONE transaction: either all lines move or none.
    """
    STATUS_POSTED = 'POSTED'
    STATUS_CHOICES = [(STATUS_POSTED, _('مرحل'))]

    number = models.CharField(max_length=30, unique=True, editable=False, verbose_name=_('رقم التحويل'))
    date = models.DateField(default=datetime.date.today, verbose_name=_('التاريخ'))
    from_warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='stock_transfers_out',
                                       verbose_name=_('من مستودع'))
    to_warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='stock_transfers_in',
                                     verbose_name=_('إلى مستودع'))
    notes = models.TextField(blank=True, default='', verbose_name=_('ملاحظات'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_POSTED, verbose_name=_('الحالة'))
    total_cost = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name=_('إجمالي التكلفة'))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='stock_transfers', verbose_name=_('بواسطة'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('تحويل مخزني')
        verbose_name_plural = _('التحويلات المخزنية')
        ordering = ['-date', '-id']

    def __str__(self):
        return self.number

    @property
    def is_inter_branch(self):
        return bool(self.from_warehouse.branch_id and self.to_warehouse.branch_id
                    and self.from_warehouse.branch_id != self.to_warehouse.branch_id)

    def save(self, *args, **kwargs):
        if not self.number:
            from django.db import transaction
            from apps.tenant.core.sequences import max_numeric_suffix, next_sequence
            prefix = f'STK-{(self.date or datetime.date.today()).year}-'
            with transaction.atomic():
                n = next_sequence(f'stock_transfer:{prefix}',
                                  seed=lambda: max_numeric_suffix(StockTransfer.objects.all(), 'number', prefix))
                self.number = f'{prefix}{n:05d}'
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)


class StockTransferLine(models.Model):
    transfer = models.ForeignKey(StockTransfer, on_delete=models.CASCADE, related_name='lines', verbose_name=_('التحويل'))
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='stock_transfer_lines', verbose_name=_('الصنف'))
    uom = models.ForeignKey(ProductUoM, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_('الوحدة'))
    quantity = models.DecimalField(max_digits=18, decimal_places=4, verbose_name=_('الكمية (بالوحدة المختارة)'))
    conversion_factor = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('1'), verbose_name=_('معامل التحويل'))
    base_quantity = models.DecimalField(max_digits=18, decimal_places=4, verbose_name=_('الكمية بالوحدة الأساسية'))
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0'), verbose_name=_('متوسط تكلفة الوحدة الأساسية'))

    class Meta:
        verbose_name = _('بند تحويل مخزني')
        verbose_name_plural = _('بنود التحويل المخزني')

    @property
    def line_cost(self):
        return (self.base_quantity or Decimal('0')) * (self.unit_cost or Decimal('0'))

    @property
    def uom_name(self):
        return self.uom.uom.name if self.uom_id else self.product.pos_unit_name


class ProductBarcode(models.Model):
    """باركود إضافي للصنف (نفس الصنف بأكثر من باركود: موردين أو تعبئات مختلفة).
    uom = وحدة الباركود (فاضي = الوحدة الأساسية). البحث: inventory.barcodes.find_by_barcode."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='extra_barcodes',
                                verbose_name=_('الصنف'))
    uom = models.ForeignKey(ProductUoM, on_delete=models.CASCADE, null=True, blank=True, related_name='extra_barcodes',
                            verbose_name=_('الوحدة'))
    barcode = models.CharField(max_length=100, unique=True, verbose_name=_('الباركود'))
    note = models.CharField(max_length=150, blank=True, default='', verbose_name=_('ملاحظة (المورد / التعبئة)'))

    class Meta:
        verbose_name = _('باركود إضافي')
        verbose_name_plural = _('باركودات إضافية')
        ordering = ['id']

    def __str__(self):
        return f'{self.barcode} → {self.product}'
