from django_tenants.utils import schema_context
from apps.tenant.invoicing.models import InvoiceLine
from apps.tenant.inventory.models import SerialItem

with schema_context('ahmedelsayedhussien'):
    lines = InvoiceLine.objects.filter(product__product_type='SERIALIZED', serial_item__isnull=True)
    count = 0
    for line in list(lines):
        qty = int(line.quantity)
        serials = SerialItem.objects.filter(product=line.product).order_by('-id')[:qty]
        for i, s in enumerate(serials):
            if i == 0:
                line.serial_item = s
                line.quantity = 1
                line.save()
                count += 1
            else:
                InvoiceLine.objects.create(
                    invoice=line.invoice, 
                    product=line.product, 
                    serial_item=s, 
                    quantity=1, 
                    unit_price=line.unit_price, 
                    tax_rate=line.tax_rate, 
                    wht_rate=line.wht_rate, 
                    discount_pct=line.discount_pct, 
                    uom=line.uom
                )
                count += 1
    print(f'Fixed {count} lines.')
