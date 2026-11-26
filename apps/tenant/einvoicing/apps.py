from django.apps import AppConfig


class EinvoicingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.einvoicing'
    verbose_name = 'منظومة الفاتورة الإلكترونية'

    def ready(self):
        pass  # the e-invoice queue is filled explicitly by invoice_service (no signals)
