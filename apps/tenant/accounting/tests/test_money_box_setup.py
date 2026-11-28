"""P1-6: إنشاء خزينة / بنك برصيد أول مدة في فترة مقفولة = رسالة في الفورم (مش 500)، ensure_account ما بتبلعش
الأخطاء، والـ migration بتكمّل الحسابات الناقصة."""
import datetime
import importlib
from decimal import Decimal as D
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import BankAccount, Treasury
from apps.tenant.core.tests.factories import build_world, login, system_settings


class MoneyBoxSetupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)

    def test_opening_date_in_closed_period_is_a_form_error(self):
        system_settings(accounting_lock_date=datetime.date(2025, 6, 30))
        r = self.client.post(reverse('accounting:treasury_create'), {
            'name': 'خزنة قديمة', 'branch': self.w.branch.id, 'treasury_type': Treasury.CASH,
            'opening_balance': '1000', 'opening_balance_date': '2025-06-01'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['form'].errors.get('opening_balance_date'))
        self.assertFalse(Treasury.objects.filter(name='خزنة قديمة').exists())
        r = self.client.post(reverse('accounting:bank_create'), {
            'name': 'بنك قديم', 'opening_balance': '500', 'opening_balance_date': '2025-01-01'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(BankAccount.objects.filter(name='بنك قديم').exists())

    def test_ledger_error_at_save_is_shown_on_the_form_and_nothing_is_saved(self):
        with mock.patch('apps.tenant.accounting.models.book_opening_balance',
                        side_effect=ValueError('الحساب 3100 مش موجود')):
            r = self.client.post(reverse('accounting:bank_create'), {'name': 'بنك جديد', 'opening_balance': '500'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'الحساب 3100 مش موجود')
        self.assertFalse(BankAccount.objects.filter(name='بنك جديد').exists())

    def test_ensure_account_does_not_swallow_errors(self):
        t = Treasury.objects.create(name='خزنة', branch=self.w.branch)
        Treasury.objects.filter(pk=t.pk).update(account=None)
        t.refresh_from_db()
        with mock.patch('apps.tenant.services.journal_service._get_system_account',
                        side_effect=ValueError('مفيش 1110')):
            with self.assertRaisesMessage(ValueError, 'مفيش 1110'):
                t.ensure_account()

    def test_migration_fills_missing_accounts(self):
        t = Treasury.objects.create(name='خزنة بلا حساب', branch=self.w.branch)
        Treasury.objects.filter(pk=t.pk).update(account=None)
        mig = importlib.import_module('apps.tenant.accounting.migrations.0044_fill_missing_money_accounts')
        mig.fill(None, None)
        t.refresh_from_db()
        self.assertIsNotNone(t.account_id)
        self.assertEqual(t.account.code, f'1110-{t.pk}')


class RepCashBoxVisibilityTests(TestCase):
    """P2-1 regression: a sales rep's cash box is managed on the treasury screens, never picked for money."""
    @classmethod
    def setUpTestData(cls):
        from apps.tenant.core.tests.factories import make_user
        from apps.tenant.invoicing import van_services as van
        from apps.tenant.invoicing.models import SalesRep
        cls.w = build_world()
        u = make_user('box_rep', role='SALES_REP', branch=cls.w.branch)
        cls.rep = SalesRep.objects.create(employee=u.employee_profile, source_warehouse=cls.w.warehouse)
        van.ensure_rep_setup(cls.rep, create_van=True, create_treasury=True)
        cls.rep.refresh_from_db()

    def test_admin_sees_rep_box_on_list_not_in_receipt_choices(self):
        login(self.client, self.w.admin)
        r = self.client.get(reverse('accounting:treasury_list'))
        self.assertIn(self.rep.treasury, list(r.context['treasuries']))
        self.assertEqual(self.client.get(reverse('accounting:treasury_update', args=[self.rep.treasury.pk])).status_code,
                         200)
        r = self.client.get(reverse('accounting:receipt_create'))
        self.assertNotIn(self.rep.treasury, list(r.context['form'].fields['treasury'].queryset))
