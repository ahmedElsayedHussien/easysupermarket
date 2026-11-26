from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from apps.tenant.core.models import Employee
from apps.tenant.core.usernames import display_username

class Attendance(models.Model):
    STATUS_CHOICES = (
        ('present', _('حاضر')),
        ('absent', _('غائب')),
        ('leave', _('إجازة')),
    )

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendances', verbose_name=_('الموظف'))
    date = models.DateField(default=timezone.now, verbose_name=_('التاريخ'))
    check_in = models.DateTimeField(null=True, blank=True, verbose_name=_('وقت الحضور'))
    check_out = models.DateTimeField(null=True, blank=True, verbose_name=_('وقت الانصراف'))
    delay_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_('ساعات التأخير'))
    overtime_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_('الساعات الإضافية'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='present', verbose_name=_('الحالة'))

    # How the record was made: the employee's location check-in (GPS) or typed in by a
    # manager on the daily attendance sheet (MANUAL) — e.g. when the system runs locally.
    SOURCE_GPS = 'GPS'
    SOURCE_MANUAL = 'MANUAL'
    SOURCE_CHOICES = ((SOURCE_GPS, _('بصمة الموقع')), (SOURCE_MANUAL, _('إدخال يدوي')))
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_GPS, verbose_name=_('طريقة التسجيل'))
    entered_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name=_('أدخله'))
    notes = models.CharField(max_length=255, blank=True, default='', verbose_name=_('ملاحظات'))
    leave_request = models.ForeignKey('hr.LeaveRequest', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='attendance_days', verbose_name=_('طلب الإجازة'))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('سجل حضور')
        verbose_name_plural = _('سجلات الحضور والانصراف')
        unique_together = ('employee', 'date')

    def __str__(self):
        return f"حضور {display_username(self.employee.user)} - {self.date}"

class Payroll(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='payrolls', verbose_name=_('الموظف'))
    month = models.IntegerField(verbose_name=_('الشهر'))
    year = models.IntegerField(verbose_name=_('السنة'))
    total_worked_hours = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'), verbose_name=_('إجمالي ساعات العمل'))
    total_delay_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_('إجمالي ساعات التأخير'))
    total_overtime_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name=_('إجمالي الساعات الإضافية'))
    base_pay = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('المستحق الأساسي'))
    overtime_pay = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('مكافأة الإضافي'))
    deductions = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('إجمالي الخصومات'))
    net_salary = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('الراتب الصافي'))
    # Attendance behind the figures (filled when the payroll is processed)
    working_days = models.PositiveIntegerField(default=0, verbose_name=_('أيام العمل'))
    present_days = models.PositiveIntegerField(default=0, verbose_name=_('أيام الحضور'))
    leave_days = models.PositiveIntegerField(default=0, verbose_name=_('أيام الإجازة'))
    absent_days = models.PositiveIntegerField(default=0, verbose_name=_('أيام الغياب'))
    absence_deduction = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('خصم الغياب'))
    delay_deduction = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('خصم التأخير'))
    loan_deduction = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('أقساط السلف'))
    social_insurance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('تأمينات (حصة الموظف)'))
    employer_insurance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('تأمينات (حصة صاحب العمل)'))
    income_tax = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('ضريبة كسب العمل'))
    commission_pay = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name=_('عمولة مبيعات'))
    is_paid = models.BooleanField(default=False, verbose_name=_('تم الصرف'))
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_('تاريخ الصرف'))
    
    # Linked Journal Entry
    journal_entry = models.OneToOneField('accounting.JournalEntry', null=True, blank=True, on_delete=models.SET_NULL, related_name='payroll', verbose_name=_('قيد الاستحقاق'))

    class Meta:
        verbose_name = _('مسير راتب')
        verbose_name_plural = _('مسيرات الرواتب')
        unique_together = ('employee', 'month', 'year')

    def __str__(self):
        return f"راتب {display_username(self.employee.user)} - {self.month}/{self.year}"


