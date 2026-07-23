from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context
from apps.tenant.invoicing.models import InvoiceLine
from apps.tenant.inventory.models import SerialItem
from decimal import Decimal


class Command(BaseCommand):
    help = 'Links SerialItems to unlinked SERIALIZED InvoiceLines'

    def add_arguments(self, parser):
        parser.add_argument('schema', type=str, help='Tenant schema name')

    def handle(self, *args, **options):
        schema = options['schema']
        with schema_context(schema):
            lines = list(InvoiceLine.objects.filter(
                product__product_type='SERIALIZED',
                serial_item__isnull=True
            ).select_related('product', 'invoice'))

            self.stdout.write(f'Found {len(lines)} unlinked serialized invoice lines in schema: {schema}')

            fixed = 0
            for line in lines:
                qty = int(line.quantity)
                invoice_date = line.invoice.created_at if hasattr(line.invoice, 'created_at') else None

                # Get serials for this product, not sold, not returned, ordered by creation
                serials = SerialItem.objects.filter(
                    product=line.product,
                    is_sold=False,
                    is_returned=False
                ).order_by('id')[:qty]

                if not serials:
                    # Try any serial for this product
                    serials = SerialItem.objects.filter(product=line.product).order_by('id')[:qty]

                if not serials:
                    self.stdout.write(self.style.WARNING(
                        f'  No serials found for line {line.id} (product: {line.product.name}, invoice: {line.invoice.invoice_number})'
                    ))
                    continue

                for i, s in enumerate(serials):
                    if i == 0:
                        line.serial_item = s
                        line.quantity = Decimal('1')
                        line.save()
                        fixed += 1
                        self.stdout.write(f'  Fixed line {line.id} -> serial {s.id}')
                    else:
                        InvoiceLine.objects.create(
                            invoice=line.invoice,
                            product=line.product,
                            serial_item=s,
                            quantity=Decimal('1'),
                            unit_price=line.unit_price,
                            tax_rate=line.tax_rate,
                            wht_rate=line.wht_rate,
                            discount_pct=line.discount_pct,
                            uom=line.uom,
                            returned_quantity=Decimal('0'),
                        )
                        fixed += 1
                        self.stdout.write(f'  Created new line for serial {s.id}')

            self.stdout.write(self.style.SUCCESS(f'Done. Fixed/created {fixed} lines.'))
