from django.db import migrations

GROUPS = {
    'show_nav_inventory_dashboard': ['show_nav_inventory_fifo', 'show_nav_stocktake', 'show_nav_adjustments',
                                     'show_nav_products', 'show_nav_uom'],
    'show_nav_partners_dashboard': ['show_nav_suppliers', 'show_nav_customers', 'show_nav_sectors',
                                    'show_nav_partners_report'],
    'show_nav_liquidity_dashboard': ['show_nav_treasuries', 'show_nav_banks', 'show_nav_ewallets'],
}


def forwards(apps, schema_editor):
    """A dashboard link is shown if ANY of the per-page links it replaces was shown."""
    SystemSetting = apps.get_model('core', 'SystemSetting')
    for s in SystemSetting.objects.all():
        for new, olds in GROUPS.items():
            setattr(s, new, any(getattr(s, o, True) for o in olds))
        s.save(update_fields=list(GROUPS))


class Migration(migrations.Migration):
    dependencies = [('core', '0016_settings_round2')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
