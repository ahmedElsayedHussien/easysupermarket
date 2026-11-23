import datetime
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.core.tests.factories import (build_world, login, make_branch, make_warehouse, stock_in,
                                              system_settings)


class InventoryDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.wh2 = make_warehouse(cls.branch2, 'مخزن 2', code='WH-2')

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 10, 5)     # 50
        stock_in(self.w.product2, self.w.warehouse, 2, 30)    # 60
        stock_in(self.w.product2, self.wh2, 4, 30)            # 120 (other branch)
        self.w.product.min_stock_level = D('20')              # 10 on hand -> low stock
        self.w.product.save(update_fields=['min_stock_level'])

    def _get(self, header='all'):
        c = login(self.client, self.w.admin)
        c.get(f'/?branch={header}')
        return c.get('/inventory/dashboard/')

    def test_all_branches_value_and_counts(self):
        r = self._get('all')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_value'], D('230'))
        self.assertEqual(r.context['warehouses_count'], 2)
        self.assertEqual(r.context['low_stock_count'], 1)
        self.assertEqual(r.context['low_stock'][0][0], self.w.product)

    def test_header_branch_scopes_warehouses(self):
        r = self._get(self.w.branch.id)
        self.assertEqual(r.context['total_value'], D('110'))
        self.assertEqual([row['obj'] for row in r.context['wh_rows']], [self.w.warehouse])

    def test_flow_counts_purchases_in(self):
        r = self._get('all')
        self.assertAlmostEqual(r.context['total_in'], 230.0)
        self.assertEqual(r.context['total_out'], 0.0)

    def test_expiry_alerts(self):
        from apps.tenant.inventory.models import InventoryBatch
        system_settings(enable_expiry_tracking=True)
        b = InventoryBatch.objects.filter(product=self.w.product).first()
        b.expiry_date = datetime.date.today() - datetime.timedelta(days=1)
        b.save(update_fields=['expiry_date'])
        r = self._get('all')
        self.assertEqual(r.context['expired_count'], 1)
        self.assertEqual(r.context['expired_value'], D('50'))

    def test_stagnant_stock(self):
        r = self._get('all')
        # nothing sold yet -> everything on hand is stagnant
        self.assertEqual(r.context['stagnant_count'], 2)

    def test_cashier_without_permission_redirected(self):
        r = login(self.client, self.w.cashier).get('/inventory/dashboard/')
        self.assertEqual(r.status_code, 302)

    def test_sidebar_single_inventory_link(self):
        html = self._get('all').content.decode()
        self.assertIn('لوحة المخزون', html)
        for old in ('/inventory/stocktake/" class="nav-link', '/inventory/uom/" class="nav-link',
                    '/inventory/opening-stock/" class="nav-link'):
            self.assertNotIn(old, html)


