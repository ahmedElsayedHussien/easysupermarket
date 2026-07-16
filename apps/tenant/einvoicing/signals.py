from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.tenant.invoicing.models import Invoice
from .models import EInvoiceLog

@receiver(post_save, sender=Invoice)
def create_einvoice_log(sender, instance, created, **kwargs):
    """
    Automatically creates an EInvoiceLog entry when an Invoice is marked as POSTED
    and it is a SALE invoice.
    """
    if instance.invoice_type == 'SALE' and instance.status == Invoice.POSTED:
        # User requested to only send invoices that have taxes applied
        if getattr(instance, 'tax_amount', 0) > 0:
            from .models import TaxIntegrationSettings
            settings_obj = TaxIntegrationSettings.objects.first()
            if settings_obj and settings_obj.enable_einvoicing:
                EInvoiceLog.objects.get_or_create(invoice=instance)
