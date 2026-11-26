"""شركات التحصيل الوسيطة: المندوب يورّد المبلغ كامل في فوري، والمحاسب يسوّي لما فوري تحوّل للبنك ناقص نسبتها."""
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.intermediary_views import balances, settle
from apps.tenant.accounting.models import IntermediaryCompany, IntermediarySettlement
from apps.tenant.core.tests.factories import (build_world, ledger_totals, login, make_bank_account, make_user,
                                              stock_in)
from apps.tenant.invoicing import van_services as van
from apps.tenant.invoicing.models import RepCashHandover, SalesRep


class IntermediaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        u = make_user('fawry_rep', role='SALES_REP', branch=cls.w.branch)
        cls.rep_user = u
        cls.rep = SalesRep.objects.create(employee=u.employee_profile, source_warehouse=cls.w.warehouse)
        van.ensure_rep_setup(cls.rep, create_van=True, create_treasury=True)
        cls.rep.refresh_from_db()
        cls.bank = make_bank_account('البنك الأهلي')

    def setUp(self):
        stock_in(self.w.product, self.rep.van_warehouse, 50, D('5'))
        van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 10}])     # 114 in his box
        login(self.client, self.w.admin)

    def test_full_cycle_through_fawry(self):
        url = reverse('accounting:intermediaries')
        self.client.post(url, {'action': 'add', 'name': 'فوري'})
        fawry = IntermediaryCompany.objects.get(name='فوري')
        self.assertTrue(fawry.account.code.startswith('1157-'))
        # the rep pays the FULL amount at Fawry — a reference is required
        h_url = reverse('invoicing:handover_list')
        self.client.post(h_url, {'rep': self.rep.id, 'amount': '114', 'destination': f'i:{fawry.id}'})
        self.assertFalse(RepCashHandover.objects.exists())
        self.client.post(h_url, {'rep': self.rep.id, 'amount': '114', 'destination': f'i:{fawry.id}',
                                 'reference': 'FWR-778899'})
        h = RepCashHandover.objects.get()
        self.assertEqual((h.intermediary, h.reference, h.amount), (fawry, 'FWR-778899', D('114.00')))
        self.assertEqual(van.cash_balance(self.rep), D('0'))
        self.assertEqual(balances([fawry])[fawry.id], D('114.00'))                       # Fawry holds it
        # Fawry pays the bank 112 (2 commission) → the accountant records it
        r = self.client.post(url, {'action': 'settle', 'company': fawry.id, 'gross': '114', 'fee': '2',
                                   'bank': self.bank.id, 'reference': 'TRX-1'})
        s = IntermediarySettlement.objects.get()
        self.assertEqual((s.gross, s.fee, s.net), (D('114.00'), D('2.00'), D('112.00')))
        self.assertEqual(balances([fawry])[fawry.id], D('0'))
        self.assertEqual(self.bank.ensure_account().current_balance, D('112.00'))
        from apps.tenant.accounting.models import Account
        self.assertEqual(Account.objects.get(code='5306').current_balance, D('2.00'))
        debit, credit = ledger_totals()
        self.assertEqual(debit, credit)
        page = self.client.get(url)
        self.assertContains(page, 'FWR-778899')
        self.assertEqual(r.status_code, 302)

    def test_settlement_rules_and_permissions(self):
        fawry = IntermediaryCompany.objects.create(name='أمان')
        fawry.ensure_account()
        van.post_handover(self.rep, '50', self.w.admin, intermediary=fawry, reference='AMN-1')
        with self.assertRaises(ValidationError):                                       # more than it holds
            settle(fawry, '60', '0', self.bank)
        with self.assertRaises(ValidationError):                                       # fee ≥ amount
            settle(fawry, '50', '50', self.bank)
        login(self.client, make_user('plain_cashier', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('accounting:intermediaries')).status_code, 302)


class IntermediaryVatTests(IntermediaryTests):
    """ضريبة 14% على عمولة فوري: مدخلات لو الشركة مسجلة، وإلا مصروف."""

    def _fawry_with_114(self):
        fawry = IntermediaryCompany.objects.create(name='فوري')
        fawry.ensure_account()
        van.post_handover(self.rep, '114', self.w.admin, intermediary=fawry, reference='F-1')
        return fawry

    def test_vat_on_commission_is_input_vat_when_registered(self):
        from apps.tenant.accounting.models import Account
        fawry = self._fawry_with_114()
        s = settle(fawry, '114', '2', self.bank, vat='0.28', vat_recoverable=True)
        self.assertEqual(s.net, D('111.72'))
        self.assertEqual(self.bank.ensure_account().current_balance, D('111.72'))
        self.assertEqual(Account.objects.get(code='5306').current_balance, D('2.00'))
        self.assertEqual(Account.objects.get(code='1190').current_balance, D('0.28'))
        self.assertEqual(balances([fawry])[fawry.id], D('0'))
        debit, credit = ledger_totals()
        self.assertEqual(debit, credit)

    def test_vat_is_expense_when_not_registered(self):
        from apps.tenant.accounting.models import Account
        fawry = self._fawry_with_114()
        settle(fawry, '114', '2', self.bank, vat='0.28', vat_recoverable=False)
        self.assertEqual(Account.objects.get(code='5306').current_balance, D('2.28'))
        self.assertEqual(Account.objects.get(code='1190').current_balance, D('0'))

    def test_screen_posts_vat(self):
        fawry = self._fawry_with_114()
        self.client.post(reverse('accounting:intermediaries'), {'action': 'settle', 'company': fawry.id, 'gross': '114',
                                                                'fee': '2', 'vat': '0.28', 'vat_recoverable': 'on',
                                                                'bank': self.bank.id})
        s = IntermediarySettlement.objects.get()
        self.assertEqual((s.fee, s.vat, s.vat_recoverable, s.net), (D('2.00'), D('0.28'), True, D('111.72')))
        self.assertContains(self.client.get(reverse('accounting:intermediaries')), 'مدخلات')


class HandoverReferenceTests(IntermediaryTests.__bases__[0]):
    @classmethod
    def setUpTestData(cls):
        IntermediaryTests.setUpTestData.__func__(cls)

    def setUp(self):
        IntermediaryTests.setUp(self)

    def test_same_receipt_twice_refused(self):
        fawry = IntermediaryCompany.objects.create(name='فوري')
        van.post_handover(self.rep, '50', self.w.admin, intermediary=fawry, reference='FWR-1')
        with self.assertRaisesMessage(ValidationError, 'مرة واحدة'):
            van.post_handover(self.rep, '50', self.w.admin, intermediary=fawry, reference='FWR-1')
        self.assertEqual(RepCashHandover.objects.count(), 1)
