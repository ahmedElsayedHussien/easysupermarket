from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.core.tests.factories import build_world, login, make_invoice, make_partner, stock_in
from apps.tenant.partners.models import Partner


class PartnersDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.limited = make_partner('عميل بحد ائتمان', 'CUSTOMER', credit_limit=D('100'))
        cls.dormant = make_partner('عميل قديم', 'CUSTOMER', opening_balance=D('700'), opening_balance_type=Partner.OPENING_DEBIT)

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 100, 5)
        # credit sale 300 to the main customer, 95 to the limited one (95% of its limit)
        make_invoice(self.w, 'SALE', lines=[(self.w.product, 30, 10, 0)], payment_type='CREDIT', partner=self.w.customer,
                     confirm=True)
        make_invoice(self.w, 'SALE', lines=[(self.w.product, 1, 95, 0)], payment_type='CREDIT', partner=self.limited,
                     confirm=True)
        # credit purchase 500 from the supplier
        make_invoice(self.w, 'PURCHASE', lines=[(self.w.product, 50, 10, 0)], payment_type='CREDIT', partner=self.w.supplier,
                     confirm=True)

    def _get(self):
        return login(self.client, self.w.admin).get('/partners/dashboard/')

    def test_page_and_totals(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['receivables'], D('1095'))    # 300 + 95 + 700 opening
        self.assertEqual(r.context['payables'], D('500'))
        self.assertEqual(r.context['net_position'], D('595'))

    def test_top_lists(self):
        r = self._get()
        self.assertEqual(r.context['top_debtors'][0]['label'], 'عميل قديم')
        self.assertEqual(r.context['top_creditors'][0]['value'], D('500'))

    def test_credit_limit_usage(self):
        rows = {x['partner'].pk: x for x in self._get().context['credit_rows']}
        self.assertEqual(rows[self.limited.pk]['level'], 'warning')     # 95%
        self.assertAlmostEqual(rows[self.limited.pk]['usage'], 95.0)

    def test_dormant_debtors(self):
        names = [d['partner'].name for d in self._get().context['dormant']]
        self.assertIn('عميل قديم', names)            # opening balance only, no activity
        self.assertNotIn(self.w.customer.name, names)  # invoiced today

    def test_trend_counts_credit_sales_this_month(self):
        t = self._get().context['trend']
        self.assertEqual(len(t['labels']), 6)
        self.assertEqual(t['credit_sales'][-1], 395.0)
        self.assertEqual(t['credit_purchases'][-1], 500.0)

    def test_cashier_without_permission_redirected(self):
        r = login(self.client, self.w.cashier).get('/partners/dashboard/')
        self.assertEqual(r.status_code, 302)

    def test_sidebar_single_partners_link(self):
        html = self._get().content.decode()
        self.assertIn('لوحة الشركاء', html)
        for old in ('/partners/customers/" class="nav-link', '/partners/suppliers/" class="nav-link'):
            self.assertNotIn(old, html)

    def test_receipt_prefilled_with_partner(self):
        r = login(self.client, self.w.admin).get(f'/accounting/receipts/create/?partner={self.dormant.pk}')
        self.assertEqual(r.context['form'].initial.get('partner'), self.dormant.pk)


class PartnersDashboardSettingsTests(TestCase):
    """dormant_debt_days / credit_limit_warning_percent come from SystemSetting."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.limited = make_partner('عميل بحد', 'CUSTOMER', credit_limit=D('100'))

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 100, 5)
        make_invoice(self.w, 'SALE', lines=[(self.w.product, 1, 70, 0)], payment_type='CREDIT', partner=self.limited,
                     confirm=True)   # 70% of the limit

    def _get(self):
        return login(self.client, self.w.admin).get('/partners/dashboard/')

    def _level(self):
        rows = {x['partner'].pk: x for x in self._get().context['credit_rows']}
        return rows[self.limited.pk]['level']

    def test_warning_percent_from_settings(self):
        from apps.tenant.core.tests.factories import system_settings
        self.assertEqual(self._level(), 'good')          # default 80%
        system_settings(credit_limit_warning_percent=60)
        self.assertEqual(self._level(), 'warning')

    def test_dormant_days_from_settings(self):
        import datetime
        from apps.tenant.core.tests.factories import system_settings
        from apps.tenant.invoicing.models import Invoice
        Invoice.objects.filter(partner=self.limited).update(date=datetime.date.today() - datetime.timedelta(days=20))
        names = lambda: [d['partner'].pk for d in self._get().context['dormant']]  # noqa: E731
        self.assertNotIn(self.limited.pk, names())       # default 60 days
        system_settings(dormant_debt_days=10)
        self.assertIn(self.limited.pk, names())
        self.assertEqual(self._get().context['dormant_days'], 10)
