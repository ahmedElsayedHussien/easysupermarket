"""Lines saved before InvoiceLine.save() maintained total_amount (July 2026) hold 0
although subtotal / tax are right: recompute total = subtotal + tax - withholding."""
from django.db import migrations
from django.db.models import F, Q


def backfill(apps, schema_editor):
    InvoiceLine = apps.get_model('invoicing', 'InvoiceLine')
    (InvoiceLine.objects
     .filter(total_amount=0)
     .filter(Q(subtotal__gt=0) | Q(tax_amount__gt=0))
     .update(total_amount=F('subtotal') + F('tax_amount') - F('wht_amount')))


class Migration(migrations.Migration):

    dependencies = [
        ('invoicing', '0019_pos_session_variance_entry'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