class EmployeeLoan(models.Model):
    """سلفة موظف تُخصم أقساطها من المسير شهرياً.

    CASH      سلفة نقدية اتصرفت من خزينة (قيد: مدين 1135 ذمم الموظفين / دائن الخزينة)
    SHORTAGE  عجز وردية اتحمّل على الكاشير (القيد اتعمل وقت التحميل على 1135)
    The remaining balance = amount - instalments deducted in payrolls (PayrollLoanDeduction).
    """
    CASH = 'CASH'
    SHORTAGE = 'SHORTAGE'
    CUSTODY = 'CUSTODY'
    SOURCE_CHOICES = ((CASH, _('سلفة نقدية')), (SHORTAGE, _('عجز وردية')), (CUSTODY, _('عهدة لم تُسوَّ')))
    ACTIVE = 'ACTIVE'
    SETTLED = 'SETTLED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = ((ACTIVE, _('سارية')), (SETTLED, _('مسددة')), (CANCELLED, _('ملغاة')))

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='loans', verbose_name=_('الموظف'))
    date = models.DateField(default=timezone.localdate, verbose_name=_('التاريخ'))
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=CASH, verbose_name=_('النوع'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('المبلغ'))
    installments_count = models.PositiveIntegerField(default=1, verbose_name=_('عدد الأقساط'))
    monthly_installment = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('القسط الشهري'))
    start_year = models.PositiveIntegerField(verbose_name=_('سنة أول قسط'))
    start_month = models.PositiveSmallIntegerField(verbose_name=_('شهر أول قسط'))
    reason = models.CharField(max_length=255, blank=True, default='', verbose_name=_('السبب'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=ACTIVE, verbose_name=_('الحالة'))
    treasury = models.ForeignKey('accounting.Treasury', on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='+', verbose_name=_('الخزينة'))
    pos_session = models.OneToOneField('invoicing.POSSession', on_delete=models.PROTECT, null=True, blank=True,
                                       related_name='shortage_loan', verbose_name=_('الوردية'))
    journal_entry = models.ForeignKey('accounting.JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+', verbose_name=_('قيد الصرف'))
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name=_('بواسطة'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('سلفة موظف')
        verbose_name_plural = _('سلف الموظفين')
        ordering = ['-date', '-id']

    def __str__(self):
        return f"سلفة {display_username(self.employee.user)} - {self.amount}"

    @property
    def deducted(self):
        if '_deducted' in self.__dict__:          # annotated by hr.loan_services.with_balances()
            return self._deducted or Decimal('0')
        from django.db.models import Sum
        return self.deductions.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    @property
    def remaining(self):
        return max(self.amount - self.deducted, Decimal('0'))


class PayrollLoanDeduction(models.Model):
    """Instalment of a loan deducted in one month's payroll (rebuilt when an unconfirmed
    payroll is recalculated)."""
    payroll = models.ForeignKey(Payroll, on_delete=models.CASCADE, related_name='loan_deductions', verbose_name=_('المسير'))
    loan = models.ForeignKey(EmployeeLoan, on_delete=models.PROTECT, related_name='deductions', verbose_name=_('السلفة'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('القسط المخصوم'))

    class Meta:
        verbose_name = _('قسط سلفة مخصوم')
        verbose_name_plural = _('أقساط السلف المخصومة')
        unique_together = ('payroll', 'loan')


class LeaveType(models.Model):
    """نوع إجازة: عدد أيامه في السنة، وهل بأجر، وهل بيتخصم من رصيد."""
    name = models.CharField(max_length=100, unique=True, verbose_name=_('النوع'))
    days_per_year = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('0'),
                                        verbose_name=_('الأيام في السنة'))
    is_paid = models.BooleanField(default=True, verbose_name=_('بأجر'))
    uses_balance = models.BooleanField(default=True, verbose_name=_('بتتخصم من رصيد'))
    is_active = models.BooleanField(default=True, verbose_name=_('نشط'))

    class Meta:
        verbose_name = _('نوع إجازة')
        verbose_name_plural = _('أنواع الإجازات')
        ordering = ['id']

    def __str__(self):
        return self.name


class LeaveAllocation(models.Model):
    """رصيد خاص لموظف في سنة (لو مختلف عن أيام النوع، أو رصيد مرحّل)."""
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_allocations',
                                 verbose_name=_('الموظف'))
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE, related_name='allocations',
                                   verbose_name=_('النوع'))
    year = models.PositiveIntegerField(verbose_name=_('السنة'))
    days = models.DecimalField(max_digits=5, decimal_places=1, verbose_name=_('الرصيد'))

    class Meta:
        verbose_name = _('رصيد إجازة خاص')
        verbose_name_plural = _('أرصدة الإجازات الخاصة')
        unique_together = ('employee', 'leave_type', 'year')


