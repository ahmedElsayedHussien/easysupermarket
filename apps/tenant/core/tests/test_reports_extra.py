"""تقارير المخزون: دوران المخزون، الهالك والعجز، تتبع التشغيلة."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_invoice, make_product, make_user, stock_in


class InventoryReportsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.dead = make_product(cls.w.category, 'صنف راكد', sale_price=D('20'))

    def setUp(self):
        login(self.client, self.w.admin)
        stock_in(self.w.product, self.w.warehouse, 100, D('5'))
        stock_in(self.dead, self.w.warehouse, 10, D('12'))
        make_invoice(self.w, lines=[(self.w.product, 60, D('10'))])      # COGS 300, 40 left (value 200)

    def test_turnover_and_dead_stock(self):
        r = self.client.get(reverse('core:report_inventory_turnover'))
        rows = {x['name']: x for x in r.context['rows']}
        self.assertEqual(rows[self.w.product.name]['cogs'], D('300'))
        self.assertEqual(rows[self.w.product.name]['turnover'], D('1.50'))
        self.assertEqual(rows['صنف راكد']['klass'], 'dead')
        self.assertEqual(r.context['dead_value'], D('120'))

    def test_wastage(self):
        from apps.tenant.inventory.models import StockMovement
        StockMovement.objects.create(product=self.w.product, warehouse=self.w.warehouse, movement_type='ADJUSTMENT_OUT',
                                     quantity=D('3'), unit_cost=D('5'), reference='تالف')
        r = self.client.get(reverse('core:report_wastage'))
        self.assertEqual(r.context['totals']['lost'], D('15'))

    def test_batch_trace_and_permission(self):
        r = self.client.get(reverse('core:report_batch_trace'), {'q': self.w.product.name})
        self.assertTrue(r.context['batches'])
        self.assertTrue(any(b.moves for b in r.context['batches']))
        login(self.client, make_user('no_inv_reports', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('core:report_wastage')).status_code, 302)
