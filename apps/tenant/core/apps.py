from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.core'
    label = 'core'
    verbose_name = 'الإعدادات الأساسية'
