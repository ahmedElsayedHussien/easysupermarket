"""
P3-4: check that money is whole piasters where it must be, then run inventory_ledger_check.

1) Every posted invoice: its total equals the payment side of its journal entry — the lines on a money box
   (treasury / bank / e-wallet) or on the invoice partner's own account:
       sale              Σ (debit − credit) on those lines = total_amount + rounding_amount (POS cash rounding)
       sales return      Σ (credit − debit)                = total_amount + rounding_amount
       purchase          Σ (credit − debit)                = total_amount
       purchase return   Σ (debit − credit)                = total_amount
   and the invoice's own figures have 2 places.
2) No customer / supplier ledger balance has a fraction of a piaster.
3) inventory_ledger_check (batches vs account 1140).

    manage.py check_money_precision [--limit N]          (tenant: manage.py tenant_command check_money_precision ...)
Exits with an error when something does not match.
"""
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from apps.tenant.core.money import money

ZERO = Decimal('0')


def money_box_accounts():
    from apps.tenant.accounting.models import BankAccount, EWallet, Treasury
    ids = set()
    for model in (Treasury, BankAccount, EWallet):
        ids |= set(model.objects.exclude(account=None).values_list('account_id', flat=True))
    return ids


def invoice_problems(limit=None):
    """[(invoice_number, message)] for posted invoices whose total does not match their entry."""
    from apps.tenant.accounting.models import JournalItem
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.partners.balances import _accounts
    boxes = money_box_accounts()
    problems = []
    qs = Invoice.objects.filter(status=Invoice.POSTED).select_related('partner', 'journal_entry').order_by('id')
    if limit:
        qs = qs[:limit]
    sign = {Invoice.SALE: 1, Invoice.RETURN_PURCHASE: 1, Invoice.RETURN_SALE: -1, Invoice.PURCHASE: -1}
    for inv in qs.iterator(chunk_size=500):
        for field in ('subtotal', 'discount_amount', 'tax_amount', 'wht_amount', 'total_amount', 'paid_cash',
                      'paid_card', 'paid_ewallet', 'paid_bank', 'paid_credit', 'rounding_amount'):
            value = getattr(inv, field) or ZERO
            if value != money(value):
                problems.append((inv.invoice_number, f'{field} = {value} (أكتر من خانتين)'))
        if inv.invoice_type not in sign:
            continue
        entry = inv.journal_entry
        if entry is None or entry.status != 'POSTED':
            problems.append((inv.invoice_number, 'فاتورة مرحّلة من غير قيد مرحّل'))
            continue
        accounts = set(boxes)
        if inv.partner_id:
            accounts |= {a for a in _accounts(inv.partner) if a}
        agg = JournalItem.objects.filter(entry=entry, account_id__in=accounts).aggregate(d=Sum('debit'), c=Sum('credit'))
        paid = sign[inv.invoice_type] * ((agg['d'] or ZERO) - (agg['c'] or ZERO))
        expected = (inv.total_amount or ZERO)
        if inv.invoice_type in (Invoice.SALE, Invoice.RETURN_SALE):
            expected += inv.rounding_amount or ZERO
        if paid != expected:
            problems.append((inv.invoice_number,
                             f'القيد {entry.reference}: طرف الدفع {paid:.4f} ≠ إجمالي الفاتورة {expected:.4f}'))
    return problems


def partner_problems():
    """[(partner, message)] for ledger balances with a fraction of a piaster."""
    from apps.tenant.partners.balances import ledger_balance_map
    from apps.tenant.partners.models import Partner
    partners = list(Partner.objects.all())
    out = []
    balances = ledger_balance_map(partners)
    by_id = {p.pk: p for p in partners}
    for pid, (cust, supp) in balances.items():
        for label, value in (('رصيد عميل', cust), ('رصيد مورد', supp)):
            if value != money(value):
                out.append((by_id[pid].name, f'{label} {value} فيه كسور أقل من قرش'))
    return out


class Command(BaseCommand):
    help = 'Posted invoices = their entries to the piaster, no sub-piaster partner balance, then inventory_ledger_check.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None, help='check only the first N posted invoices')
        parser.add_argument('--skip-inventory', action='store_true', help='do not run inventory_ledger_check')

    def handle(self, *args, **opts):
        inv = invoice_problems(opts['limit'])
        part = partner_problems()
        self.stdout.write('=' * 70)
        self.stdout.write('فحص دقة المبالغ (P3-4)')
        self.stdout.write('=' * 70)
        for number, msg in inv:
            self.stdout.write(f'  فاتورة {number}: {msg}')
        for name, msg in part:
            self.stdout.write(f'  {name}: {msg}')
        self.stdout.write(f'الفواتير: {len(inv)} مشكلة — أرصدة العملاء والموردين: {len(part)} مشكلة')
        if not opts['skip_inventory']:
            call_command('inventory_ledger_check', stdout=self.stdout, stderr=self.stderr)
        if inv or part:
            raise CommandError(f'دقة المبالغ: {len(inv) + len(part)} مشكلة')
        self.stdout.write(self.style.SUCCESS('كل الفواتير مطابقة لقيودها بالقرش، ومفيش رصيد فيه كسور.'))
