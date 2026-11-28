"""Cash boxes / banks / wallets saved without their ledger sub-account (ensure_account used to swallow
errors, and the GET pages no longer create it): create the missing ones now.

Uses the real models on purpose: the sub-account is an MPTT node and ensure_account() holds the rule
(parent 1110 / 1120 / 1160, code <parent>-<id>). A box whose chart account is missing is reported, not hidden.
"""
from django.db import migrations


def fill(apps, schema_editor):
    from apps.tenant.accounting.models import Account, BankAccount, EWallet, Treasury
    if not Account.objects.exists():
        return                                   # empty schema (no chart yet): nothing to link to
    for model in (Treasury, BankAccount, EWallet):
        for obj in model.objects.filter(account__isnull=True):
            obj.ensure_account()


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0043_pg_immutable_posted_journalitem_trigger'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
