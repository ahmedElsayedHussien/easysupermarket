# Generated for auto-healing and linking treasury, bank, and e-wallet accounts
from django.db import migrations


def link_treasuries_and_accounts(apps, schema_editor):
    try:
        from apps.tenant.core.management.commands.setup_chart_of_accounts import setup_chart_of_accounts_data
        from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
        from apps.tenant.core.models import Branch, SystemSetting

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
