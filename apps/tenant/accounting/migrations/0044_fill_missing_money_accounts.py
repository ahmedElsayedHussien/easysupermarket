"""Kept as a no-op on purpose.

It used to import the CURRENT models to fill missing ledger sub-accounts — that breaks any database migrating
through it once a field is added later. The work is now done by the `fill_money_accounts` management command,
run after every migrate (post_migrate) and from setup_database.bat.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0043_pg_immutable_posted_journalitem_trigger'),
    ]

    operations = []
