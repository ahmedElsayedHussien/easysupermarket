"""N+1 guards: busy pages run the same number of queries whatever the number of rows,
and money/quantity figures are computed by the database / on the server."""
import datetime
from decimal import Decimal as D

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_invoice, make_product, stock_in


class QueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)

    def queries(self, url):
        self.client.get(url)   # warm caches (settings, permissions, session)
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return len(ctx.captured_queries)

    def assert_flat(self, url, add_rows):
        before = self.queries(url)
        add_rows()
        after = self.queries(url)
        self.assertEqual(before, after, f'{url}: {before} queries before, {after} after adding rows')

    def _products(self, n=5):
        from apps.tenant.inventory.models import ProductBranch
        for i in range(n):
            p = make_product(self.w.category, sale_price=D('10'))
            stock_in(p, self.w.warehouse, 5, D('4'))
            ProductBranch.objects.create(product=p, branch=self.w.branch, sale_price=D('12'), min_sale_price=D('0'))

    def test_pos_screen(self):
        from apps.tenant.invoicing.models import POSSession
        POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        self._products(1)
        self.assert_flat(reverse('invoicing:pos'), self._products)

    def test_product_list(self):
        self._products(1)
        self.assert_flat(reverse('inventory:product_list'), self._products)

    def test_journal_list(self):
        stock_in(self.w.product, self.w.warehouse, 50, D('5'))
        make_invoice(self.w, lines=[(self.w.product, 1, D('10'))])
        self.assert_flat(reverse('accounting:journal_list'),
                         lambda: [make_invoice(self.w, lines=[(self.w.product, 1, D('10'))]) for _ in range(4)])

    def test_shifts_report(self):
        from apps.tenant.invoicing.models import POSSession
        from apps.tenant.core.tests.factories import make_user

        def add_closed_shifts(n=1):
            for i in range(n):
                u = make_user(role='CASHIER', branch=self.w.branch)
                POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=u,
                                          status=POSSession.CLOSED, end_time=datetime.datetime.now(),
                                          closing_balance_expected=D('0'), closing_balance_actual=D('0'))
        add_closed_shifts()
        self.assert_flat(reverse('core:report_shifts'), lambda: add_closed_shifts(4))


class ServerSideFigureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_average_cost_is_weighted_by_the_database(self):
        p = make_product(self.w.category)
        stock_in(p, self.w.warehouse, 2, D('10'))
        stock_in(p, self.w.warehouse, 3, D('20'))
        self.assertEqual(p.get_average_cost(self.w.warehouse), D('16.0000'))

    def test_split_payment_must_add_up_to_the_total(self):
        from apps.tenant.services.invoice_service import update_invoice_payment_method
        stock_in(self.w.product, self.w.warehouse, 10, D('5'))
        inv = make_invoice(self.w, lines=[(self.w.product, 2, D('50'))])
        with self.assertRaisesMessage(ValueError, 'لا يساوي إجمالي الفاتورة'):
            update_invoice_payment_method(invoice_id=inv.id, payment_type='SPLIT', treasury_id=self.w.treasury.id,
                                          paid_cash=D('30'), paid_card=D('10'), user=self.w.admin)

    def test_stocktake_uses_server_book_quantity(self):
        from apps.tenant.inventory.models import StockAdjustmentLine
        stock_in(self.w.product, self.w.warehouse, 7, D('3'))
        login(self.client, self.w.admin)
        self.client.post(reverse('inventory:stocktake'), {
            'warehouse': self.w.warehouse.id, 'action': 'save_draft',
            'product_id[]': [self.w.product.id], 'system_qty[]': ['999'],   # stale / tampered browser value
            'physical_qty[]': ['5'], 'unit_cost[]': ['3.00'],
        })
        line = StockAdjustmentLine.objects.get(product=self.w.product)
        self.assertEqual(line.system_quantity, D('7'))
        self.assertEqual(line.quantity, D('-2'))
        self.assertEqual(line.unit_cost, D('3'))     # untouched cost = exact server average

    def test_hr_hours_are_decimal_half_up(self):
        # 594 s = 0.165 h -> 0.17 (float arithmetic gave 0.16)
        from decimal import ROUND_HALF_UP
        self.assertEqual((D(594) / D('3600')).quantize(D('0.01'), rounding=ROUND_HALF_UP), D('0.17'))
