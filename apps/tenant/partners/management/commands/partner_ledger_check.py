"""
Compare each partner's balance from its documents (invoices, vouchers, cheques… — the old way) with its
balance in the ledger (its own sub-accounts). Read-only.

    python manage.py tenant_command partner_ledger_check --schema=<schema>
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.tenant.partners.balances import ledger_balance_map
from apps.tenant.partners.models import Partner


class Command(BaseCommand):
    help = 'Partners whose document balance differs from their ledger balance (read-only).'

    def handle(self, *args, **opts):
        partners = list(Partner.objects.all().select_related('receivable_account', 'payable_account', 'account')
                        .defer('eta_governate', 'eta_region_city', 'eta_building_number'))
        ledger = ledger_balance_map(partners)
        diffs = 0
        shared = {}
        for p in partners:
            for acc in (p.receivable_account, p.payable_account):
                if acc is not None:
                    shared.setdefault(acc.pk, []).append(p)
        for acc_id, owners in shared.items():
            if len(owners) > 1:
                self.stdout.write(self.style.WARNING(
                    f'account #{acc_id} is shared by: ' + ', '.join(f'{o.pk} {o.name}' for o in owners)))
        for p in partners:
            doc_c, doc_s = p.document_balances()
            led_c, led_s = ledger[p.pk]
            if abs(doc_c - led_c) >= Decimal('0.01') or abs(doc_s - led_s) >= Decimal('0.01'):
                diffs += 1
                self.stdout.write(
                    f'{p.pk:>5} {p.name[:30]:<30} {p.partner_type:<8} '
                    f'customer docs {doc_c:>12.2f} ledger {led_c:>12.2f} | '
                    f'supplier docs {doc_s:>12.2f} ledger {led_s:>12.2f} | '
                    f'accounts {getattr(p.receivable_account, "code", "-")} / {getattr(p.payable_account, "code", "-")}')
        self.stdout.write(self.style.SUCCESS(f'{len(partners)} partners checked, {diffs} with a difference.'))
