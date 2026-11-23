"""
Core models - Branch.
The Branch is the fundamental organizational unit within each tenant.
"""
from django.core.validators import MinValueValidator, MaxValueValidator
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

    # HR & Payroll Fields
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0.00, verbose_name=_('قيمة ساعة العمل العادية'))
    daily_working_hours = models.DecimalField(max_digits=4, decimal_places=2, default=8.00, verbose_name=_('ساعات العمل اليومية'))
    shift_start_time = models.TimeField(null=True, blank=True, verbose_name=_('ميعاد الحضور'))
    shift_end_time = models.TimeField(null=True, blank=True, verbose_name=_('ميعاد الانصراف'))
    deduction_per_hour = models.DecimalField(max_digits=8, decimal_places=2, default=0.00, verbose_name=_('قيمة الخصم للساعة'))
    overtime_per_hour = models.DecimalField(max_digits=8, decimal_places=2, default=0.00, verbose_name=_('قيمة الإضافي للساعة'))
    base_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_('الراتب الأساسي (اختياري)'))

    def is_admin(self):
        # Tenant admin rights come only from the role in THIS schema; the global
        # is_superuser flag is reserved for platform operators (auth_user is shared).
        return self.role == Role.ADMIN or self.user.is_superuser

    def is_manager(self):
        return self.role == Role.MANAGER or self.is_admin()

    def is_accountant(self):
        return self.role == Role.ACCOUNTANT or self.is_admin()

    def is_cashier(self):
        return self.role == Role.CASHIER or self.is_admin()

    def is_inventory(self):
        return self.role == Role.INVENTORY or self.is_admin()

    def save(self, *args, **kwargs):
        # SECURITY: never derive the global is_superuser/is_staff flags from a
        # tenant role. auth_user is shared by all tenants, so doing so would
        # make a tenant admin an admin of every tenant and of the public /admin/.
        # Admin rights are granted via is_admin_user() (role == ADMIN here).
        super().save(*args, **kwargs)

    def __str__(self):
        from apps.tenant.core.usernames import display_username
        return f"{display_username(self.user)} - {self.get_role_display()}"


