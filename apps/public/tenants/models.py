"""
Tenant and Domain models for django-tenants multi-tenancy.
Each Tenant gets its own PostgreSQL schema.
"""
from django_tenants.models import TenantMixin, DomainMixin
from django.db import models


class Tenant(TenantMixin):
    """
    Represents a supermarket company (the top-level tenant).
    Each tenant has its own isolated PostgreSQL schema.
    """
    name = models.CharField(max_length=200, verbose_name='اسم السوبر ماركت')
    owner_name = models.CharField(max_length=200, verbose_name='اسم المالك')
    owner_email = models.EmailField(verbose_name='البريد الإلكتروني')
    owner_phone = models.CharField(max_length=20, blank=True, verbose_name='رقم الهاتف')
    plan = models.ForeignKey(
        'subscriptions.SubscriptionPlan',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name='خطة الاشتراك'
    )
    paid_until = models.DateField(null=True, blank=True, verbose_name='مدفوع حتى')
    on_trial = models.BooleanField(default=True, verbose_name='على فترة تجريبية')
    created_on = models.DateField(auto_now_add=True)

    # django-tenants: auto-create the schema when a Tenant is saved
    AUTO_CREATE_SCHEMA = True

    class Meta:
        verbose_name = 'مستأجر'
        verbose_name_plural = 'المستأجرون'

    def __str__(self):
        return self.name

    @property
    def is_subscription_active(self):
        """Check if subscription is either on trial or paid and not expired."""
        import datetime
        if self.on_trial:
            return True
        if self.paid_until and self.paid_until >= datetime.date.today():
            return True
        return False


class Domain(DomainMixin):
    """
    Maps domain names (e.g., carrefour.easysupermarket.com) to a Tenant.
    """
    class Meta:
        verbose_name = 'نطاق'
        verbose_name_plural = 'النطاقات'

    def __str__(self):
        return self.domain
