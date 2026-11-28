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
    # then every cash box / bank / wallet gets its sub-account (warns, never stops the migrate)
    try:
        import logging
        from apps.tenant.accounting.management.commands.fill_money_accounts import fill_money_accounts
        _, warnings = fill_money_accounts()
        for w in warnings:
            logging.getLogger(__name__).warning(w)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('fill_money_accounts skipped after migrate', exc_info=True)


class AccountingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.accounting'
    label = 'accounting'
    verbose_name = 'المحاسبة'

    def ready(self):
        post_migrate.connect(auto_populate_chart_of_accounts, sender=self)