class LeaveRequest(models.Model):
    """طلب إجازة. الاعتماد بيسجل أيام العمل في الحضور: إجازة (بأجر) أو غياب (بدون أجر)."""
    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = ((PENDING, _('مستني الاعتماد')), (APPROVED, _('معتمدة')), (REJECTED, _('مرفوضة')),
                      (CANCELLED, _('ملغاة')))

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_requests',
                                 verbose_name=_('الموظف'))
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name='requests', verbose_name=_('النوع'))
    start_date = models.DateField(verbose_name=_('من'))
    end_date = models.DateField(verbose_name=_('إلى'))
    days = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('0'), verbose_name=_('أيام العمل'))
    reason = models.CharField(max_length=255, blank=True, default='', verbose_name=_('السبب'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True,
                              verbose_name=_('الحالة'))
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name=_('سجّلها'))
    decided_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name=_('اعتمدها / رفضها'))
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name=_('وقت القرار'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('طلب إجازة')
        verbose_name_plural = _('طلبات الإجازات')
        ordering = ['-start_date', '-id']

    def __str__(self):
        return f'{self.leave_type} — {display_username(self.employee.user)} ({self.start_date} → {self.end_date})'


def _default_brackets():
    # Law 30/2023 (annual, after the personal exemption): upper limit, rate %. None = no limit.
    return [[40000, 0], [55000, 10], [70000, 15], [200000, 20], [400000, 22.5], [1200000, 25], [None, 27.5]]


class PayrollTaxSettings(models.Model):
    """إعدادات ضريبة كسب العمل والتأمينات الاجتماعية في المسير (صف واحد).
    مقفولة افتراضياً؛ النسب والشرايح بتتغير بالقوانين فلازم تتراجع مع المحاسب."""
    tax_enabled = models.BooleanField(default=False, verbose_name=_('حساب ضريبة كسب العمل'))
    personal_exemption = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('20000'),
                                             verbose_name=_('الإعفاء الشخصي السنوي'))
    brackets = models.JSONField(default=_default_brackets, verbose_name=_('الشرائح السنوية'))
    insurance_enabled = models.BooleanField(default=False, verbose_name=_('حساب التأمينات الاجتماعية'))
    employee_insurance_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('11'),
                                                  verbose_name=_('حصة الموظف %'))
    employer_insurance_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('18.75'),
                                                  verbose_name=_('حصة صاحب العمل %'))
    insurance_min_wage = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('2300'),
                                             verbose_name=_('الحد الأدنى للأجر التأميني الشهري'))
    insurance_max_wage = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('14500'),
                                             verbose_name=_('الحد الأقصى للأجر التأميني الشهري'))

    class Meta:
        verbose_name = _('إعدادات ضرائب وتأمينات المرتبات')
        verbose_name_plural = _('إعدادات ضرائب وتأمينات المرتبات')

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PayrollPayment(models.Model):
    """صرف مرتبات شهر (للموظفين) أو سداد مستحقات حكومية عن الشهر (تأمينات + ضريبة كسب العمل).

    SALARIES    مدين 2160 الرواتب المستحقة / دائن الخزينة أو البنك
    GOVERNMENT  مدين 2165 التأمينات المستحقة + مدين 2166 ضريبة كسب العمل / دائن الخزينة أو البنك"""
    SALARIES = 'SALARIES'
    GOVERNMENT = 'GOVERNMENT'
    KIND_CHOICES = ((SALARIES, _('صرف مرتبات')), (GOVERNMENT, _('سداد تأمينات وضرائب')))

    kind = models.CharField(max_length=12, choices=KIND_CHOICES, verbose_name=_('النوع'))
    year = models.PositiveIntegerField(verbose_name=_('السنة'))
    month = models.PositiveSmallIntegerField(verbose_name=_('الشهر'))
    date = models.DateField(default=timezone.localdate, verbose_name=_('التاريخ'))
    amount = models.DecimalField(max_digits=14, decimal_places=2, verbose_name=_('المبلغ'))
    insurance_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'), verbose_name=_('منها تأمينات'))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'), verbose_name=_('منها ضريبة'))
    treasury = models.ForeignKey('accounting.Treasury', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                 verbose_name=_('الخزينة'))
    bank_account = models.ForeignKey('accounting.BankAccount', on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='+', verbose_name=_('الحساب البنكي'))
    journal_entry = models.ForeignKey('accounting.JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+', verbose_name=_('القيد'))
    payrolls = models.ManyToManyField(Payroll, related_name='payments', blank=True, verbose_name=_('المسيرات'))
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name=_('بواسطة'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('صرف / سداد مرتبات')
        verbose_name_plural = _('صرف وسداد المرتبات')
        ordering = ['-date', '-id']

    def __str__(self):
        return f'{self.get_kind_display()} {self.month}/{self.year} — {self.amount}'


class Department(models.Model):
    name = models.CharField(max_length=120, unique=True, verbose_name=_('القسم'))
    manager = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name='managed_departments',
                                verbose_name=_('مدير القسم'))

    class Meta:
        verbose_name = _('قسم')
        verbose_name_plural = _('الأقسام')
        ordering = ['name']

    def __str__(self):
        return self.name


