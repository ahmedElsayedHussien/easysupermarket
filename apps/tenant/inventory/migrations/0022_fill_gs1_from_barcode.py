"""Existing products: a manufacturer (GS1) barcode is also their GS1 code for the Tax
Authority. Fills empty GS1 codes only; in-store / scale barcodes (starting with 2) and
barcodes with a wrong check digit are left alone."""
from django.db import migrations


def is_official_gtin(code):
    code = (code or '').strip()
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    if (len(code) in (12, 13) and code[0] == '2') or (len(code) == 14 and code[1] == '2'):
        return False
    if set(code) == {'0'} or '00000000' in code:
        return False    # placeholder / generated numbers (e.g. 100000000007), not a manufacturer GTIN
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def fill(apps, schema_editor):
    return  # policy changed: EGS coding only — GS1 codes are not filled
    from django.db.models import Q
    Product = apps.get_model('inventory', 'Product')
    rows = (Product.objects.filter(Q(gs1_code__isnull=True) | Q(gs1_code=''))
            .exclude(barcode__isnull=True).exclude(barcode='').values_list('pk', 'barcode'))
    for pk, barcode in rows:
        if is_official_gtin(barcode):
            Product.objects.filter(pk=pk).update(gs1_code=barcode.strip())


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0021_category_egs_help'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
