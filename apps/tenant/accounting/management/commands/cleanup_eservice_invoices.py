"""
Removes the legacy "commission" sales invoices (SVC-...) that older versions created
for every e-payment operation (EServiceTransaction). The commission is a payment-services
revenue booked by the operation's own journal entry (4240 / legacy 4200); those invoices
had no lines, no journal entry and only polluted sales reports / e-invoicing.

For each EServiceTransaction whose ``invoice`` is an SVC-... SALE invoice WITHOUT lines:
  - dry run (default): list it;
  - --apply: delete its einvoicing EInvoiceLog row(s), delete the invoice, clear
    transaction.invoice. Invoices linked to any journal entry (invoice.journal_entry or
    a JournalEntry pointing at the invoice through content_type/object_id) are SKIPPED
    and reported.

Usage:
    python manage.py cleanup_eservice_invoices                 # dry run, every tenant schema
    python manage.py cleanup_eservice_invoices --apply
    python manage.py cleanup_eservice_invoices --schema=<name> [--apply]
Single-instance SQLite (no django_tenants): runs on the default database.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


def _tenants_enabled():
    return 'django_tenants' in settings.INSTALLED_APPS


class Command(BaseCommand):
    help = 'Deletes the empty SVC-... sales invoices created for e-payment operations (dry run by default).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually delete (default: dry run).')
        parser.add_argument('--schema', default='', help='Only this tenant schema (django_tenants).')

    def handle(self, *args, **options):
        apply = options['apply']
        schema = options['schema']
        current = getattr(connection, 'schema_name', None)

        if not _tenants_enabled():
            self._run(apply, label='default')
            return

        from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context
        public = get_public_schema_name()
        if current and current != public and not schema:
            # Already scoped to a tenant (e.g. via tenant_command)
            self._run(apply, label=current)
            return
        tenants = get_tenant_model().objects.exclude(schema_name=public)
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f'Unknown schema "{schema}".')
        for tenant in tenants:
            with schema_context(tenant.schema_name):
                self._run(apply, label=tenant.schema_name)

    def _run(self, apply, label):
        from django.contrib.contenttypes.models import ContentType
        from apps.tenant.accounting.models import EServiceTransaction, JournalEntry
        from apps.tenant.einvoicing.models import EInvoiceLog
        from apps.tenant.invoicing.models import Invoice

        mode = 'APPLY' if apply else 'DRY RUN'
        self.stdout.write(f'[{label}] {mode}')
        inv_ct = ContentType.objects.get_for_model(Invoice)
        txns = (EServiceTransaction.objects
                .filter(invoice__isnull=False, invoice__invoice_number__startswith='SVC-',
                        invoice__invoice_type=Invoice.SALE)
                .select_related('invoice').order_by('pk'))
        done = skipped = 0
        for txn in txns:
            inv = txn.invoice
            if inv.lines.exists():
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f'  skip {inv.invoice_number} ({txn.transaction_number}): the invoice has lines'))
                continue
            linked = JournalEntry.objects.filter(content_type=inv_ct, object_id=inv.pk).exists()
            if inv.journal_entry_id or linked:
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f'  skip {inv.invoice_number} ({txn.transaction_number}): linked to a journal entry'))
                continue
            logs = EInvoiceLog.objects.filter(invoice=inv)
            log_info = f', e-invoice log: {logs.count()}' if logs.exists() else ''
            if not apply:
                done += 1
                self.stdout.write(f'  would delete {inv.invoice_number} '
                                  f'({inv.total_amount:.2f}) of {txn.transaction_number}{log_info}')
                continue
            try:
                with transaction.atomic():
                    logs.delete()
                    EServiceTransaction.objects.filter(pk=txn.pk).update(invoice=None)
                    # queryset delete: Invoice.delete() refuses POSTED invoices by design
                    Invoice.objects.filter(pk=inv.pk).delete()
                done += 1
                self.stdout.write(self.style.SUCCESS(
                    f'  deleted {inv.invoice_number} of {txn.transaction_number}{log_info}'))
            except Exception as exc:  # e.g. ProtectedError from another document
                skipped += 1
                self.stderr.write(f'  failed {inv.invoice_number} ({txn.transaction_number}): {exc}')
        verb = 'deleted' if apply else 'to delete'
        self.stdout.write(f'[{label}] {verb}: {done}, skipped: {skipped}')