class JobTitle(models.Model):
    name = models.CharField(max_length=120, unique=True, verbose_name=_('الوظيفة'))
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True, related_name='job_titles',
                                   verbose_name=_('القسم'))

    class Meta:
        verbose_name = _('وظيفة')
        verbose_name_plural = _('الوظائف')
        ordering = ['name']

    def __str__(self):
        return self.name


class EmployeeProfile(models.Model):
    """بيانات الموظف الوظيفية (مكمّلة لـ core.Employee)."""
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name='hr_profile', verbose_name=_('الموظف'))
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True, related_name='members',
                                   verbose_name=_('القسم'))
    job_title = models.ForeignKey(JobTitle, on_delete=models.SET_NULL, null=True, blank=True, related_name='members',
                                  verbose_name=_('الوظيفة'))
    hire_date = models.DateField(null=True, blank=True, verbose_name=_('تاريخ التعيين'))
    national_id = models.CharField(max_length=20, blank=True, default="", verbose_name=_('الرقم القومي'))
    phone = models.CharField(max_length=30, blank=True, default="", verbose_name=_('التليفون'))
    address = models.CharField(max_length=255, blank=True, default="", verbose_name=_('العنوان'))

    class Meta:
        verbose_name = _('بيانات وظيفية')
        verbose_name_plural = _('البيانات الوظيفية')


