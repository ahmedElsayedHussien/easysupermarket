"""
Pricing models: CustomerSector, PriceList, and PriceListItem.
Supports 3-tier hierarchical pricing:
  Tier 1: Customer-specific Price List (highest priority)
  Tier 2: Sector-specific Price List (secondary priority)
  Tier 3: Product card default price (base/default priority)
"""
from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import Decimal


# ---------------------------------------------------------------------------
# Customer Sector
# ---------------------------------------------------------------------------

class CustomerSector(models.Model):
    """
    Market sector for customers (e.g. Wholesale, Semi-Wholesale, Retail VIP, HORECA, Corporate).
    Used to categorize customers and apply sector-specific price lists.
    """
    name = models.CharField(max_length=150, unique=True, verbose_name='اسم القطاع')
    code = models.CharField(max_length=50, unique=True, blank=True, null=True, verbose_name='كود القطاع')
    description = models.TextField(blank=True, verbose_name='وصف القطاع')
    is_active = models.BooleanField(default=True, verbose_name='نشط')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاريخ الإنشاء')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='تاريخ التحديث')

    class Meta:
        verbose_name = 'قطاع عملاء'
        verbose_name_plural = 'قطاعات العملاء'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def customers_count(self):
        return self.partners.count()


# ---------------------------------------------------------------------------
# Price List
# ---------------------------------------------------------------------------

class PriceList(models.Model):
    """
    Master price list header. Can be assigned directly to a specific Partner,
    to a CustomerSector, or marked as GENERAL.
    """
    CUSTOMER = 'CUSTOMER'
    SECTOR = 'SECTOR'
    GENERAL = 'GENERAL'

    PRICE_LIST_TYPE_CHOICES = [
        (CUSTOMER, 'قائمة مخصصة لعميل'),
        (SECTOR, 'قائمة مخصصة لقطاع'),
        (GENERAL, 'قائمة عامة / موسمية'),
    ]

    name = models.CharField(max_length=200, verbose_name='اسم قائمة الأسعار')
    code = models.CharField(max_length=50, blank=True, null=True, verbose_name='الكود')
    list_type = models.CharField(
        max_length=15, choices=PRICE_LIST_TYPE_CHOICES, default=SECTOR, verbose_name='نوع القائمة'
    )
    partner = models.ForeignKey(
        'partners.Partner',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='price_lists',
        verbose_name='العميل المخصص',
        limit_choices_to={'partner_type__in': ['CUSTOMER', 'BOTH']}
    )
    sector = models.ForeignKey(
        CustomerSector,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='price_lists',
        verbose_name='القطاع المستهدف'
    )
    start_date = models.DateField(null=True, blank=True, verbose_name='تاريخ بدء السريان')
    end_date = models.DateField(null=True, blank=True, verbose_name='تاريخ انتهاء السريان')
    is_active = models.BooleanField(default=True, verbose_name='نشطة')
    notes = models.TextField(blank=True, verbose_name='ملاحظات')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاريخ الإنشاء')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='تاريخ التحديث')

    class Meta:
        verbose_name = 'قائمة أسعار'
        verbose_name_plural = 'قوائم الأسعار'
        ordering = ['-created_at']

    def __str__(self):
        if self.list_type == self.CUSTOMER and self.partner:
            return f"{self.name} [عميل: {self.partner.name}]"
        elif self.list_type == self.SECTOR and self.sector:
            return f"{self.name} [قطاع: {self.sector.name}]"
        return self.name

    def save(self, *args, **kwargs):
        # only the target of the list's own type is kept (a type change never leaves the old target behind)
        if self.list_type != self.CUSTOMER:
            self.partner = None
        if self.list_type != self.SECTOR:
            self.sector = None
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.list_type == self.CUSTOMER and not self.partner:
            raise ValidationError({'partner': 'يجب تحديد العميل عند اختيار قائمة مخصصة لعميل.'})
        if self.list_type == self.SECTOR and not self.sector:
            raise ValidationError({'sector': 'يجب تحديد القطاع عند اختيار قائمة مخصصة لقطاع.'})
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({'end_date': 'تاريخ انتهاء السريان لا يمكن أن يكون قبل تاريخ البدء.'})

    def is_currently_valid(self, as_of_date=None) -> bool:
        if not self.is_active:
            return False
        if as_of_date is None:
            as_of_date = timezone.localdate()
        if self.start_date and as_of_date < self.start_date:
            return False
        if self.end_date and as_of_date > self.end_date:
            return False
        return True


