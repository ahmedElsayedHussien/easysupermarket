"""
Report / restore stock deducted by the OLD quotation-confirm logic.

Before the fix, Quotation.confirm_quotation() consumed FIFO batches (without COGS
or accounting). Converting such a quotation then deducted the same stock a second
time through the SALE invoice, and editing/deleting a POSTED quotation never put
the stock back.

This command finds quotations whose legacy OUT movements
(notes starting with "ترحيل عرض سعر رقم") were never restored and, with --apply,
adds the exact quantities back at the consumed batches' unit cost
(Quotation.restore_legacy_stock(); idempotent).

Usage:
    python manage.py restore_quotation_stock                 # dry-run report (all tenants)
    python manage.py restore_quotation_stock --schema=foo     # one tenant
    python manage.py restore_quotation_stock --apply          # restore stock
"""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = 'Report (default) or restore (--apply) stock deducted by legacy quotation confirmation.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Restore the stock. Without this flag only a report is printed.')
        parser.add_argument('--schema', type=str, default=None,
                            help='Tenant schema to process (PostgreSQL multi-tenant). Default: all tenants.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
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
                self.stdout.write(self.style.NOTICE(f'\nTenant: {t.name} (schema: {t.schema_name})'))
                with schema_context(t.schema_name):
                    self._process(apply_changes)
        else:
            self._process(apply_changes)

    def _process(self, apply_changes):
        from apps.tenant.invoicing.models import Quotation, LEGACY_QUOTATION_OUT_NOTE
        from apps.tenant.inventory.models import StockMovement

        refs = (
            StockMovement.objects.filter(movement_type='OUT', notes__startswith=LEGACY_QUOTATION_OUT_NOTE)
            .values_list('reference', flat=True).distinct()
        )
        quotations = Quotation.objects.filter(quotation_number__in=list(refs)).order_by('id')

        affected = [q for q in quotations if q.has_unrestored_legacy_stock()]
        if not affected:
            self.stdout.write(self.style.SUCCESS('No quotations with unrestored legacy stock deductions.'))
            return

        for q in affected:
            movements = list(q.legacy_stock_movements().select_related('product', 'warehouse'))
            total_value = sum((m.quantity * (m.unit_cost or Decimal('0')) for m in movements), Decimal('0'))
            note = ''
            if q.status == Quotation.CONVERTED and q.converted_invoice_id:
                note = f' -> converted to invoice {q.converted_invoice.invoice_number} (stock was deducted twice)'
            self.stdout.write(
                f'{q.quotation_number} [{q.get_status_display()}]{note}: '
                f'{len(movements)} movement(s), value {total_value:.4f}'
            )
            for m in movements:
                self.stdout.write(
                    f'    - {m.product.name} @ {m.warehouse.name}: qty {m.quantity} x cost {m.unit_cost}'
                )

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                f'\n[DRY RUN] {len(affected)} quotation(s) affected. Run with --apply to restore the stock.'
            ))
            return

        restored_count = 0
        with transaction.atomic():
            for q in affected:
                if q.restore_legacy_stock():
                    restored_count += 1
        self.stdout.write(self.style.SUCCESS(f'\nRestored stock for {restored_count} quotation(s).'))
