from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.tenant.invoicing.models import Invoice

from .models import EInvoiceLog

# Logs that never reached the Tax Authority: a cancelled invoice simply leaves the queue.
NOT_SENT = ('WAITING_APPROVAL', 'PENDING', 'SIGNED', 'ERROR', 'INVALID')
# Logs the Tax Authority holds: the cancellation must be requested there too.
SENT = ('SUBMITTED', 'VALID')


@receiver(post_save, sender=Invoice)
def create_einvoice_log(sender, instance, created, **kwargs):
    """
    Queue documents for the Tax Authority and keep the queue in step with the invoice:
      - POSTED sales invoice with tax            -> invoice (I)
      - POSTED sales return of a queued invoice   -> credit note (C) referencing it
      - CANCELLED invoice                         -> dropped if not sent yet,
                                                     "cancel at the ETA" if already sent
    """
    from .models import TaxIntegrationSettings

    if instance.status == Invoice.CANCELLED:
        log = EInvoiceLog.objects.filter(invoice=instance).first()
        if log is None:
            return
        if log.status in NOT_SENT:
            log.delete()
        elif log.status in SENT:
            log.status = 'CANCEL_PENDING'
            log.save(update_fields=['status', 'updated_at'])
        return

    if instance.status != Invoice.POSTED:
        return
    settings_obj = TaxIntegrationSettings.objects.first()
    if not (settings_obj and settings_obj.enable_einvoicing):
        return

    if instance.invoice_type == 'SALE':
        # Only invoices with taxes applied are sent (user rule).
        if getattr(instance, 'tax_amount', 0) > 0:
            EInvoiceLog.objects.get_or_create(invoice=instance)
    elif instance.invoice_type == 'RETURN_SALE' and instance.parent_invoice_id:
        # A return of an invoice that went (or is going) to the ETA becomes a credit note.
        if EInvoiceLog.objects.filter(invoice_id=instance.parent_invoice_id).exists():
            EInvoiceLog.objects.get_or_create(invoice=instance)
