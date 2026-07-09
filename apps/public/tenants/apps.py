from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.public.tenants'
    label = 'tenants'
    verbose_name = 'المستأجرون'
