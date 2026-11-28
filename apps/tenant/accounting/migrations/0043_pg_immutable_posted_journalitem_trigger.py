from django.db import migrations

from apps.tenant.accounting.db_triggers import (
    drop_posted_journalitem_trigger,
    install_posted_journalitem_trigger,
)


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0042_idempotency_key'),
    ]

    operations = [
        migrations.RunPython(
            install_posted_journalitem_trigger,
            reverse_code=drop_posted_journalitem_trigger,
        ),
    ]