class InventoryDashboardSettingsTests(TestCase):
    """Thresholds come from SystemSetting (stagnant / expiry / low stock / expiry tracking)."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 10, 5)
        stock_in(self.w.product2, self.w.warehouse, 3, 30)

    def _get(self):
        c = login(self.client, self.w.admin)
        c.get('/?branch=all')
        return c.get('/inventory/dashboard/')

    def _set_expiry(self, product, days):
        from apps.tenant.inventory.models import InventoryBatch
        InventoryBatch.objects.filter(product=product).update(
            expiry_date=datetime.date.today() + datetime.timedelta(days=days))

    def test_stagnant_days_from_settings(self):
        system_settings(stagnant_stock_days=45)
        r = self._get()
        self.assertEqual(r.context['stagnant_days'], 45)
        self.assertIn('45 يوم بدون صرف', r.content.decode())

    def test_low_stock_falls_back_to_global_threshold(self):
        # neither product has its own min level -> SystemSetting.low_stock_threshold (default 5)
        r = self._get()
        self.assertEqual([t[0] for t in r.context['low_stock']], [self.w.product2])   # 3 <= 5, 10 > 5
        self.assertEqual(r.context['low_stock'][0][2], 5)
        system_settings(low_stock_threshold=0)
        self.assertEqual(self._get().context['low_stock_count'], 0)

    def test_product_min_level_overrides_global(self):
        self.w.product2.min_stock_level = D('2')        # 3 on hand > own level 2
        self.w.product2.save(update_fields=['min_stock_level'])
        system_settings(low_stock_threshold=50)
        r = self._get()
        self.assertEqual([t[0] for t in r.context['low_stock']], [self.w.product])   # 10 <= 50

    def test_expiry_warning_days_from_settings(self):
        system_settings(enable_expiry_tracking=True, expiry_warning_days=10)
        self._set_expiry(self.w.product, 20)
        self.assertEqual(self._get().context['expiring_soon_count'], 0)
        system_settings(expiry_warning_days=30)
        r = self._get()
        self.assertEqual(r.context['expiring_soon_count'], 1)
        self.assertEqual(r.context['expiry_days'], 30)

    def test_expiry_hidden_when_tracking_disabled(self):
        self._set_expiry(self.w.product, -1)
        system_settings(enable_expiry_tracking=False)
        r = self._get()
        self.assertEqual(r.context['expired_count'], 0)
        self.assertEqual(r.context['expiring_list'], [])
        self.assertNotIn('منتهي الصلاحية', r.content.decode())

    def test_expiry_report_uses_warning_days(self):
        self._set_expiry(self.w.product, 20)
        c = login(self.client, self.w.admin)
        system_settings(expiry_warning_days=10)
        self.assertEqual(len(c.get('/inventory/expiry-report/').context['batches']), 0)
        system_settings(expiry_warning_days=25)
        self.assertEqual(len(c.get('/inventory/expiry-report/').context['batches']), 1)


class OpeningStockExpiryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def _post(self, product, **extra):
        c = login(self.client, self.w.admin)
        data = {'warehouse': self.w.warehouse.id, 'quantity': '12', 'unit_cost': '4'}
        data.update(extra)
        return c.post(f'/inventory/products/{product.pk}/opening-stock/?warehouse={self.w.warehouse.id}', data)

    def _batch(self, product):
        from apps.tenant.inventory.models import InventoryBatch
        return InventoryBatch.objects.get(product=product, warehouse=self.w.warehouse)

    def test_expiry_saved_when_tracking_enabled(self):
        system_settings(enable_expiry_tracking=True)
        page = login(self.client, self.w.admin).get(f'/inventory/products/{self.w.product.pk}/opening-stock/')
        self.assertContains(page, 'name="expiry_date"')
        self._post(self.w.product, expiry_date='2027-05-01')
        self.assertEqual(self._batch(self.w.product).expiry_date, datetime.date(2027, 5, 1))
        # editing again can change it
        self._post(self.w.product, expiry_date='2027-06-01')
        self.assertEqual(self._batch(self.w.product).expiry_date, datetime.date(2027, 6, 1))

    def test_expiry_ignored_when_tracking_disabled(self):
        system_settings(enable_expiry_tracking=False)
        page = login(self.client, self.w.admin).get(f'/inventory/products/{self.w.product.pk}/opening-stock/')
        self.assertNotContains(page, 'name="expiry_date"')
        self._post(self.w.product, expiry_date='2027-05-01')
        self.assertIsNone(self._batch(self.w.product).expiry_date)

    def test_invalid_expiry_rejected_before_saving_stock(self):
        from apps.tenant.inventory.models import InventoryBatch
        system_settings(enable_expiry_tracking=True)
        self._post(self.w.product2, expiry_date='not-a-date')
        self.assertFalse(InventoryBatch.objects.filter(product=self.w.product2).exists())

    def test_api_returns_and_saves_expiry(self):
        system_settings(enable_expiry_tracking=True)
        c = login(self.client, self.w.admin)
        url = f'/inventory/api/products/{self.w.product.pk}/opening-stock/'
        r = c.post(url, {'warehouse_id': self.w.warehouse.id, 'quantity': '5', 'unit_cost': '3',
                         'expiry_date': '2027-01-15'})
        self.assertEqual(r.status_code, 200, r.content)
        data = c.get(url, {'warehouse_id': self.w.warehouse.id}).json()
        self.assertTrue(data['expiry_tracking'])
        self.assertEqual(data['expiry_date'], '2027-01-15')