class EmployeeDocument(models.Model):
    TYPE_CHOICES = (('ID', _('بطاقة رقم قومي')), ('CONTRACT', _('عقد عمل')), ('HEALTH', _('شهادة صحية')),
                    ('CRIMINAL', _('فيش جنائي')), ('INSURANCE', _('برنت تأمينات')), ('OTHER', _('أخرى')))
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='documents', verbose_name=_('الموظف'))
    doc_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='OTHER', verbose_name=_('النوع'))
    title = models.CharField(max_length=150, blank=True, default="", verbose_name=_('الوصف'))
    number = models.CharField(max_length=60, blank=True, default="", verbose_name=_('الرقم'))
    issue_date = models.DateField(null=True, blank=True, verbose_name=_('تاريخ الإصدار'))
    expiry_date = models.DateField(null=True, blank=True, db_index=True, verbose_name=_('تاريخ الانتهاء'))
    file = models.FileField(upload_to='employee_docs/', null=True, blank=True, verbose_name=_('الملف'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('مستند موظف')
        verbose_name_plural = _('مستندات الموظفين')
        ordering = ['expiry_date', 'id']

    def __str__(self):
        return f'{self.get_doc_type_display()} — {display_username(self.employee.user)}'


class EmployeeAsset(models.Model):
    """عهدة عينية مع الموظف (لابتوب، يونيفورم، مفاتيح...)."""
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='assets', verbose_name=_('الموظف'))
    item = models.CharField(max_length=150, verbose_name=_('الصنف'))
    serial = models.CharField(max_length=80, blank=True, default="", verbose_name=_('السيريال / الكود'))
    value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), verbose_name=_('القيمة'))
    issued_on = models.DateField(default=timezone.localdate, verbose_name=_('تاريخ التسليم'))
    returned_on = models.DateField(null=True, blank=True, verbose_name=_('تاريخ الرجوع'))
    notes = models.CharField(max_length=255, blank=True, default="", verbose_name=_('ملاحظات'))

    class Meta:
        verbose_name = _('عهدة عينية')
        verbose_name_plural = _('العهد العينية')
        ordering = ['returned_on', '-issued_on']

    def __str__(self):
        return f'{self.item} — {display_username(self.employee.user)}'


class LoanRequest(models.Model):
    """طلب سلفة من الموظف (الخدمة الذاتية) — الاعتماد بيصرفها من شاشة السلف."""
    PENDING, APPROVED, REJECTED = 'PENDING', 'APPROVED', 'REJECTED'
    STATUS_CHOICES = ((PENDING, _('مستني الاعتماد')), (APPROVED, _('اتصرفت')), (REJECTED, _('مرفوضة')))
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='loan_requests', verbose_name=_('الموظف'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('المبلغ'))
    installments_count = models.PositiveIntegerField(default=1, verbose_name=_('عدد الأقساط'))
    reason = models.CharField(max_length=255, blank=True, default="", verbose_name=_('السبب'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True, verbose_name=_('الحالة'))
    loan = models.OneToOneField(EmployeeLoan, on_delete=models.SET_NULL, null=True, blank=True, related_name='request',
                                verbose_name=_('السلفة'))
    decided_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name=_('القرار بواسطة'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('طلب سلفة')
        verbose_name_plural = _('طلبات السلف')
        ordering = ['-created_at']


class EndOfService(models.Model):
    """مخالصة نهاية خدمة: مكافأة + بدل رصيد إجازات - سلف متبقية = صافي يتصرف، ويتقفل حساب الموظف.
    قيد: مدين 5210 (المكافأة + بدل الإجازات) / دائن 1135 (السلف المتبقية) / دائن الخزينة أو البنك (الصافي)."""
    employee = models.OneToOneField(Employee, on_delete=models.PROTECT, related_name='end_of_service', verbose_name=_('الموظف'))
    termination_date = models.DateField(verbose_name=_('تاريخ انتهاء الخدمة'))
    reason = models.CharField(max_length=150, verbose_name=_('السبب'))
    years_of_service = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0'), verbose_name=_('سنين الخدمة'))
    severance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), verbose_name=_('مكافأة نهاية الخدمة'))
    leave_days = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal('0'), verbose_name=_('أيام إجازة متبقية'))
    leave_encashment = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), verbose_name=_('بدل الإجازات'))
    loans_deducted = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), verbose_name=_('سلف مخصومة'))
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), verbose_name=_('الصافي'))
    assets_returned = models.BooleanField(default=False, verbose_name=_('العهد العينية رجعت'))
    treasury = models.ForeignKey('accounting.Treasury', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                 verbose_name=_('الخزينة'))
    bank_account = models.ForeignKey('accounting.BankAccount', on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='+', verbose_name=_('الحساب البنكي'))
    journal_entry = models.ForeignKey('accounting.JournalEntry', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+', verbose_name=_('القيد'))
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                   verbose_name=_('بواسطة'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('مخالصة نهاية خدمة')
        verbose_name_plural = _('مخالصات نهاية الخدمة')
        ordering = ['-termination_date']
