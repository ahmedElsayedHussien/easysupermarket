"""
Core models - Branch.
The Branch is the fundamental organizational unit within each tenant.
"""
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    ADMIN = 'ADMIN', 'مدير النظام'
    MANAGER = 'MANAGER', 'مدير فرع'
    ACCOUNTANT = 'ACCOUNTANT', 'محاسب'
    CASHIER = 'CASHIER', 'كاشير'
    INVENTORY = 'INVENTORY', 'أمين مخزن'
    SALES_REP = 'SALES_REP', 'مندوب مبيعات'


class Employee(models.Model):
    user = models.OneToOneField('auth.User', on_delete=models.CASCADE, related_name='employee_profile', verbose_name=_('المستخدم'))
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CASHIER, verbose_name=_('الدور'))
    branch = models.ForeignKey('Branch', on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_('الفرع'))

    # HR & Payroll Fields
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0.00'), verbose_name=_('قيمة ساعة العمل العادية'))
    daily_working_hours = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('8.00'), verbose_name=_('ساعات العمل اليومية'))
    shift_start_time = models.TimeField(null=True, blank=True, verbose_name=_('ميعاد الحضور'))
    shift_end_time = models.TimeField(null=True, blank=True, verbose_name=_('ميعاد الانصراف'))
    deduction_per_hour = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0.00'), verbose_name=_('قيمة الخصم للساعة'))
    overtime_per_hour = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0.00'), verbose_name=_('قيمة الإضافي للساعة'))
    base_salary = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('الراتب الأساسي (اختياري)'))
    is_insured = models.BooleanField(default=True, verbose_name=_('مؤمَّن عليه'))
    insurance_wage = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                         verbose_name=_('الأجر التأميني (فاضي = الأساسي)'))
    tax_exempt = models.BooleanField(default=False, verbose_name=_('معفى من ضريبة كسب العمل'))

    class Meta:
        verbose_name = _('موظف')
        verbose_name_plural = _('الموظفين')

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

    def is_sales_rep(self):
        return self.role == Role.SALES_REP

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

    # Per-branch POS payment defaults (fall back to SystemSetting when empty).
    # Use apps.tenant.core.settings_helpers.get_pos_defaults(branch) — never read these directly.
    default_pos_treasury = models.ForeignKey('accounting.Treasury', on_delete=models.SET_NULL, null=True, blank=True,
                                             related_name='default_for_branch_pos', verbose_name=_('الخزينة الافتراضية لنقطة البيع بالفرع'))
    default_pos_bank_account = models.ForeignKey('accounting.BankAccount', on_delete=models.SET_NULL, null=True, blank=True,
                                                 related_name='default_card_for_branch_pos', verbose_name=_('حساب الفيزا الافتراضي بالفرع'))
    default_pos_instapay_account = models.ForeignKey('accounting.BankAccount', on_delete=models.SET_NULL, null=True, blank=True,
                                                     related_name='default_instapay_for_branch_pos', verbose_name=_('حساب إنستاباي الافتراضي بالفرع'))
    default_pos_ewallet = models.ForeignKey('accounting.EWallet', on_delete=models.SET_NULL, null=True, blank=True,
                                            related_name='default_for_branch_pos', verbose_name=_('المحفظة الافتراضية بالفرع'))

    # The branch as registered at the Tax Authority (e-invoice issuer). Empty = the main settings.
    eta_branch_code = models.CharField(max_length=20, blank=True, default='', verbose_name=_('كود الفرع لدى الضرائب'),
                                       help_text=_('فاضي = كود وعنوان إعدادات الفاتورة الإلكترونية (المركز الرئيسي).'))
    eta_governate = models.CharField(max_length=100, blank=True, default='', verbose_name=_('المحافظة (الضرائب)'))
    eta_region_city = models.CharField(max_length=100, blank=True, default='', verbose_name=_('المدينة / المنطقة (الضرائب)'))
    eta_street = models.CharField(max_length=200, blank=True, default='', verbose_name=_('الشارع (الضرائب)'))
    eta_building_number = models.CharField(max_length=50, blank=True, default='', verbose_name=_('رقم المبنى (الضرائب)'))

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
    pos_price_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.0'), verbose_name=_('هامش التعديل المسموح للسعر (%)'))
    rep_allow_price_edit = models.BooleanField(
        default=False, verbose_name=_('السماح للمندوب بتعديل سعر البيع من الموبايل'),
        help_text=_('أدنى سعر بيع للصنف بيفضل شغال دايماً. في المرتجع المندوب يقدر ينزّل السعر بس.'))
    rep_price_margin_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        verbose_name=_('هامش تعديل السعر المسموح للمندوب (%) — 0 = من غير حد'))
    rep_allow_return_without_invoice = models.BooleanField(
        default=True, verbose_name=_('السماح للمندوب باستلام مرتجع بدون فاتورة بيع'),
        help_text=_('لعميل مسجّل بس، بسعر ميزيدش عن سعر البيع الحالي، والفاتورة بتتعلّم «بدون فاتورة» للمراجعة.'))
    rep_allow_cancel_invoice = models.BooleanField(
        default=False, verbose_name=_('السماح للمندوب بإلغاء فواتيره'),
        help_text=_('فواتيره هو بس، بتاعة النهارده، بسبب مكتوب. بيرجّع المخزون لنفس الدفعات ويعكس القيود. '
                    'الفاتورة اللي اتبعتت للضرايب أو عليها مرتجع إلغاؤها من الإدارة.'))
    rep_return_without_invoice_max = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0'),
        verbose_name=_('أقصى قيمة لمرتجع المندوب بدون فاتورة — 0 = من غير حد'))
    max_pos_discount_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('100'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name=_('الحد الأقصى لنسبة الخصم في نقطة البيع (%)'),
        help_text=_('أقصى نسبة خصم إجمالية مسموحة لكل صنف وللفاتورة لغير المديرين (0 - 100). المديرون مستثنون.'),
    )
    apply_vat_by_default = models.BooleanField(default=True, verbose_name=_('تطبيق الضريبة الافتراضي'))
    large_amount_requires_customer = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'), help_text=_('0 تعني غير مفعل'), verbose_name=_('إلزام اختيار عميل للمبالغ الأكبر من'))
    
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
    store_phone = models.CharField(max_length=50, blank=True, verbose_name=_('هاتف المحل'),
                                   help_text=_('يظهر على الفواتير المطبوعة. فارغ = هاتف الفرع.'))
    store_address = models.CharField(max_length=255, blank=True, verbose_name=_('عنوان المحل'),
                                     help_text=_('يظهر على الفواتير المطبوعة. فارغ = عنوان الفرع.'))
    tax_number = models.CharField(max_length=100, blank=True, verbose_name=_('الرقم الضريبي'))
    commercial_register = models.CharField(max_length=100, blank=True, verbose_name=_('السجل التجاري'))
    receipt_footer = models.TextField(blank=True, verbose_name=_('رسالة أسفل الإيصال'))
    company_logo = models.ImageField(upload_to='company/', null=True, blank=True, verbose_name=_('شعار الشركة (للإيصال والتقارير)'))
    receipt_paper_width = models.CharField(max_length=2, choices=[('58', '58 مم'), ('80', '80 مم')], default='80',
                                           verbose_name=_('مقاس ورق طابعة الإيصال'))
    show_branch_info_on_receipt = models.BooleanField(default=True, verbose_name=_('إظهار عنوان وتليفون الفرع على الإيصال'))

    # --- Accounting ---
    accounting_lock_date = models.DateField(
        null=True, blank=True, verbose_name=_('تاريخ قفل الفترة المحاسبية'),
        help_text=_('لا يمكن إضافة أو تعديل أو إلغاء قيود بتاريخ في هذا اليوم أو قبله. اتركه فارغاً لعدم القفل.'))
    pos_cash_rounding = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0'),
        choices=[(Decimal('0.00'), 'بدون تقريب'), (Decimal('0.25'), 'لأقرب 0.25 ج.م'),
                 (Decimal('0.50'), 'لأقرب 0.50 ج.م'), (Decimal('1.00'), 'لأقرب 1 ج.م')],
        verbose_name=_('تقريب المدفوع نقداً في نقطة البيع'),
        help_text=_('الفرق يُسجل على حساب فروق تقريب نقطة البيع (5115).'))

    # --- Thresholds used by the dashboards ---
    stagnant_stock_days = models.PositiveIntegerField(default=60, verbose_name=_('أيام اعتبار الصنف راكداً (بدون صرف)'))
    expiry_warning_days = models.PositiveIntegerField(default=30, verbose_name=_('التنبيه قبل انتهاء الصلاحية بـ (يوم)'))
    dormant_debt_days = models.PositiveIntegerField(default=60, verbose_name=_('أيام اعتبار المديونية راكدة (بلا حركة)'))
    credit_limit_warning_percent = models.PositiveIntegerField(
        default=80, validators=[MinValueValidator(1), MaxValueValidator(100)],
        verbose_name=_('نسبة التحذير من حد الائتمان (%)'))

    # --- HR ---
    weekly_off_days = models.CharField(
        max_length=20, default='4', blank=True, verbose_name=_('أيام الإجازة الأسبوعية'),
        help_text=_('أرقام أيام الأسبوع مفصولة بفاصلة: 0=الاثنين 1=الثلاثاء 2=الأربعاء 3=الخميس 4=الجمعة 5=السبت 6=الأحد'))
    payroll_day_basis = models.CharField(
        max_length=15, default='CALENDAR_30',
        choices=[('CALENDAR_30', 'أجر اليوم = المرتب ÷ 30'), ('WORKING_DAYS', 'أجر اليوم = المرتب ÷ أيام العمل الفعلية بالشهر')],
        verbose_name=_('طريقة حساب أجر اليوم'))
    late_grace_minutes = models.PositiveIntegerField(default=0, verbose_name=_('سماحية التأخير قبل الخصم (دقيقة)'))

    # --- Security ---
    session_timeout_hours = models.PositiveIntegerField(
        default=12, validators=[MinValueValidator(1), MaxValueValidator(720)], verbose_name=_('مدة بقاء الجلسة (ساعة)'))
    login_max_attempts = models.PositiveIntegerField(
        default=5, validators=[MinValueValidator(3), MaxValueValidator(50)], verbose_name=_('عدد محاولات الدخول الخاطئة قبل القفل'))
    login_lockout_minutes = models.PositiveIntegerField(
        default=15, validators=[MinValueValidator(1), MaxValueValidator(1440)], verbose_name=_('مدة قفل الدخول (دقيقة)'))

    # --- Backups ---
    auto_backup_enabled = models.BooleanField(default=False, verbose_name=_('نسخ احتياطي تلقائي يومي'))
    auto_backup_retention = models.PositiveIntegerField(
        default=7, validators=[MinValueValidator(1), MaxValueValidator(365)], verbose_name=_('عدد النسخ التلقائية المحفوظة'))
    
    # Sidebar Navigation Visibility Settings
    # Quick Navigation
    show_nav_dashboard = models.BooleanField(default=True, verbose_name=_('لوحة التحكم الرئيسية'))
    show_nav_pos = models.BooleanField(default=True, verbose_name=_('نقطة البيع'))

    # Sales
    show_nav_quotations = models.BooleanField(default=True, verbose_name=_('عروض الأسعار'))
    show_nav_sales_invoices = models.BooleanField(default=True, verbose_name=_('فواتير المبيعات'))
    show_nav_pricelists = models.BooleanField(default=True, verbose_name=_('قوائم الأسعار'))
    show_nav_sales_reps = models.BooleanField(default=True, verbose_name=_('مناديب المبيعات'))
    show_nav_loading_orders = models.BooleanField(default=True, verbose_name=_('طلبات التحميل'))
    show_nav_unloading_orders = models.BooleanField(default=True, verbose_name=_('طلبات التفريغ'))
    show_nav_rep_handovers = models.BooleanField(default=True, verbose_name=_('توريدات المناديب'))
    show_nav_rep_app = models.BooleanField(default=True, verbose_name=_('واجهة المندوب'))

    # Purchases
    show_nav_purchase_invoices = models.BooleanField(default=True, verbose_name=_('فواتير المشتريات'))
    show_nav_purchase_orders = models.BooleanField(default=True, verbose_name=_('أوامر الشراء'))
    show_nav_create_purchase = models.BooleanField(default=True, verbose_name=_('إنشاء فاتورة مشتريات'))

    # Inventory
    show_nav_inventory_dashboard = models.BooleanField(default=True, verbose_name=_('لوحة المخزون'))

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
    show_nav_hr_attendance_manage = models.BooleanField(default=True, verbose_name=_('إدخال الحضور اليدوي'))
    show_nav_hr_attendance_report = models.BooleanField(default=True, verbose_name=_('تقرير الحضور والرواتب'))
    show_nav_hr_payroll = models.BooleanField(default=True, verbose_name=_('مسير الرواتب'))
    show_nav_hr_employees = models.BooleanField(default=True, verbose_name=_('الموظفين'))
    show_nav_hr_my_portal = models.BooleanField(default=True, verbose_name=_('صفحتي'))
    show_nav_hr_leaves = models.BooleanField(default=True, verbose_name=_('الإجازات'))
    show_nav_hr_loans = models.BooleanField(default=True, verbose_name=_('سلف الموظفين'))

    # Partners
    show_nav_partners_dashboard = models.BooleanField(default=True, verbose_name=_('لوحة الشركاء'))

    # Accounting
    show_nav_accounting_dashboard = models.BooleanField(default=True, verbose_name=_('لوحة المحاسبة'))
    show_nav_liquidity_dashboard = models.BooleanField(default=True, verbose_name=_('إدارة السيولة'))
    show_nav_fiscal_years = models.BooleanField(default=True, verbose_name=_('إقفال السنة المالية'))
    show_nav_account_mapping = models.BooleanField(default=True, verbose_name=_('ربط الحسابات'))
    show_nav_intermediaries = models.BooleanField(default=True, verbose_name=_('الشركات الوسيطة'))
    show_nav_tax_settlement = models.BooleanField(default=True, verbose_name=_('التسوية الضريبية'))
    show_nav_bank_reconciliation = models.BooleanField(default=True, verbose_name=_('التسوية البنكية'))
    show_nav_custody = models.BooleanField(default=True, verbose_name=_('العهد النقدية'))
    show_nav_eservices = models.BooleanField(default=True, verbose_name=_('خدمات الدفع'))

    # Administration
    show_nav_admin_dashboard = models.BooleanField(default=True, verbose_name=_('لوحة المدير'))
    show_nav_branches = models.BooleanField(default=True, verbose_name=_('الفروع والكيانات'))
    show_nav_users = models.BooleanField(default=True, verbose_name=_('المستخدمين'))
    show_nav_audit_log = models.BooleanField(default=True, verbose_name=_('سجل التعديلات'))
    show_nav_reports = models.BooleanField(default=True, verbose_name=_('التقارير الشاملة'))

    # Tax & E-Invoicing
    show_nav_einvoice_approval = models.BooleanField(default=True, verbose_name=_('إرسال للتوقيع'))
    show_nav_einvoice_history = models.BooleanField(default=True, verbose_name=_('سجل الفواتير المرسلة'))
    show_nav_received_documents = models.BooleanField(default=True, verbose_name=_('الفواتير الواردة من الموردين'))
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
        # A singleton: always row 1. A second, new instance used to be dropped silently (its values
        # lost without a word) — now it's an error; edit SystemSetting.get_settings() instead.
        if self.pk is None:
            if SystemSetting.objects.exists():
                raise ValueError('إعدادات النظام صف واحد بس — عدّل SystemSetting.get_settings() بدل ما تعمل جديد.')
            self.pk = 1
        return super().save(*args, **kwargs)

    @property
    def company_name(self):
        """Legacy alias used by older views/templates: the organisation name is
        store_name (Settings > اسم المتجر). Prefer core.company.company_profile."""
        return self.store_name or ''

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


