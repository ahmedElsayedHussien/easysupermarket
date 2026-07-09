"""
Subscription Plans model - lives in the public schema.
Defines tiers available for tenants (Starter, Professional, Enterprise).
"""
from django.db import models


class SubscriptionPlan(models.Model):
    """
    Defines tiered subscription packages for supermarket tenants.
    Lives in the public (shared) schema.
    """
    PLAN_TYPES = [
        ('STARTER', 'Starter'),
        ('PROFESSIONAL', 'Professional'),
        ('ENTERPRISE', 'Enterprise'),
    ]

    name = models.CharField(max_length=100, verbose_name='اسم الخطة')
    plan_type = models.CharField(
        max_length=20, choices=PLAN_TYPES, default='STARTER',
        verbose_name='نوع الخطة'
    )
    price = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        verbose_name='السعر الشهري'
    )
    max_branches = models.IntegerField(default=1, verbose_name='أقصى عدد فروع')
    max_users = models.IntegerField(default=5, verbose_name='أقصى عدد مستخدمين')
    max_warehouses_per_branch = models.IntegerField(
        default=2, verbose_name='أقصى مستودعات لكل فرع'
    )
    features = models.JSONField(default=dict, blank=True, verbose_name='مميزات الخطة')
    is_active = models.BooleanField(default=True, verbose_name='نشطة')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'خطة اشتراك'
        verbose_name_plural = 'خطط الاشتراك'
        ordering = ['price']

    def __str__(self):
        return f'{self.name} - {self.price} ج.م/شهر'
