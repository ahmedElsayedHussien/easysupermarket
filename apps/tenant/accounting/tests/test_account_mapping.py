"""ربط الحسابات: العملية تترحّل على الحساب المربوط، والرجوع للافتراضي، والتحقق من النوع والحساب الفرعي.
ومفيش signals في العمليات المحاسبية."""
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting import account_mapping as amap
from apps.tenant.accounting.models import Account, AccountMapping
from apps.tenant.core.tests.factories import account, build_world, login, make_invoice, make_user, stock_in
from apps.tenant.services.journal_service import _get_system_account


class AccountMappingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        amap.clear_cache()

    def tearDown(self):
        amap.clear_cache()

    def test_posting_follows_the_mapping(self):
        output_vat = account('2130')
        custom = Account.objects.create(code='2131', name='ضريبة مخرجات — فرع', account_type=output_vat.account_type,
                                        parent=output_vat.parent)
        amap.set_mapping('2130', custom, self.w.admin)
        self.assertEqual(_get_system_account('2130'), custom)
        stock_in(self.w.product, self.w.warehouse, 10, D('5'))
        make_invoice(self.w, lines=[(self.w.product, 1, D('100'), D('14'))])  # 14 VAT
        self.assertEqual(custom.current_balance, D('14.00'))
        self.assertEqual(output_vat.current_balance, D('0'))
        amap.set_mapping('2130', None)                                        # back to the default
        self.assertEqual(_get_system_account('2130'), output_vat)
        self.assertFalse(AccountMapping.objects.exists())

    def test_only_leaf_accounts_of_the_same_type(self):
        with self.assertRaisesMessage(ValidationError, 'نوعه مختلف'):
            amap.set_mapping('2130', account('5306') if Account.objects.filter(code='5306').exists()
                             else _get_system_account('5306'))
        with self.assertRaisesMessage(ValidationError, 'رئيسي'):
            amap.set_mapping('1135', account('1100'))
        with self.assertRaises(ValidationError):
            amap.set_mapping('9999', account('1135'))

    def test_screen_is_admin_only(self):
        login(self.client, self.w.admin)
        r = self.client.get(reverse('accounting:account_mapping'))
        self.assertContains(r, 'ضريبة')
        custom = Account.objects.create(code='1136', name='سلف المناديب', account_type=account('1135').account_type,
                                        parent=account('1135').parent)
        self.client.post(reverse('accounting:account_mapping'), {'code': '1135', 'account': custom.id})
        self.assertEqual(_get_system_account('1135'), custom)
        accountant = make_user('acc_map', role='ACCOUNTANT', branch=self.w.branch,
                               perms=('accounting.view_journalentry',))
        login(self.client, accountant)
        self.assertEqual(self.client.get(reverse('accounting:account_mapping')).status_code, 302)

    def test_no_accounting_signals_left(self):
        from apps.tenant.accounting.models import BankAccount, EWallet, POSMachine, Treasury
        from apps.tenant.invoicing.models import Invoice
        from apps.tenant.partners.models import Partner
        for model in (Treasury, BankAccount, EWallet, POSMachine, Partner, Invoice):
            modules = {getattr(r[1](), '__module__', '') for r in post_save.receivers
                       if r[0][1] == id(model) and r[1]() is not None}
            # only the audit log (who changed what) may listen — never accounting
            self.assertFalse({m for m in modules if m.startswith('apps.tenant')} - {'apps.tenant.core.audit'},
                             model.__name__)
        # …and what they did still happens explicitly
        from apps.tenant.core.tests.factories import make_partner, make_treasury
        t = make_treasury(self.w.branch, 'خزنة برصيد', opening_balance=D('250'))
        self.assertEqual(t.account.current_balance, D('250.00'))
        p = make_partner('عميل جديد')
        self.assertTrue(p.receivable_account.code.startswith('1130-'))
