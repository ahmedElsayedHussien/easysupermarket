from django.apps import AppConfig
from django.db.models.signals import post_migrate


def auto_populate_chart_of_accounts(sender, **kwargs):
    """
    Automatically ensures the standard Chart of Accounts exists
    whenever migrations are applied, so no new installation or machine
    is ever left with an empty chart of accounts.
    """
    try:
        from apps.tenant.core.management.commands.setup_chart_of_accounts import setup_chart_of_accounts_data
        setup_chart_of_accounts_data()
    except Exception:
        pass


class AccountingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.accounting'
    label = 'accounting'
    verbose_name = 'المحاسبة'

    def ready(self):
        post_migrate.connect(auto_populate_chart_of_accounts, sender=self)
