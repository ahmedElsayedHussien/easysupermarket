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
from django.db.models import Sum
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
        on_delete=models.CASCADE,
        related_name='warehouses',
        verbose_name=_('الفرع')
    )
    name = models.CharField(max_length=200, verbose_name=_('اسم المستودع'))
    code = models.CharField(max_length=20, unique=True, verbose_name=_('كود المستودع'))
    is_cold_storage = models.BooleanField(default=False, verbose_name=_('تبريد'))
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
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name=_('سعر البيع')
    )
    min_sale_price = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name=_('أدنى سعر بيع')
    )
    last_purchase_price = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
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
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name=_('حد أدنى للمخزون')
    )
    max_stock_level = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name=_('حد أقصى للمخزون')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('نشط'))
    allow_negative_stock = models.BooleanField(
        default=False, verbose_name=_('السماح بمخزون سالب')
    )
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
            product=self,
            quantity_remaining__gt=Decimal('0')
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
        total_qty = qs.aggregate(q=Sum('quantity_remaining'))['q'] or Decimal('0')
        if total_qty == 0:
            return self.last_purchase_price
        # Weighted average: sum(qty * cost) / total_qty
        total_value = sum(
            b.quantity_remaining * b.unit_cost for b in qs
        )
        return (total_value / total_qty).quantize(Decimal('0.0001'))

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


# ---------------------------------------------------------------------------
# Unit of Measure Models
# ---------------------------------------------------------------------------

class UnitOfMeasure(models.Model):
    name = models.CharField(max_length=50, verbose_name=_('اسم الوحدة'))

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
        max_digits=10, decimal_places=4, default=1.0, verbose_name=_('معامل التحويل')
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
    sale_price = models.DecimalField(max_digits=15, decimal_places=4, verbose_name=_('سعر البيع بالفرع'))
    min_sale_price = models.DecimalField(max_digits=15, decimal_places=4, verbose_name=_('أقل سعر بيع بالفرع'))
    
    class Meta:
        unique_together = ('product', 'branch')


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
        on_delete=models.CASCADE,
        related_name='batches',
        verbose_name=_('المنتج')
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='batches',
        verbose_name=_('المستودع')
    )
    quantity_original = models.DecimalField(
        max_digits=15, decimal_places=4, verbose_name=_('الكمية الأصلية')
    )
    quantity_remaining = models.DecimalField(
        max_digits=15, decimal_places=4, verbose_name=_('الكمية المتبقية')
    )
    unit_cost = models.DecimalField(
        max_digits=15, decimal_places=4, verbose_name=_('تكلفة الوحدة')
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
        # CRITICAL: This ordering ensures FIFO - oldest batches are consumed first
        ordering = ['created_at']

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
        if self.quantity_remaining < 0:
            raise ValidationError('الكمية المتبقية لا يمكن أن تكون سالبة.')
        if self.quantity_remaining > self.quantity_original:
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
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='movements',
        verbose_name=_('المنتج')
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.CASCADE,
        related_name='movements',
        verbose_name=_('المستودع')
    )
    batch = models.ForeignKey(
        InventoryBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='movements',
        verbose_name=_('الدفعة')
    )
    movement_type = models.CharField(
        max_length=20, choices=MOVEMENT_TYPES, verbose_name=_('نوع الحركة')
    )
    quantity = models.DecimalField(
        max_digits=15, decimal_places=4, verbose_name=_('الكمية')
    )
    unit_cost = models.DecimalField(
        max_digits=15, decimal_places=4, default=Decimal('0'),
        verbose_name=_('تكلفة الوحدة')
    )
    reference = models.CharField(
        max_length=100, blank=True, verbose_name=_('المرجع')
    )
    notes = models.TextField(blank=True, verbose_name=_('ملاحظات'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('تاريخ الحركة'))

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
    ]

    adjustment_type = models.CharField(
        max_length=10, choices=ADJUSTMENT_TYPE_CHOICES, default='OUT', verbose_name=_('نوع العملية')
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
    quantity = models.DecimalField(max_digits=15, decimal_places=4, verbose_name=_('الكمية'))
    unit_cost = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal('0'), verbose_name=_('تكلفة الوحدة (تقديرية)'))

    class Meta:
        verbose_name = _('بند تسوية')
        verbose_name_plural = _('بنود التسوية')

    def __str__(self):
        return f'{self.product.name} - {self.quantity}'

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
    actual_cost = models.DecimalField(max_digits=15, decimal_places=4, verbose_name=_('التكلفة الفعلية لهذه القطعة'))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('منتج بسيريال')
        verbose_name_plural = _('المنتجات بالسيريال')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.product.name} - {self.serial_number} ({self.get_condition_display()})"
