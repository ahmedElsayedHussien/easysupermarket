from django.db import migrations

from apps.tenant.core.db_triggers import (
    drop_auditlog_immutability_trigger,
    install_auditlog_immutability_trigger,
)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_sidebar_nav_toggles'),
    ]

    operations = [
        migrations.RunPython(
            install_auditlog_immutability_trigger,
            reverse_code=drop_auditlog_immutability_trigger,
        ),
    ]
