from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.core.tests.factories import (
    build_world, login, make_branch, make_user, make_warehouse, stock_in,
)
from apps.tenant.inventory.models import ProductUoM, StockTransfer, UnitOfMeasure


class MultiItemStockTransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.wh_b = make_warehouse(cls.w.branch, 'مخزن فرعي', code='WH-SUB')
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.wh2 = make_warehouse(cls.branch2, 'مخزن الفرع 2', code='WH-2')
        cls.storekeeper = make_user('store1', role='CASHIER', branch=cls.w.branch,
                                    perms=('inventory.change_inventorybatch', 'inventory.view_inventorybatch'))
        carton = UnitOfMeasure.objects.create(name='كرتونة')
        cls.carton = ProductUoM.objects.create(product=cls.w.product, uom=carton, conversion_factor=D('12'))

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, 2)     # 50 pcs @2
        stock_in(self.w.product2, self.w.warehouse, 10, 30)   # 10 @30

    def _post(self, user, lines, to_wh=None, from_wh=None):
        c = login(self.client, user)
        data = {'from_warehouse': (from_wh or self.w.warehouse).id, 'to_warehouse': (to_wh or self.wh2).id,
                'product_id[]': [l[0].id for l in lines], 'uom_id[]': [l[2] for l in lines],
                'quantity[]': [str(l[1]) for l in lines], 'notes': 'test'}
        return c.post('/inventory/transfer/', data)

    def test_transfer_many_items_in_one_document(self):
        r = self._post(self.storekeeper, [(self.w.product, 20, 'base'), (self.w.product2, 4, 'base')])
        doc = StockTransfer.objects.get()
        self.assertRedirects(r, f'/inventory/transfers/{doc.pk}/', fetch_redirect_response=False)
        self.assertEqual(doc.lines.count(), 2)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('30'))
        self.assertEqual(self.w.product.get_stock(self.wh2), D('20'))
        self.assertEqual(self.w.product2.get_stock(self.wh2), D('4'))
        self.assertEqual(doc.total_cost, D('160'))          # 20*2 + 4*30
        self.assertTrue(doc.number.startswith('STK-'))
        self.assertTrue(doc.is_inter_branch)

    def test_all_or_nothing_when_one_line_is_short(self):
        self._post(self.storekeeper, [(self.w.product, 20, 'base'), (self.w.product2, 999, 'base')])
        self.assertFalse(StockTransfer.objects.exists())
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('50'))   # nothing moved
        self.assertEqual(self.w.product.get_stock(self.wh2), D('0'))

    def test_unit_conversion(self):
        self._post(self.storekeeper, [(self.w.product, 2, str(self.carton.id))])   # 2 cartons = 24 pcs
        line = StockTransfer.objects.get().lines.get()
        self.assertEqual(line.base_quantity, D('24'))
        self.assertEqual(self.w.product.get_stock(self.wh2), D('24'))

    def test_duplicate_rows_are_merged(self):
        self._post(self.storekeeper, [(self.w.product, 5, 'base'), (self.w.product, 7, 'base')])
        doc = StockTransfer.objects.get()
        self.assertEqual(doc.lines.count(), 1)
        self.assertEqual(doc.lines.get().base_quantity, D('12'))

    def test_same_warehouse_rejected(self):
        self._post(self.storekeeper, [(self.w.product, 1, 'base')], to_wh=self.w.warehouse)
        self.assertFalse(StockTransfer.objects.exists())

    def test_user_cannot_transfer_from_other_branch_warehouse(self):
        stock_in(self.w.product, self.wh2, 10, 2)
        self._post(self.storekeeper, [(self.w.product, 5, 'base')], from_wh=self.wh2, to_wh=self.w.warehouse)
        self.assertFalse(StockTransfer.objects.exists())
        self.assertEqual(self.w.product.get_stock(self.wh2), D('10'))

    def test_same_branch_transfer(self):
        self._post(self.storekeeper, [(self.w.product, 10, 'base')], to_wh=self.wh_b)
        self.assertFalse(StockTransfer.objects.get().is_inter_branch)
        self.assertEqual(self.w.product.get_stock(self.wh_b), D('10'))

    def test_list_and_detail_pages(self):
        self._post(self.storekeeper, [(self.w.product, 3, 'base')])
        doc = StockTransfer.objects.get()
        c = login(self.client, self.storekeeper)
        self.assertContains(c.get('/inventory/transfers/'), doc.number)
        self.assertContains(c.get(f'/inventory/transfers/{doc.pk}/'), self.w.product.name)

    def test_form_page_renders(self):
        r = login(self.client, self.storekeeper).get('/inventory/transfer/')
        self.assertEqual(r.status_code, 200)
        # source list = own branch warehouses only; destinations include the other branch
        self.assertEqual(set(r.context['sources']), {self.w.warehouse, self.wh_b})
        self.assertIn(self.wh2, set(r.context['destinations']))
