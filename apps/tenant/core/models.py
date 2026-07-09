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

