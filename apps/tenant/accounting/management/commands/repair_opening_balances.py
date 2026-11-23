"""
Repairs opening-balance journal entries that were never booked.

Before the fix, the post_save signals of Treasury / BankAccount / EWallet imported a
non-existent model (JournalEntryLine): the entry header (DRAFT, no lines) was created and
the lines failed, so the opening balance never reached the ledger (EWallet creation crashed).

This command books every missing / empty opening entry via accounting.models.book_opening_balance
(atomic + idempotent: already-correct entries are left untouched).

Multi-tenant (django-tenants) usage:
    python manage.py tenant_command repair_opening_balances --schema=<schema> [--dry-run]
    python manage.py all_tenants_command repair_opening_balances [--dry-run]
"""
from django.core.management.base import BaseCommand

from apps.tenant.accounting.models import (
    BankAccount, EWallet, JournalEntry, POSMachine, Treasury,
    OPENING_REFERENCE_PREFIXES, book_opening_balance,
)


class Command(BaseCommand):
    help = 'Books missing opening-balance entries for treasuries, bank accounts, e-wallets and POS machines.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only list what would be booked.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        fixed = skipped = failed = 0
        for model in (Treasury, BankAccount, EWallet, POSMachine):
            prefix = OPENING_REFERENCE_PREFIXES[model.__name__]
            for obj in model.objects.filter(opening_balance__gt=0):
                ref = f'{prefix}-{obj.pk}'
                entry = JournalEntry.objects.filter(reference=ref).first()
                if entry and entry.status == JournalEntry.POSTED and entry.items.exists():
                    skipped += 1
                    continue
                if obj.account_id is None and hasattr(obj, 'ensure_account'):
                    if dry_run:
                        self.stdout.write(f'[dry-run] {model.__name__} #{obj.pk} "{obj}" has no ledger account (would be created)')
                    else:
                        obj.ensure_account()
                        obj.refresh_from_db()
                if dry_run:
                    self.stdout.write(f'[dry-run] would book {ref}: {obj.opening_balance} for {model.__name__} "{obj}"')
                    fixed += 1
                    continue
                try:
                    result = book_opening_balance(obj, prefix)
                    if result is None:
                        self.stderr.write(f'{model.__name__} #{obj.pk}: no ledger account, skipped')
                        failed += 1
                    else:
                        self.stdout.write(self.style.SUCCESS(f'booked {ref}: {obj.opening_balance} for {model.__name__} "{obj}"'))
                        fixed += 1
                except Exception as exc:  # keep going with the other objects
                    failed += 1
                    self.stderr.write(f'{model.__name__} #{obj.pk}: {exc}')
        self.stdout.write(f'done: booked={fixed} already_ok={skipped} failed={failed}' + (' (dry run)' if dry_run else ''))
