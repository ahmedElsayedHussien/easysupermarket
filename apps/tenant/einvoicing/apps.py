from django.apps import AppConfig


class EinvoicingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.einvoicing'
    verbose_name = 'منظومة الفاتورة الإلكترونية'

    def ready(self):
        import apps.tenant.einvoicing.signals
