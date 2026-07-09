from django.apps import AppConfig


class InvoicingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.invoicing'
    label = 'invoicing'
    verbose_name = 'الفواتير'
