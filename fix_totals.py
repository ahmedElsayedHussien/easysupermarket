import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django_tenants.utils import schema_context
from apps.tenant.invoicing.models import InvoiceLine, Invoice

with schema_context('ahmedelsayedhussien'):
    lines = InvoiceLine.objects.filter(total_amount=0, subtotal__gt=0)
    print(f"Updating {lines.count()} lines...")
    for line in lines:
        line.save()
        
    invoices = Invoice.objects.filter(total_amount=0)
    print(f"Updating {invoices.count()} invoices...")
    for inv in invoices:
        inv.recalculate_totals()
