from django.db import migrations

OLD = ['show_nav_expenses', 'show_nav_receipts', 'show_nav_payments', 'show_nav_transfers', 'show_nav_cheques',
       'show_nav_fixed_assets', 'show_nav_loans', 'show_nav_journals', 'show_nav_chart_of_accounts']


def forwards(apps, schema_editor):
    """The accounting dashboard link is shown if ANY of the 9 links it replaces was shown."""
    SystemSetting = apps.get_model('core', 'SystemSetting')
    for s in SystemSetting.objects.all():
        s.show_nav_accounting_dashboard = any(getattr(s, o, True) for o in OLD)
        s.save(update_fields=['show_nav_accounting_dashboard'])


class Migration(migrations.Migration):
    dependencies = [('core', '0019_accounting_dashboard_toggle')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
