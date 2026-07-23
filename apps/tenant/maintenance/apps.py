from django.apps import AppConfig


class MaintenanceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.maintenance'
    label = 'maintenance'
    verbose_name = 'الصيانة'
