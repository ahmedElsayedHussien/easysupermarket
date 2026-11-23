from collections import Counter

from django.test import TestCase

from apps.tenant.accounting.models import Account
from apps.tenant.core.management.commands.setup_chart_of_accounts import setup_chart_of_accounts_data


class ChartOfAccountsSeedTests(TestCase):
    def test_seed_is_idempotent(self):
        setup_chart_of_accounts_data()
        first = Account.objects.count()
        setup_chart_of_accounts_data()
        self.assertEqual(Account.objects.count(), first)
        dupes = [c for c, n in Counter(Account.objects.values_list('code', flat=True)).items() if n > 1]
        self.assertEqual(dupes, [])

    def test_required_accounts_exist(self):
        setup_chart_of_accounts_data()
        for code in ('1110', '1130', '1140', '1155', '1190', '1215', '2110', '2130',
                     '2165', '3100', '4100', '4120', '4140', '5100', '5305'):
            self.assertTrue(Account.objects.filter(code=code).exists(), f'account {code} missing')

    def test_contra_flags(self):
        setup_chart_of_accounts_data()
        self.assertTrue(Account.objects.get(code='1220').is_contra)
        self.assertTrue(Account.objects.get(code='5110').is_contra)
        self.assertTrue(Account.objects.get(code='4120').is_contra_revenue)
        self.assertFalse(Account.objects.get(code='4100').is_contra)

    def test_vat_accounts_types(self):
        setup_chart_of_accounts_data()
        self.assertEqual(Account.objects.get(code='1190').account_type, Account.ASSET)   # input VAT
        self.assertEqual(Account.objects.get(code='2130').account_type, Account.LIABILITY)  # output VAT
