from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from apps.tenant.core.models import Employee

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
    delay_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_('ساعات التأخير'))
    overtime_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_('الساعات الإضافية'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='present', verbose_name=_('الحالة'))
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('سجل حضور')
        verbose_name_plural = _('سجلات الحضور والانصراف')
        unique_together = ('employee', 'date')

    def __str__(self):
        return f"حضور {self.employee.user.username} - {self.date}"

class Payroll(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='payrolls', verbose_name=_('الموظف'))
    month = models.IntegerField(verbose_name=_('الشهر'))
    year = models.IntegerField(verbose_name=_('السنة'))
    total_worked_hours = models.DecimalField(max_digits=6, decimal_places=2, default=0.00, verbose_name=_('إجمالي ساعات العمل'))
    total_delay_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_('إجمالي ساعات التأخير'))
    total_overtime_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_('إجمالي الساعات الإضافية'))
    base_pay = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_('المستحق الأساسي'))
    overtime_pay = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_('مكافأة الإضافي'))
    deductions = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_('إجمالي الخصومات'))
    net_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_('الراتب الصافي'))
    is_paid = models.BooleanField(default=False, verbose_name=_('تم الصرف'))
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_('تاريخ الصرف'))
    
    # Linked Journal Entry
    journal_entry = models.OneToOneField('accounting.JournalEntry', null=True, blank=True, on_delete=models.SET_NULL, related_name='payroll', verbose_name=_('قيد الاستحقاق'))

    class Meta:
        verbose_name = _('مسير راتب')
        verbose_name_plural = _('مسيرات الرواتب')
        unique_together = ('employee', 'month', 'year')

    def __str__(self):
        return f"راتب {self.employee.user.username} - {self.month}/{self.year}"
