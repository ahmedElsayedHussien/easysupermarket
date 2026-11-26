"""Home page: today's figures, needs-attention alerts, quick actions, chart, modules."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import (build_world, login, make_branch, make_invoice, make_user,
                                              make_warehouse, stock_in)


class HomeDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        stock_in(cls.w.product, cls.w.warehouse, 100, D('6'))

    def get(self, user=None):
        login(self.client, user or self.w.admin)
        r = self.client.get(reverse('core:main_screen'))
        self.assertEqual(r.status_code, 200)
        return r

    def test_today_figures_net_of_returns_and_change_vs_yesterday(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        make_invoice(self.w, lines=[(self.w.product, 10, D('10'))], date=yesterday)       # 100 yesterday
        make_invoice(self.w, lines=[(self.w.product, 15, D('10'))])                       # 150 today
        make_invoice(self.w, invoice_type='RETURN_SALE', lines=[(self.w.product, 3, D('10'))])  # -30 today
        f = self.get().context['figures']
        self.assertEqual(f['sales'], D('120'))
        self.assertEqual(f['invoices'], 1)
        self.assertEqual(f['change'], 20.0)
        self.assertEqual(f['profit'], D('120') - D('72'))   # 12 net units x cost 6

    def test_chart_and_top_items(self):
        make_invoice(self.w, lines=[(self.w.product, 4, D('10'))])
        r = self.get()
        week = r.context['week']
        self.assertEqual(len(week['days']), 7)
        self.assertTrue(week['days'][-1]['is_today'])
        self.assertEqual(week['days'][-1]['pct'], 100.0)
        self.assertEqual(r.context['top_items'][0]['product_id'], self.w.product.id)

    def test_figures_follow_the_header_branch(self):
        b2 = make_branch('فرع تاني')
        wh2 = make_warehouse(b2, 'مخزن تاني')
        stock_in(self.w.product, wh2, 10, D('6'))
        make_invoice(self.w, lines=[(self.w.product, 5, D('10'))], branch=b2, warehouse=wh2, treasury=None,
                     payment_type='CREDIT')
        login(self.client, self.w.admin)
        session = self.client.session
        session['current_branch_id'] = self.w.branch.id
        session.save()
        r = self.client.get(reverse('core:main_screen'))
        self.assertEqual(r.context['figures']['sales'], D('0'))

    def test_incoming_transfer_alert(self):
        from apps.tenant.accounting.models import InternalTransfer
        from apps.tenant.core.tests.factories import make_treasury
        b2 = make_branch('فرع المرسل')
        t2 = make_treasury(b2, 'خزينة المرسل', opening_balance=D('1000'))
        InternalTransfer.objects.create(amount=D('300'), from_account_type='TREASURY', from_treasury=t2,
                                        to_account_type='TREASURY', to_treasury=self.w.treasury,
                                        status=InternalTransfer.IN_TRANSIT)
        alerts = self.get().context['alerts']
        self.assertTrue(any('تحويل' in a['text'] and a['count'] == 1 for a in alerts))

    def test_cashier_sees_no_money_and_only_allowed_links(self):
        cashier = make_user('home_cashier_x', role='CASHIER', branch=self.w.branch, perms=('invoicing.view_invoice',))
        r = self.get(cashier)
        self.assertFalse(r.context['show_figures'])
        self.assertNotIn('figures', r.context)
        titles = [m['title'] for m in r.context['modules']]
        self.assertIn('نقطة البيع', titles)
        self.assertNotIn('الإعدادات', titles)
        self.assertNotIn('الحسابات', titles)
        self.assertNotContains(r, 'صافي مبيعات اليوم')
        labels = [a['label'] for a in r.context['quick_actions']]
        self.assertEqual(labels, ['نقطة البيع'])

    def test_admin_sees_new_modules(self):
        r = self.get()
        urls = [m['url'] for m in r.context['modules']]
        self.assertIn(reverse('inventory:dashboard'), urls)
        self.assertIn(reverse('accounting:accounting_dashboard'), urls)
        self.assertIn(reverse('invoicing:purchase_invoice_list'), urls)
        self.assertContains(r, 'محتاج انتباهك')
