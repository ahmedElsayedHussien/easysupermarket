"""شاشة التحويل المخزني: أول ما يتختار المخزن المصدر تظهر أصنافه بكمياتها."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_branch, make_product, make_user, make_warehouse, stock_in


class WarehouseStockPanelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.service = make_product(cls.w.category, 'خدمة توصيل', product_type='SERVICE')
        cls.other = make_warehouse(make_branch('فرع تاني'), 'مخزن فرع تاني', code='WH-OTH')

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 12, D('5'))
        stock_in(self.w.product2, self.w.warehouse, 3, D('20'))
        stock_in(self.w.product, self.other, 7, D('5'))

    def test_lists_products_with_quantities(self):
        login(self.client, self.w.admin)
        d = self.client.get(reverse('inventory:api_warehouse_stock'), {'warehouse_id': self.w.warehouse.id}).json()
        rows = {p['id']: p['available_stock'] for p in d['products']}
        self.assertEqual(rows, {self.w.product.id: 12.0, self.w.product2.id: 3.0})
        self.assertEqual(d['products'][0]['uoms'][0]['id'], 'base')
        self.assertContains(self.client.get(reverse('inventory:transfer_stock')), 'id="whStock"')

    def test_branch_scope_and_permission(self):
        keeper = make_user('st_keeper', role='INVENTORY', branch=self.w.branch, perms=('inventory.change_inventorybatch',))
        login(self.client, keeper)
        d = self.client.get(reverse('inventory:api_warehouse_stock'), {'warehouse_id': self.other.id}).json()
        self.assertEqual(d['products'], [])                                   # another branch's warehouse
        login(self.client, make_user('no_stock', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('inventory:api_warehouse_stock'),
                                         {'warehouse_id': self.w.warehouse.id}).status_code, 302)

    def test_stock_level_api_is_branch_scoped(self):
        url = reverse('inventory:api_stock_level')
        keeper = make_user('lvl_keeper', role='INVENTORY', branch=self.w.branch, perms=('inventory.view_inventorybatch',))
        login(self.client, keeper)
        r = self.client.get(url, {'product_id': self.w.product.id, 'warehouse_id': self.w.warehouse.id})
        self.assertEqual(D(r.json()['stock']), D('12'))
        r = self.client.get(url, {'product_id': self.w.product.id, 'warehouse_id': self.other.id})
        self.assertEqual(r.status_code, 404)                                  # another branch's warehouse
        self.assertEqual(self.client.get(url, {'product_id': self.w.product.id}).status_code, 400)   # company total
        self.assertEqual(self.client.get(url, {'product_id': 'x', 'warehouse_id': '1'}).status_code, 400)
        login(self.client, self.w.admin)                                       # admin on "all branches" in the header
        r = self.client.get(url, {'product_id': self.w.product.id, 'warehouse_id': self.other.id, 'branch': 'all'})
        self.assertEqual(D(r.json()['stock']), D('7'))
        login(self.client, make_user('lvl_cashier', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(url, {'product_id': self.w.product.id,
                                               'warehouse_id': self.w.warehouse.id}).status_code, 302)


class InventoryHardeningTests(TestCase):
    """FEFO، منع بيع المنتهي، إلزام الصلاحية، البيع تحت أدنى سعر، مين عمل الحركة، عدم تكرار الأجهزة، أكواد ETA."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def _batch(self, qty, cost, expiry=None):
        from apps.tenant.services.fifo_engine import add_inventory_batch
        return add_inventory_batch(self.w.product, self.w.warehouse, D(qty), D(cost), expiry_date=expiry)

    def test_fefo_nearest_expiry_leaves_first(self):
        import datetime
        from apps.tenant.services.fifo_engine import consume_fifo_batches
        today = datetime.date.today()
        old_far = self._batch('5', '4', today + datetime.timedelta(days=200))      # bought first, expires late
        new_near = self._batch('5', '6', today + datetime.timedelta(days=10))      # bought later, expires soon
        used = consume_fifo_batches(self.w.product, self.w.warehouse, D('5'))
        self.assertEqual([c['batch'].pk for c in used], [new_near.pk])
        self.assertTrue(old_far.pk)

    def test_expired_not_sold_without_permission(self):
        import datetime
        from apps.tenant.core.tests.factories import add_perms, make_invoice
        self._batch('3', '5', datetime.date.today() - datetime.timedelta(days=1))   # expired
        with self.assertRaisesMessage(ValueError, 'منتهي الصلاحية'):
            make_invoice(self.w, lines=[(self.w.product, 2, D('10'))], cashier=self.w.cashier)
        add_perms(self.w.cashier, 'inventory.sell_expired_batch')
        from django.contrib.auth.models import User
        cashier = User.objects.get(pk=self.w.cashier.pk)
        inv = make_invoice(self.w, lines=[(self.w.product, 2, D('10'))], cashier=cashier)
        self.assertEqual(inv.status, 'POSTED')

    def test_purchase_of_expiring_product_needs_the_date(self):
        from apps.tenant.core.tests.factories import make_invoice
        self.w.product.has_expiry = True
        self.w.product.save()
        with self.assertRaisesMessage(ValueError, 'تاريخ الصلاحية'):
            make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.w.product, 5, D('4'))])

    def test_movement_records_the_user_and_devices_are_unique(self):
        from django.db import IntegrityError, transaction
        from apps.tenant.inventory.models import SerialItem, StockMovement
        login(self.client, self.w.admin)
        stock_in(self.w.product, self.w.warehouse, 10, D('5'))
        self.client.post(reverse('inventory:transfer_stock'), {
            'from_warehouse': self.w.warehouse.id, 'to_warehouse': self.other.id if hasattr(self, 'other') else
            make_warehouse(self.w.branch, 'مخزن 2', code='WH-2').id,
            'product_id[]': [self.w.product.id], 'uom_id[]': [''], 'quantity[]': ['2']})
        m = StockMovement.objects.filter(movement_type='TRANSFER_OUT').first()
        self.assertEqual(m.created_by, self.w.admin)
        SerialItem.objects.create(product=self.w.product, imei_1='356938035643809', actual_cost=D('1'))
        with self.assertRaises(IntegrityError), transaction.atomic():
            SerialItem.objects.create(product=self.w.product, imei_1='356938035643809', actual_cost=D('1'))
        SerialItem.objects.create(product=self.w.product, imei_1='', actual_cost=D('1'))      # blanks are fine
        SerialItem.objects.create(product=self.w.product, imei_1='', actual_cost=D('1'))

    def test_eta_codes_from_unit_and_product(self):
        from apps.tenant.einvoicing.services import unit_type
        from apps.tenant.inventory.models import ProductUoM, UnitOfMeasure
        from types import SimpleNamespace
        sack = UnitOfMeasure.objects.create(name='شكارة', eta_code='BG')
        puom = ProductUoM.objects.create(product=self.w.product, uom=sack, conversion_factor=D('25'))
        self.assertEqual(unit_type(SimpleNamespace(uom_id=puom.id, uom=puom, product=self.w.product)), 'BG')
        self.w.product.eta_tax_subtype = 'V004'
        self.w.product.save()
        self.assertEqual(self.w.product.eta_tax_subtype, 'V004')

    def test_lock_order_and_typed_error(self):
        from apps.tenant.services.fifo_engine import InsufficientStockError, consume_fifo_batches, lock_order
        from types import SimpleNamespace
        rows = [SimpleNamespace(product_id=9), SimpleNamespace(product_id=2), SimpleNamespace(product_id=5)]
        self.assertEqual([r.product_id for r in lock_order(rows)], [2, 5, 9])
        with self.assertRaises(InsufficientStockError):
            consume_fifo_batches(self.w.product2, self.w.warehouse, D('1'))
        self.assertTrue(issubclass(InsufficientStockError, ValueError))           # old callers still catch it


