"""
Gives every treasury / bank account / e-wallet saved without its ledger sub-account one
(ensure_account: <parent>-<id> under 1110 / 1120 / 1160).

Runs after every migrate (post_migrate, like the chart of accounts) and from setup_database.bat.
Never stops a migration: when the parent account itself is missing it prints a warning and skips.

    python manage.py fill_money_accounts [--schema <schema>]
"""
from django.conf import settings
from django.core.management.base import BaseCommand


def _parent_code(obj):
    from apps.tenant.accounting.models import BankAccount, EWallet, Treasury
    if isinstance(obj, Treasury):
        return '1120' if obj.treasury_type == Treasury.BANK else '1110'
    if isinstance(obj, BankAccount):
        return '1120'
    if isinstance(obj, EWallet):
        return '1160'
    return None


def fill_money_accounts():
    """(filled count, [warnings]) for the current schema."""
    from apps.tenant.accounting.models import Account, BankAccount, EWallet, Treasury
    filled, warnings = 0, []
    if not Account.objects.exists():
        return filled, warnings                       # empty schema (no chart yet): nothing to link to
    for model in (Treasury, BankAccount, EWallet):
        for obj in model.objects.filter(account__isnull=True):
            code = _parent_code(obj)
            if not Account.objects.filter(code=code, is_active=True).exists():
                warnings.append(f'{model._meta.verbose_name} "{obj}" مالهاش حساب، والحساب الأب {code} مش موجود '
                                f'في الشجرة — شغّل setup_chart_of_accounts.')
                continue
            obj.ensure_account()
            filled += 1
    return filled, warnings


class Command(BaseCommand):
    help = "Create the missing ledger sub-account of treasuries / banks / e-wallets."

    def add_arguments(self, parser):
        parser.add_argument('--schema', type=str, help='only this tenant schema')

    def _run(self, label):
        filled, warnings = fill_money_accounts()
        for w in warnings:
            self.stdout.write(self.style.WARNING(f'[{label}] {w}'))
        self.stdout.write(self.style.SUCCESS(f'[{label}] {filled} account(s) created.'))

    def handle(self, *args, **options):
        if 'django_tenants' in getattr(settings, 'INSTALLED_APPS', []):
            from django_tenants.utils import schema_context
            from apps.public.tenants.models import Tenant
            tenants = Tenant.objects.exclude(schema_name='public')
            if options.get('schema'):
                tenants = tenants.filter(schema_name=options['schema'])
            if tenants.exists():
                for tenant in tenants:
                    with schema_context(tenant.schema_name):
                        self._run(tenant.schema_name)
                return
        self._run('current schema')
