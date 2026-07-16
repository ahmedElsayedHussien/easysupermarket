"""
Core models - Branch.
The Branch is the fundamental organizational unit within each tenant.
"""
from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    ADMIN = 'ADMIN', 'مدير النظام'
    MANAGER = 'MANAGER', 'مدير فرع'
    ACCOUNTANT = 'ACCOUNTANT', 'محاسب'
    CASHIER = 'CASHIER', 'كاشير'
    INVENTORY = 'INVENTORY', 'أمين مخزن'


class Employee(models.Model):
    user = models.OneToOneField('auth.User', on_delete=models.CASCADE, related_name='employee_profile', verbose_name=_('المستخدم'))
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CASHIER, verbose_name=_('الدور'))
    branch = models.ForeignKey('Branch', on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_('الفرع'))

    def is_admin(self):
        return self.role == Role.ADMIN or self.user.is_superuser

    def is_manager(self):
        return self.role == Role.MANAGER or self.is_admin()

    def is_accountant(self):
        return self.role == Role.ACCOUNTANT or self.is_admin()

    def is_cashier(self):
        return self.role == Role.CASHIER or self.is_admin()

    def is_inventory(self):
        return self.role == Role.INVENTORY or self.is_admin()

    def __str__(self):
        return f"{self.user.username} - {self.get_role_display()}"

class Branch(models.Model):
    """
    A physical or logical branch of the supermarket chain.
    Each Branch can have multiple Warehouses.
    """
    name = models.CharField(max_length=200, verbose_name=_('اسم الفرع'))
    code = models.CharField(max_length=20, unique=True, verbose_name=_('كود الفرع'))
    address = models.TextField(blank=True, verbose_name=_('العنوان'))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_('التليفون'))
    is_active = models.BooleanField(default=True, verbose_name=_('نشط'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('فرع')
        verbose_name_plural = _('الفروع')
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.code})'


class SystemSetting(models.Model):
    # POS Settings
    allow_negative_stock = models.BooleanField(default=False, verbose_name=_('السماح بالبيع بالسالب'))
    auto_print_receipt = models.BooleanField(default=True, verbose_name=_('الطباعة التلقائية للإيصال'))
    allow_pos_price_modification = models.BooleanField(default=False, verbose_name=_('تعديل الأسعار في نقطة البيع'))
    pos_price_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.0, verbose_name=_('هامش التعديل المسموح للسعر (%)'))
    apply_vat_by_default = models.BooleanField(default=True, verbose_name=_('تطبيق الضريبة الافتراضي'))
    large_amount_requires_customer = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text=_('0 تعني غير مفعل'), verbose_name=_('إلزام اختيار عميل للمبالغ الأكبر من'))
    
    # Inventory Settings
    enable_expiry_tracking = models.BooleanField(default=False, verbose_name=_('تتبع تاريخ الصلاحية'))
    low_stock_threshold = models.IntegerField(default=5, verbose_name=_('الحد الأدنى لتنبيهات المخزون'))
    
    # Accounting Settings
    auto_post_journals = models.BooleanField(default=True, verbose_name=_('الترحيل التلقائي للقيود'))
    
    # Printing & Branding
    store_name = models.CharField(max_length=200, blank=True, verbose_name=_('اسم المتجر للإيصال'))
    tax_number = models.CharField(max_length=100, blank=True, verbose_name=_('الرقم الضريبي'))
    commercial_register = models.CharField(max_length=100, blank=True, verbose_name=_('السجل التجاري'))
    receipt_footer = models.TextField(blank=True, verbose_name=_('رسالة أسفل الإيصال'))
    
    class Meta:
        verbose_name = _('إعدادات النظام')
        verbose_name_plural = _('إعدادات النظام')
        permissions = [
            ("view_sales_reports", "Can view sales reports"),
            ("view_purchases_reports", "Can view purchases reports"),
            ("view_inventory_reports", "Can view inventory reports"),
            ("view_accounting_reports", "Can view accounting reports"),
        ]
        
    def save(self, *args, **kwargs):
        # Ensure only one instance exists
        if not self.pk and SystemSetting.objects.exists():
            return SystemSetting.objects.first()
        return super().save(*args, **kwargs)

    @classmethod
    def get_settings(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

