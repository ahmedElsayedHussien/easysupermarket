"""
Commission models:
  - CommissionRule: Defines milestone-based commission per Category.
  - CommissionRecord: Tracks calculated commissions per user per period.

Logic (from EasyMbStore):
  - For each Category with a rule, total sales are accumulated per cashier.
  - Commission = floor(sales / milestone) × commission_amount
  - Example: 3500 ج موبايلات, milestone=1000, amount=5 → 3 × 5 = 15 ج
"""
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _

User = settings.AUTH_USER_MODEL


class CommissionRule(models.Model):
    """
    قاعدة عمولة للفئة الرئيسية من المنتجات.
    العمولة = (مبيعات ÷ شريحة_المبيعات) × مبلغ_العمولة
    """
    category = models.OneToOneField(
        'inventory.Category',
        on_delete=models.CASCADE,
        related_name='commission_rule',
        verbose_name=_('الفئة الرئيسية')
    )
    sales_milestone = models.DecimalField(
        max_digits=12, decimal_places=2,
        verbose_name=_('شريحة المبيعات (ج.م)'),
        help_text=_('مثال: 1000 — أي عمولة لكل 1000 جنيه مبيعات في هذه الفئة')
    )
    commission_amount = models.DecimalField(
        max_digits=8, decimal_places=2,
        verbose_name=_('مبلغ العمولة لكل شريحة (ج.م)'),
        help_text=_('مثال: 5 — أي 5 جنيه لكل 1000 جنيه مبيعات')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('نشطة'))

    class Meta:
        verbose_name = _('قاعدة عمولة')
        verbose_name_plural = _('لائحة قواعد العمولات')

    def __str__(self):
        return f"عمولة {self.category.name}: {self.commission_amount} ج لكل {self.sales_milestone} ج مبيعات"

    def calculate(self, total_sales: Decimal) -> Decimal:
        """حساب العمولة المستحقة بناءً على إجمالي المبيعات."""
        if self.sales_milestone <= 0:
            return Decimal('0')
        milestones = int(total_sales // self.sales_milestone)
        return Decimal(str(milestones)) * self.commission_amount


class CommissionRecord(models.Model):
    """
    سجل العمولة المحتسبة لموظف في فترة زمنية معينة.
    يُنشأ/يُحدَّث عند طلب التقرير أو يدوياً.
    """
    PERIOD_DAILY   = 'DAILY'
    PERIOD_MONTHLY = 'MONTHLY'
    PERIOD_YEARLY  = 'YEARLY'

    PERIOD_CHOICES = [
        (PERIOD_DAILY,   _('يومي')),
        (PERIOD_MONTHLY, _('شهري')),
        (PERIOD_YEARLY,  _('سنوي')),
    ]

    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='commission_records',
        verbose_name=_('الموظف / الكاشير')
    )
    category = models.ForeignKey(
        'inventory.Category', on_delete=models.PROTECT,
        related_name='commission_records',
        verbose_name=_('الفئة')
    )
    period      = models.CharField(max_length=10, choices=PERIOD_CHOICES, verbose_name=_('الفترة'))
    period_start = models.DateField(verbose_name=_('بداية الفترة'))
    period_end   = models.DateField(verbose_name=_('نهاية الفترة'))

    total_sales      = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal('0'), verbose_name=_('إجمالي المبيعات'))
    commission_earned = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'), verbose_name=_('العمولة المستحقة'))
    is_paid          = models.BooleanField(default=False, verbose_name=_('تم الصرف'))
    paid_at          = models.DateTimeField(null=True, blank=True, verbose_name=_('تاريخ الصرف'))
    journal_entry    = models.ForeignKey(
        'accounting.JournalEntry', on_delete=models.SET_NULL,
        null=True, blank=True,
        verbose_name=_('قيد صرف العمولة')
    )

    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('سجل عمولة')
        verbose_name_plural = _('سجلات العمولات')
        unique_together = ('user', 'category', 'period', 'period_start')
        ordering = ['-period_start', 'user']

    def __str__(self):
        return f"عمولة {self.user.username} | {self.category.name} | {self.period_start} → {self.period_end}: {self.commission_earned} ج"