class StockAdjustmentRulesTests(TestCase):
    """ترحيل التسويات: الجرد بصلاحيته، لا سيريال ولا خدمة في تسوية كمية، ولا تكلفة صفر، والكمية موجبة."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.service = make_product(cls.w.category, 'خدمة تركيب', product_type='SERVICE')

    def _adj(self, kind, product, qty=D('5'), cost=D('0'), physical=D('0')):
        from apps.tenant.inventory.models import StockAdjustment, StockAdjustmentLine
        a = StockAdjustment.objects.create(adjustment_type=kind, warehouse=self.w.warehouse, reason='اختبار',
                                           created_by=self.w.admin)
        StockAdjustmentLine.objects.create(adjustment=a, product=product, quantity=qty, unit_cost=cost,
                                           physical_quantity=physical)
        return a

    def _confirm(self, adj, user=None):
        login(self.client, user or self.w.admin)
        self.client.post(reverse('inventory:adjustment_confirm', args=[adj.pk]))
        adj.refresh_from_db()
        return adj.status

    def test_stocktake_needs_its_permission(self):
        stock_in(self.w.product, self.w.warehouse, 10, D('5'))
        adj = self._adj('STOCKTAKE', self.w.product, physical=D('8'))
        clerk = make_user('adj_clerk', role='INVENTORY', branch=self.w.branch,
                          perms=('inventory.change_stockadjustment',))
        self.assertEqual(self._confirm(adj, clerk), 'DRAFT')
        self.assertEqual(self._confirm(adj), 'POSTED')                             # the admin may

    def test_no_zero_cost_in_and_no_service_value(self):
        from apps.tenant.accounting.models import JournalEntry
        fresh = make_product(self.w.category, 'صنف جديد بلا تكلفة')
        self.assertEqual(self._confirm(self._adj('IN', fresh)), 'DRAFT')           # no cost anywhere
        self.assertEqual(self._confirm(self._adj('IN', fresh, cost=D('7'))), 'POSTED')
        before = JournalEntry.objects.count()
        self.assertEqual(self._confirm(self._adj('IN', self.service, cost=D('50'))), 'POSTED')
        self.assertEqual(JournalEntry.objects.count(), before)                      # no inventory value for a service

    def test_quantity_must_be_positive_and_serials_refused(self):
        stock_in(self.w.product, self.w.warehouse, 10, D('5'))
        self.assertEqual(self._confirm(self._adj('OUT', self.w.product, qty=D('0'))), 'DRAFT')
        phone = make_product(self.w.category, 'موبايل', product_type='SERIALIZED')
        self.assertEqual(self._confirm(self._adj('IN', phone, cost=D('900'))), 'DRAFT')
