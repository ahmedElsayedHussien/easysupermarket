# Generated for auto-healing and linking treasury, bank, and e-wallet accounts
from django.db import migrations, transaction


def link_treasuries_and_accounts(apps, schema_editor):
    # NOTE: this uses the *live* models, whose columns can be ahead of the schema at this point
    # of the migration graph (e.g. Account.is_contra added in 0026) on a fresh database. The work
    # is best-effort (it is redone lazily at runtime by Treasury/BankAccount/EWallet.ensure_account
    # and _get_system_account), so it runs inside a savepoint: a failure must not abort the
    # migration transaction on PostgreSQL.
    try:
        with transaction.atomic():
            from apps.tenant.core.management.commands.setup_chart_of_accounts import setup_chart_of_accounts_data
            from apps.tenant.accounting.models import Treasury, BankAccount, EWallet

            setup_chart_of_accounts_data()

            for t in Treasury.objects.all():
                t.ensure_account()

            for b in BankAccount.objects.all():
                b.ensure_account()

            for w in EWallet.objects.all():
                w.ensure_account()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Migration 0019 auto-link warning: {e}")


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0018_voucher_paid_bank_voucher_paid_card_and_more'),
    ]

    operations = [
        migrations.RunPython(link_treasuries_and_accounts, migrations.RunPython.noop),
    ]
