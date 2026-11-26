"""One place that knows every barcode of a product: its main barcode, the barcode of each
unit (ProductUoM) and its extra barcodes (ProductBarcode)."""
from django.core.exceptions import ValidationError


def find_by_barcode(code, active_only=True):
    """(product, product_uom or None) for a scanned code, else (None, None).
    Order: product barcode -> unit barcode -> extra barcode."""
    from .models import Product, ProductBarcode, ProductUoM
    code = (code or '').strip()
    if not code:
        return None, None
    products = Product.objects.filter(is_active=True) if active_only else Product.objects.all()
    product = products.prefetch_related('uoms__uom').filter(barcode=code).first()
    if product:
        return product, None
    puom = ProductUoM.objects.select_related('product', 'uom').filter(barcode=code).first()
    if puom and (puom.product.is_active or not active_only):
        return puom.product, puom
    extra = ProductBarcode.objects.select_related('product', 'uom__uom').filter(barcode=code).first()
    if extra and (extra.product.is_active or not active_only):
        return extra.product, extra.uom
    return None, None


def barcode_owner(code, exclude_product_id=None, exclude_extra_ids=(), exclude_uom_ids=()):
    """Description of where ``code`` is already used (None when free)."""
    from .models import Product, ProductBarcode, ProductUoM
    code = (code or '').strip()
    if not code:
        return None
    p = Product.objects.filter(barcode=code).exclude(id=exclude_product_id).first()
    if p:
        return f'باركود الصنف "{p.name}"'
    u = ProductUoM.objects.filter(barcode=code).exclude(id__in=list(exclude_uom_ids)).select_related('product', 'uom').first()
    if u:
        return f'باركود وحدة "{u.uom.name}" للصنف "{u.product.name}"'
    e = ProductBarcode.objects.filter(barcode=code).exclude(id__in=list(exclude_extra_ids)).select_related('product').first()
    if e:
        return f'باركود إضافي للصنف "{e.product.name}"'
    return None


def parse_extra_rows(post, product=None):
    """[(barcode, product_uom or None, note)] from the product form rows; raises
    ValidationError listing duplicates (inside the form or anywhere else)."""
    codes = post.getlist('extra_barcode[]')
    uoms = post.getlist('extra_barcode_uom[]')
    notes = post.getlist('extra_barcode_note[]')
    own_uoms = {str(u.id): u for u in product.uoms.all()} if product is not None and product.pk else {}
    own_extra_ids = list(product.extra_barcodes.values_list('id', flat=True)) if product is not None and product.pk else []
    rows, seen, errors = [], set(), []
    for i, code in enumerate(codes):
        code = (code or '').strip()
        if not code:
            continue
        if code in seen:
            errors.append(f'الباركود {code} مكرر في القايمة.')
            continue
        seen.add(code)
        if product is not None and product.barcode and code == product.barcode.strip():
            errors.append(f'الباركود {code} هو نفسه باركود الصنف الأساسي.')
            continue
        owner = barcode_owner(code, exclude_product_id=getattr(product, 'pk', None), exclude_extra_ids=own_extra_ids,
                              exclude_uom_ids=list(own_uoms))
        own_uom_codes = {u.barcode for u in own_uoms.values() if u.barcode}
        if owner or code in own_uom_codes:
            errors.append(f'الباركود {code} مستخدم بالفعل ({owner or "باركود وحدة لنفس الصنف"}).')
            continue
        uom = own_uoms.get(uoms[i] if i < len(uoms) else '')
        rows.append((code, uom, (notes[i] if i < len(notes) else '').strip()[:150]))
    if errors:
        raise ValidationError(errors)
    return rows


def save_extra_barcodes(product, rows):
    """Replace the product's extra barcodes with ``rows`` (from parse_extra_rows)."""
    from .models import ProductBarcode
    product.extra_barcodes.exclude(barcode__in=[r[0] for r in rows]).delete()
    existing = {b.barcode: b for b in product.extra_barcodes.all()}
    for code, uom, note in rows:
        b = existing.get(code)
        if b is None:
            ProductBarcode.objects.create(product=product, barcode=code, uom=uom, note=note)
        elif (b.uom_id, b.note) != (getattr(uom, 'id', None), note):
            b.uom, b.note = uom, note
            b.save(update_fields=['uom', 'note'])


def validate_main_barcode(code, product_id=None):
    owner = barcode_owner(code, exclude_product_id=product_id)
    if owner:
        raise ValidationError(f'الباركود مستخدم بالفعل ({owner}).')
