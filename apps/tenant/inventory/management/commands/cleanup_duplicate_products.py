import sys
import shutil
import datetime
from django.core.management.base import BaseCommand
from django.db import transaction, connection
from django.db.models import Count
from django.conf import settings


def normalize_text(val):
    if not val:
        return ''
    s = ' '.join(str(val).strip().split())
    return s.replace('\u200b', '').replace('\xa0', ' ').strip()


class Command(BaseCommand):
    help = 'Clean up duplicate products safely by merging related records (batches, invoices, movements) into the primary product.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply changes to database. Without this flag, a dry-run preview is performed.',
        )
        parser.add_argument(
            '--schema',
            type=str,
            default=None,
            help='Tenant schema name to run against (if using PostgreSQL multi-tenant). Defaults to active or all schemas.',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        schema_arg = options['schema']

        is_multi_tenant = 'django_tenants' in settings.INSTALLED_APPS and 'django_tenants.postgresql_backend' in settings.DATABASES['default']['ENGINE']

        if is_multi_tenant:
            from apps.public.tenants.models import Tenant
            from django_tenants.utils import schema_context

            tenants_to_process = []
            if schema_arg:
                tenants_to_process = list(Tenant.objects.filter(schema_name=schema_arg))
            else:
                tenants_to_process = list(Tenant.objects.exclude(schema_name='public'))

            for t in tenants_to_process:
                self.stdout.write(self.style.NOTICE(f"\nProcessing Tenant: {t.name} (Schema: {t.schema_name})"))
                with schema_context(t.schema_name):
                    self._process_active_schema(apply_changes)
        else:
            self._process_active_schema(apply_changes)

    def _process_active_schema(self, apply_changes):
        from apps.tenant.inventory.models import (
            Product, InventoryBatch, StockMovement,
            StockAdjustmentLine, ProductUoM, ProductBranch, SerialItem
        )
        from apps.tenant.invoicing.models import InvoiceLine, QuotationLine
        from apps.tenant.maintenance.models import TicketPart
        from apps.tenant.pricing.models import PriceListItem

        total_prods = Product.objects.count()
        self.stdout.write(f"Total products in schema: {total_prods}")

        # Group by name
        dup_names_qs = (
            Product.objects.values('name')
            .annotate(cnt=Count('id'))
            .filter(cnt__gt=1)
        )
        dup_name_count = dup_names_qs.count()

        # Group by non-empty barcode
        dup_bc_qs = (
            Product.objects.exclude(barcode__isnull=True)
            .exclude(barcode='')
            .values('barcode')
            .annotate(cnt=Count('id'))
            .filter(cnt__gt=1)
        )
        dup_bc_count = dup_bc_qs.count()

        self.stdout.write(f"Duplicated product names: {dup_name_count}")
        self.stdout.write(f"Duplicated barcodes: {dup_bc_count}")

        if dup_name_count == 0 and dup_bc_count == 0:
            self.stdout.write(self.style.SUCCESS("No duplicate products found in this schema!"))
            return

        if not apply_changes:
            self.stdout.write(self.style.WARNING("\n[DRY RUN PREVIEW] No changes have been saved to the database."))
            self.stdout.write("Run with --apply to perform the safe merge and cleanup.")
            return

        self.stdout.write(self.style.NOTICE("\nApplying merge in an atomic transaction..."))

        merged_count = 0
        relinked_invoices = 0
        relinked_movements = 0
        relinked_batches = 0
        relinked_adj_lines = 0

        # We process name duplicates first
        with transaction.atomic():
            for item in dup_names_qs:
                name_val = item['name']
                prods = list(Product.objects.filter(name=name_val).order_by('id'))
                if len(prods) < 2:
                    continue

                primary = prods[0]
                duplicates = prods[1:]

                # Pick up best barcode/category/unit if primary is missing them
                best_bc = primary.barcode
                best_cat = primary.category
                best_unit = primary.unit

                for d in duplicates:
                    if not best_bc and d.barcode:
                        best_bc = d.barcode
                    if not best_cat and d.category:
                        best_cat = d.category
                    if not best_unit and d.unit:
                        best_unit = d.unit

                fields_to_update = []
                if best_bc != primary.barcode:
                    primary.barcode = best_bc
                    fields_to_update.append('barcode')
                if best_cat != primary.category:
                    primary.category = best_cat
                    fields_to_update.append('category')
                if best_unit != primary.unit:
                    primary.unit = best_unit
                    fields_to_update.append('unit')

                if fields_to_update:
                    primary.save(update_fields=fields_to_update)

                # Re-link relations for each duplicate
                for d in duplicates:
                    c1 = InvoiceLine.objects.filter(product=d).update(product=primary)
                    relinked_invoices += c1

                    c2 = InventoryBatch.objects.filter(product=d).update(product=primary)
                    relinked_batches += c2

                    c3 = StockMovement.objects.filter(product=d).update(product=primary)
                    relinked_movements += c3

                    c4 = StockAdjustmentLine.objects.filter(product=d).update(product=primary)
                    relinked_adj_lines += c4

                    ProductUoM.objects.filter(product=d).update(product=primary)
                    ProductBranch.objects.filter(product=d).update(product=primary)
                    QuotationLine.objects.filter(product=d).update(product=primary)
                    TicketPart.objects.filter(product=d).update(product=primary)
                    PriceListItem.objects.filter(product=d).update(product=primary)
                    SerialItem.objects.filter(product=d).update(product=primary)

                    d.delete()
                    merged_count += 1

            # Check if any duplicate barcodes remain
            remaining_dup_bc = (
                Product.objects.exclude(barcode__isnull=True)
                .exclude(barcode='')
                .values('barcode')
                .annotate(cnt=Count('id'))
                .filter(cnt__gt=1)
            )
            for item in remaining_dup_bc:
                bc_val = item['barcode']
                prods = list(Product.objects.filter(barcode=bc_val).order_by('id'))
                if len(prods) < 2:
                    continue

                primary = prods[0]
                duplicates = prods[1:]

                for d in duplicates:
                    relinked_invoices += InvoiceLine.objects.filter(product=d).update(product=primary)
                    relinked_batches += InventoryBatch.objects.filter(product=d).update(product=primary)
                    relinked_movements += StockMovement.objects.filter(product=d).update(product=primary)
                    relinked_adj_lines += StockAdjustmentLine.objects.filter(product=d).update(product=primary)

                    ProductUoM.objects.filter(product=d).update(product=primary)
                    ProductBranch.objects.filter(product=d).update(product=primary)
                    QuotationLine.objects.filter(product=d).update(product=primary)
                    TicketPart.objects.filter(product=d).update(product=primary)
                    PriceListItem.objects.filter(product=d).update(product=primary)
                    SerialItem.objects.filter(product=d).update(product=primary)

                    d.delete()
                    merged_count += 1

        new_total = Product.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"\nMerge Complete!\n"
            f"  - Merged & Removed duplicate products: {merged_count}\n"
            f"  - Re-linked invoice lines: {relinked_invoices}\n"
            f"  - Re-linked stock movements: {relinked_movements}\n"
            f"  - Re-linked inventory batches: {relinked_batches}\n"
            f"  - New total products: {new_total}"
        ))