class AuditLog(models.Model):
    """سجل التعديلات: مين عمل إيه وإمتى، وإيه اللي اتغير (قبل ← بعد).
    Written by core.audit (signals on the tracked models + logins); never edited."""
    CREATE = 'CREATE'
    UPDATE = 'UPDATE'
    DELETE = 'DELETE'
    LOGIN = 'LOGIN'
    LOGOUT = 'LOGOUT'
    ACTION_CHOICES = (
        (CREATE, _('إضافة')), (UPDATE, _('تعديل')), (DELETE, _('حذف')),
        (LOGIN, _('تسجيل دخول')), (LOGOUT, _('تسجيل خروج')),
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_('الوقت'))
    user = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                             verbose_name=_('المستخدم'))
    action = models.CharField(max_length=10, choices=ACTION_CHOICES, db_index=True, verbose_name=_('العملية'))
    content_type = models.ForeignKey('contenttypes.ContentType', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+', verbose_name=_('القسم'))
    object_id = models.CharField(max_length=64, blank=True, default='', db_index=True, verbose_name=_('رقم السجل'))
    object_repr = models.CharField(max_length=255, blank=True, default='', verbose_name=_('السجل'))
    changes = models.JSONField(default=dict, blank=True, verbose_name=_('التغييرات'))
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name=_('IP'))

    class Meta:
        verbose_name = _('سجل تعديل')
        verbose_name_plural = _('سجل التعديلات')
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['content_type', 'object_id'], name='audit_object_idx')]

    def save(self, *args, **kwargs):
        # append-only: a log line is written once (core.audit) and never changed
        if not self._state.adding:
            raise ValueError('سجل التعديلات مينفعش يتعدّل بعد ما اتكتب.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('سجل التعديلات مينفعش يتمسح.')

    def __str__(self):
        return f'{self.get_action_display()} {self.object_repr}'
