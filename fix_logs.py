import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from apps.tenant.invoicing.models import Invoice
from apps.tenant.einvoicing.models import EInvoiceLog

invoices = Invoice.objects.filter(invoice_type='SALE', status='POSTED', tax_amount__gt=0)
created_count = 0
for inv in invoices:
    log, created = EInvoiceLog.objects.get_or_create(invoice=inv)
    if created:
        created_count += 1
        print(f"Created log for invoice {inv.invoice_number}")

print(f"Done. Created {created_count} logs.")
