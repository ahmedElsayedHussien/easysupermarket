"""
Merge duplicate products into one primary product (re-links batches, invoice lines,
movements, adjustments, UoMs, branch prices, price lists, serials, maintenance parts).

Safety:
  * Dry-run by default: prints the groups that WOULD be merged. Use --apply to write.
  * Products are only grouped by name when they also share the same base unit and
    product type (two different items with the same name but different units are kept).
  * --by-barcode additionally merges products whose barcodes are equal after trimming
    (case-insensitive) — off by default.
  * Unique constraints are respected: the duplicate's barcode/SKU is cleared before it
    is copied to the primary; conflicting ProductBranch / PriceListItem rows are merged.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction


def normalize_text(val):
    if not val:
        return ''
    s = ' '.join(str(val).replace('​', '').replace('\xa0', ' ').strip().split())
    return s.strip()


class Command(BaseCommand):
    help = 'Safely merge duplicate products (dry-run by default; --apply to write).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Apply changes. Without this flag only a dry-run preview is printed.')
        parser.add_argument('--by-barcode', action='store_true',
                            help='Also merge products whose (trimmed, case-insensitive) barcodes are equal.')
        parser.add_argument('--schema', type=str, default=None,
                            help='Tenant schema to run against (PostgreSQL multi-tenant). Defaults to all tenants.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        by_barcode = options['by_barcode']
        schema_arg = options['schema']

        engine = settings.DATABASES['default']['ENGINE']
        is_multi_tenant = 'django_tenants' in settings.INSTALLED_APPS and 'django_tenants' in engine

        if is_multi_tenant:
            from apps.public.tenants.models import Tenant
            from django_tenants.utils import schema_context
            tenants = Tenant.objects.exclude(schema_name='public')
            if schema_arg:
                tenants = Tenant.objects.filter(schema_name=schema_arg)
            for t in tenants:
                self.stdout.write(self.style.NOTICE(f"\nProcessing Tenant: {t.name} (Schema: {t.schema_name})"))
                with schema_context(t.schema_name):
                    self._process_active_schema(apply_changes, by_barcode)
        else:
            self._process_active_schema(apply_changes, by_barcode)

    # ------------------------------------------------------------------
    def _find_groups(self, by_barcode):
        from apps.tenant.inventory.models import Product

        products = list(Product.objects.all().order_by('id'))
        parent = {p.id: p.id for p in products}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        by_key = {}
        for p in products:
            key = (normalize_text(p.name).lower(), p.unit, p.product_type)
            if key[0]:
                by_key.setdefault(key, []).append(p.id)
        for ids in by_key.values():
            for other in ids[1:]:
                union(ids[0], other)

        if by_barcode:
            by_bc = {}
            for p in products:
                bc = normalize_text(p.barcode).lower()
                if bc:
                    by_bc.setdefault(bc, []).append(p.id)
            for ids in by_bc.values():
                for other in ids[1:]:
                    union(ids[0], other)

        groups = {}
        by_id = {p.id: p for p in products}
        for p in products:
            groups.setdefault(find(p.id), []).append(by_id[p.id])
        return [sorted(g, key=lambda x: x.id) for g in groups.values() if len(g) > 1]

    def _merge(self, primary, dup, stats):
        from apps.tenant.inventory.models import (
            Product, InventoryBatch, StockMovement, StockAdjustmentLine,
            ProductUoM, ProductBranch, SerialItem,
        )
        from apps.tenant.invoicing.models import InvoiceLine, QuotationLine
        from apps.tenant.pricing.models import PriceListItem
        try:
            from apps.tenant.maintenance.models import TicketPart
        except Exception:  # maintenance app optional
            TicketPart = None

        # --- Copy identifying fields without violating unique constraints ---
        fields = []
        for field in ('barcode', 'sku'):
            if not getattr(primary, field) and getattr(dup, field):
                value = getattr(dup, field)
                Product.objects.filter(pk=dup.pk).update(**{field: None})  # free the unique value first
                setattr(dup, field, None)
                setattr(primary, field, value)
                fields.append(field)
        if not primary.category_id and dup.category_id:
            primary.category_id = dup.category_id
            fields.append('category')
        if fields:
            primary.save(update_fields=fields + ['updated_at'])

        stats['invoice_lines'] += InvoiceLine.objects.filter(product=dup).update(product=primary)
        stats['batches'] += InventoryBatch.objects.filter(product=dup).update(product=primary)
        stats['movements'] += StockMovement.objects.filter(product=dup).update(product=primary)
        stats['adj_lines'] += StockAdjustmentLine.objects.filter(product=dup).update(product=primary)
        QuotationLine.objects.filter(product=dup).update(product=primary)
        SerialItem.objects.filter(product=dup).update(product=primary)
        if TicketPart is not None:
            TicketPart.objects.filter(product=dup).update(product=primary)

        # UoMs: skip units the primary already has (same UnitOfMeasure); re-point lines first
        primary_uoms = {u.uom_id: u for u in ProductUoM.objects.filter(product=primary)}
        for du in ProductUoM.objects.filter(product=dup):
            existing = primary_uoms.get(du.uom_id)
            if existing:
                InvoiceLine.objects.filter(uom=du).update(uom=existing)
                QuotationLine.objects.filter(uom=du).update(uom=existing)
                StockAdjustmentLine.objects.filter(uom=du).update(uom=existing)
                if du.barcode and not existing.barcode:
                    bc = du.barcode
                    du.barcode = None
                    du.save(update_fields=['barcode'])
                    existing.barcode = bc
                    existing.save(update_fields=['barcode'])
                du.delete()
            else:
                if du.is_base and any(u.is_base for u in primary_uoms.values()):
                    du.is_base = False
                du.product = primary
                du.save(update_fields=['product', 'is_base'])
                primary_uoms[du.uom_id] = du

        # Branch prices: unique_together (product, branch) -> keep primary's row
        primary_branches = set(ProductBranch.objects.filter(product=primary).values_list('branch_id', flat=True))
        for pb in ProductBranch.objects.filter(product=dup):
            if pb.branch_id in primary_branches:
                pb.delete()
            else:
                pb.product = primary
                pb.save(update_fields=['product'])
                primary_branches.add(pb.branch_id)

        # Price list items: unique_together (price_list, product, uom, min_quantity)
        existing_keys = set(
            PriceListItem.objects.filter(product=primary).values_list('price_list_id', 'uom_id', 'min_quantity')
        )
        for item in PriceListItem.objects.filter(product=dup):
            key = (item.price_list_id, item.uom_id, item.min_quantity)
            if key in existing_keys:
                item.delete()
            else:
                item.product = primary
                item.save(update_fields=['product'])
                existing_keys.add(key)

        dup.delete()
        stats['merged'] += 1

    def _process_active_schema(self, apply_changes, by_barcode):
        from apps.tenant.inventory.models import Product

        self.stdout.write(f"Total products in schema: {Product.objects.count()}")
        groups = self._find_groups(by_barcode)
        if not groups:
            self.stdout.write(self.style.SUCCESS("No duplicate products found in this schema!"))
            return

        self.stdout.write(f"Duplicate groups: {len(groups)}")
        for g in groups:
            primary, dups = g[0], g[1:]
            dup_desc = ', '.join('#%s (barcode=%s)' % (d.id, d.barcode or '-') for d in dups)
            self.stdout.write(
                f"  keep #{primary.id} '{primary.name}' [{primary.unit}/{primary.product_type}] <- merge {dup_desc}"
            )

        if not apply_changes:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No changes saved. Review the list above, then run with --apply."))
            return

        stats = {'merged': 0, 'invoice_lines': 0, 'batches': 0, 'movements': 0, 'adj_lines': 0}
        with transaction.atomic():
            for g in groups:
                primary = Product.objects.select_for_update().get(pk=g[0].pk)
                for d in g[1:]:
                    dup = Product.objects.select_for_update().get(pk=d.pk)
                    self._merge(primary, dup, stats)

        self.stdout.write(self.style.SUCCESS(
            f"\nMerge Complete!\n"
            f"  - Merged & removed duplicate products: {stats['merged']}\n"
            f"  - Re-linked invoice lines: {stats['invoice_lines']}\n"
            f"  - Re-linked stock movements: {stats['movements']}\n"
            f"  - Re-linked inventory batches: {stats['batches']}\n"
            f"  - Re-linked adjustment lines: {stats['adj_lines']}\n"
            f"  - New total products: {Product.objects.count()}"
        ))