class Branch(models.Model):
    """
    A physical or logical branch of the supermarket chain.
    Each Branch can have multiple Warehouses.
    """
    name = models.CharField(max_length=200, verbose_name=_('اسم الفرع'))
    code = models.CharField(max_length=20, unique=True, verbose_name=_('كود الفرع'))
    address = models.TextField(blank=True, verbose_name=_('العنوان'))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_('التليفون'))
    
    # Location for GPS Attendance
    latitude = models.DecimalField(max_digits=18, decimal_places=15, null=True, blank=True, verbose_name=_('خط العرض (Latitude)'))
    longitude = models.DecimalField(max_digits=18, decimal_places=15, null=True, blank=True, verbose_name=_('خط الطول (Longitude)'))
    allowed_radius = models.IntegerField(default=50, verbose_name=_('نطاق السماح (بالمتر)'))

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
    show_pos_cost = models.BooleanField(
        default=True,
        verbose_name=_('إظهار سعر التكلفة وآخر شراء في نقطة البيع'),
        help_text=_('عند التفعيل يظهر عمود آخر شراء وتكلفة الوحدة في شاشة نقطة البيع والسلة. أوقفه لإخفاء التكلفة عن الكاشير والعملاء.')
    )
    pos_price_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.0, verbose_name=_('هامش التعديل المسموح للسعر (%)'))
    max_pos_discount_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name=_('الحد الأقصى لنسبة الخصم في نقطة البيع (%)'),
        help_text=_('أقصى نسبة خصم إجمالية مسموحة لكل صنف وللفاتورة لغير المديرين (0 - 100). المديرون مستثنون.'),
    )
    apply_vat_by_default = models.BooleanField(default=True, verbose_name=_('تطبيق الضريبة الافتراضي'))
    large_amount_requires_customer = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text=_('0 تعني غير مفعل'), verbose_name=_('إلزام اختيار عميل للمبالغ الأكبر من'))
    
    # POS Payment Accounts Defaults & Security
    default_pos_treasury = models.ForeignKey('accounting.Treasury', on_delete=models.SET_NULL, null=True, blank=True, related_name='default_for_pos', verbose_name=_('الخزينة الافتراضية لنقطة البيع'))
    default_pos_bank_account = models.ForeignKey('accounting.BankAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='default_for_pos', verbose_name=_('حساب ماكينة الفيزا الافتراضي لنقطة البيع'))
    default_pos_instapay_account = models.ForeignKey('accounting.BankAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='default_instapay_for_pos', verbose_name=_('حساب إنستاباي الافتراضي لنقطة البيع'))
    default_pos_ewallet = models.ForeignKey('accounting.EWallet', on_delete=models.SET_NULL, null=True, blank=True, related_name='default_for_pos', verbose_name=_('المحفظة الإلكترونية الافتراضية لنقطة البيع'))
    lock_pos_payment_accounts = models.BooleanField(default=False, verbose_name=_('قفل حسابات الدفع في نقطة البيع (منع التغيير بواسطة الكاشير)'))
    
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
    
    # Sidebar Navigation Visibility Settings
    # Quick Navigation
    show_nav_dashboard = models.BooleanField(default=True, verbose_name=_('لوحة التحكم الرئيسية'))
    show_nav_pos = models.BooleanField(default=True, verbose_name=_('نقطة البيع'))

    # Sales
    show_nav_quotations = models.BooleanField(default=True, verbose_name=_('عروض الأسعار'))
    show_nav_sales_invoices = models.BooleanField(default=True, verbose_name=_('فواتير المبيعات'))
    show_nav_pricelists = models.BooleanField(default=True, verbose_name=_('قوائم الأسعار'))

    # Purchases
    show_nav_purchase_invoices = models.BooleanField(default=True, verbose_name=_('فواتير المشتريات'))
    show_nav_create_purchase = models.BooleanField(default=True, verbose_name=_('إنشاء فاتورة مشتريات'))

    # Inventory
    show_nav_inventory_fifo = models.BooleanField(default=True, verbose_name=_('تتبع المخزون FIFO'))
    show_nav_stocktake = models.BooleanField(default=True, verbose_name=_('الجرد الفعلي والتسوية'))
    show_nav_adjustments = models.BooleanField(default=True, verbose_name=_('تسويات المخزون'))
    show_nav_products = models.BooleanField(default=True, verbose_name=_('الأصناف'))
    show_nav_uom = models.BooleanField(default=True, verbose_name=_('وحدات القياس'))

    # Rentals
    show_nav_rentals_equipment = models.BooleanField(default=True, verbose_name=_('المعدات (إيجارات)'))
    show_nav_rentals_contracts = models.BooleanField(default=True, verbose_name=_('عقود الإيجار'))
    show_nav_rentals_reports = models.BooleanField(default=True, verbose_name=_('تقارير الإيجارات'))

    # Maintenance
    show_nav_maintenance_tickets = models.BooleanField(default=True, verbose_name=_('تذاكر الصيانة'))

    # Commissions
    show_nav_commission_rules = models.BooleanField(default=True, verbose_name=_('قواعد العمولات'))
    show_nav_commission_report = models.BooleanField(default=True, verbose_name=_('تقرير العمولات'))

    # HR
    show_nav_hr_attendance = models.BooleanField(default=True, verbose_name=_('الحضور والانصراف'))
    show_nav_hr_payroll = models.BooleanField(default=True, verbose_name=_('مسير الرواتب'))

    # Partners
    show_nav_suppliers = models.BooleanField(default=True, verbose_name=_('الموردون'))
    show_nav_customers = models.BooleanField(default=True, verbose_name=_('العملاء'))
    show_nav_sectors = models.BooleanField(default=True, verbose_name=_('قطاعات العملاء'))
    show_nav_partners_report = models.BooleanField(default=True, verbose_name=_('تقرير وحسابات الشركاء'))

    # Accounting
    show_nav_expenses = models.BooleanField(default=True, verbose_name=_('المصروفات'))
    show_nav_receipts = models.BooleanField(default=True, verbose_name=_('سندات القبض'))
    show_nav_payments = models.BooleanField(default=True, verbose_name=_('سندات الصرف'))
    show_nav_transfers = models.BooleanField(default=True, verbose_name=_('تحويل الأموال'))
    show_nav_cheques = models.BooleanField(default=True, verbose_name=_('إدارة الشيكات'))
    show_nav_fixed_assets = models.BooleanField(default=True, verbose_name=_('الأصول الثابتة'))
    show_nav_loans = models.BooleanField(default=True, verbose_name=_('القروض والتمويل'))
    show_nav_journals = models.BooleanField(default=True, verbose_name=_('قيود اليومية'))
    show_nav_chart_of_accounts = models.BooleanField(default=True, verbose_name=_('دليل الحسابات'))
    show_nav_treasuries = models.BooleanField(default=True, verbose_name=_('الخزائن'))
    show_nav_banks = models.BooleanField(default=True, verbose_name=_('حسابات البنوك'))
    show_nav_ewallets = models.BooleanField(default=True, verbose_name=_('المحافظ الإلكترونية'))

    # Administration
    show_nav_admin_dashboard = models.BooleanField(default=True, verbose_name=_('لوحة المدير'))
    show_nav_branches = models.BooleanField(default=True, verbose_name=_('الفروع والكيانات'))
    show_nav_users = models.BooleanField(default=True, verbose_name=_('المستخدمين'))
    show_nav_reports = models.BooleanField(default=True, verbose_name=_('التقارير الشاملة'))

    # Tax & E-Invoicing
    show_nav_einvoice_approval = models.BooleanField(default=True, verbose_name=_('إرسال للتوقيع'))
    show_nav_einvoice_history = models.BooleanField(default=True, verbose_name=_('سجل الفواتير المرسلة'))
    show_nav_tax_settings = models.BooleanField(default=True, verbose_name=_('إعدادات الضرائب'))
    
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



class DocumentSequence(models.Model):
    """
    Race-safe document number counter (one row per prefix, e.g. 'SALE-2026-').
    Always use apps.tenant.core.sequences.next_sequence() — never edit directly.
    """
    key = models.CharField(max_length=100, unique=True)
    last_value = models.BigIntegerField(default=0)

    class Meta:
        verbose_name = _('عداد ترقيم المستندات')
        verbose_name_plural = _('عدادات ترقيم المستندات')

    def __str__(self):
        return f'{self.key}{self.last_value}'