# ---------------------------------------------------------------------------
# Price List Item
# ---------------------------------------------------------------------------

class PriceListItem(models.Model):
    """
    Individual item pricing rule within a PriceList.
    Can be a fixed custom price or a discount percentage off the default price.
    Supports minimum quantity thresholds and optional UoM override.
    """
    FIXED = 'FIXED'
    DISCOUNT = 'DISCOUNT'

    PRICING_METHOD_CHOICES = [
        (FIXED, 'سعر بيع محدد (ثابت)'),
        (DISCOUNT, 'نسبة خصم مئوية % من السعر الأساسي'),
    ]

    price_list = models.ForeignKey(
        PriceList,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='قائمة الأسعار'
    )
    product = models.ForeignKey(
        'inventory.Product',
        on_delete=models.CASCADE,
        related_name='price_list_items',
        verbose_name='المنتج'
    )
    uom = models.ForeignKey(
        'inventory.UnitOfMeasure',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='وحدة القياس (اختياري)'
    )
    pricing_method = models.CharField(
        max_length=15,
        choices=PRICING_METHOD_CHOICES,
        default=FIXED,
        verbose_name='طريقة التسعير'
    )
    custom_price = models.DecimalField(
        max_digits=15,
        decimal_places=4,
        null=True,
        blank=True,
        verbose_name='السعر المخصص (ج.م)'
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name='نسبة الخصم %'
    )
    min_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal('1.000'),
        verbose_name='أقل كمية لتطبيق السعر'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاريخ الإضافة')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='تاريخ التحديث')

    class Meta:
        verbose_name = 'بند قائمة الأسعار'
        verbose_name_plural = 'بنود قائمة الأسعار'
        ordering = ['product__name', '-min_quantity']
        constraints = [
            models.UniqueConstraint(fields=['price_list', 'product', 'min_quantity'], condition=models.Q(uom__isnull=True),
                                    name='uniq_pli_base_unit'),
            models.UniqueConstraint(fields=['price_list', 'product', 'uom', 'min_quantity'],
                                    condition=models.Q(uom__isnull=False), name='uniq_pli_with_unit'),
        ]

    def __str__(self):
        method_desc = f"{self.custom_price} ج.م" if self.pricing_method == self.FIXED else f"خصم {self.discount_percentage}%"
        return f"{self.product.name} ({self.price_list.name}) -> {method_desc}"

    def clean(self):
        super().clean()
        if self.min_quantity is None or self.min_quantity <= 0:
            raise ValidationError({'min_quantity': 'أقل كمية لازم تكون أكبر من صفر.'})
        if self.uom_id and self.product_id and not self.product.uoms.filter(uom_id=self.uom_id).exists():
            raise ValidationError({'uom': 'الوحدة دي مش من وحدات الصنف.'})
        if self.pricing_method == self.FIXED and (self.custom_price is None or self.custom_price < Decimal('0')):
            raise ValidationError({'custom_price': 'يرجى إدخال سعر محدد صحيح.'})
        if self.pricing_method == self.DISCOUNT and (self.discount_percentage is None or self.discount_percentage < Decimal('0') or self.discount_percentage > Decimal('100')):
            raise ValidationError({'discount_percentage': 'نسبة الخصم يجب أن تكون بين 0 و 100%.'})

    def calculate_price(self, base_price: Decimal) -> Decimal:
        """Calculates final price given a base product price."""
        if self.pricing_method == self.DISCOUNT and self.discount_percentage is not None:
            discount_amount = (Decimal(str(base_price)) * Decimal(str(self.discount_percentage))) / Decimal('100')
            calc = Decimal(str(base_price)) - discount_amount
            return max(Decimal('0'), calc).quantize(Decimal('0.0001'))
        elif self.custom_price is not None:
            return Decimal(str(self.custom_price))
        return Decimal(str(base_price))
