"""FIFO engine (apps/tenant/services/fifo_engine.py)."""
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.core.tests.factories import (
    build_world, make_product, make_warehouse, stock_in, system_settings,
)
from apps.tenant.inventory.models import InventoryBatch, StockMovement
from apps.tenant.services.fifo_engine import add_inventory_batch, consume_fifo_batches, transfer_stock


class FifoEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        self.p = make_product(self.w.category, 'FIFO item')
        self.wh = self.w.warehouse

    def test_consume_across_batches_uses_oldest_cost_first(self):
        stock_in(self.p, self.wh, 5, '2.00')
        stock_in(self.p, self.wh, 5, '3.00')
        cons = consume_fifo_batches(self.p, self.wh, D('7'))
        self.assertEqual([c['quantity_consumed'] for c in cons], [D('5'), D('2')])
        self.assertEqual(sum(c['total_cost'] for c in cons), D('16.0000'))  # 5*2 + 2*3
        self.assertEqual(self.p.get_stock(self.wh), D('3'))
        self.assertEqual(StockMovement.objects.filter(product=self.p, movement_type='OUT').count(), 2)

    def test_insufficient_stock_raises_when_negative_disallowed(self):
        system_settings(allow_negative_stock=False)
        stock_in(self.p, self.wh, 2, '1')
        with self.assertRaises(ValueError):
            consume_fifo_batches(self.p, self.wh, D('3'))
        self.assertEqual(self.p.get_stock(self.wh), D('2'))  # nothing consumed

    def test_global_negative_stock_setting_allows_deficit(self):
        system_settings(allow_negative_stock=True)
        stock_in(self.p, self.wh, 2, '4')
        cons = consume_fifo_batches(self.p, self.wh, D('5'))
        self.assertEqual(sum(c['quantity_consumed'] for c in cons), D('5'))
        self.assertEqual(self.p.get_stock(self.wh), D('-3'))
        self.assertTrue(InventoryBatch.objects.filter(product=self.p, batch_number='DEFICIT').exists())

    def test_per_product_allow_negative_flag(self):
        system_settings(allow_negative_stock=False)
        neg = make_product(self.w.category, 'neg ok', allow_negative_stock=True)
        consume_fifo_batches(neg, self.wh, D('4'))
        self.assertEqual(neg.get_stock(self.wh), D('-4'))
        # ... while a normal product is still refused
        with self.assertRaises(ValueError):
            consume_fifo_batches(self.p, self.wh, D('1'))

    def test_deficit_offset_by_next_batch(self):
        system_settings(allow_negative_stock=True)
        consume_fifo_batches(self.p, self.wh, D('3'))
        batch = add_inventory_batch(self.p, self.wh, D('10'), D('2'))
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D('7'))
        deficit = InventoryBatch.objects.get(product=self.p, batch_number='DEFICIT')
        self.assertEqual(deficit.quantity_remaining, D('0'))
        self.assertEqual(self.p.get_stock(self.wh), D('7'))

    def test_negative_quantity_rejected(self):
        with self.assertRaises(ValueError):
            consume_fifo_batches(self.p, self.wh, D('-1'))
        with self.assertRaises(ValueError):
            add_inventory_batch(self.p, self.wh, D('0'), D('1'))

    def test_transfer_moves_stock_preserving_cost(self):
        dest = make_warehouse(self.w.branch, 'dest')
        stock_in(self.p, self.wh, 4, '2')
        stock_in(self.p, self.wh, 4, '5')
        transfer_stock(self.p, self.wh, dest, D('6'))
        self.assertEqual(self.p.get_stock(self.wh), D('2'))
        self.assertEqual(self.p.get_stock(dest), D('6'))
        dest_costs = sorted((b.quantity_remaining, b.unit_cost) for b in InventoryBatch.objects.filter(warehouse=dest))
        self.assertEqual(dest_costs, [(D('2'), D('5')), (D('4'), D('2'))])

    def test_transfer_is_all_or_nothing(self):
        system_settings(allow_negative_stock=True)  # transfers never go negative anyway
        dest = make_warehouse(self.w.branch, 'dest2')
        stock_in(self.p, self.wh, 3, '2')
        with self.assertRaises(ValueError):
            transfer_stock(self.p, self.wh, dest, D('5'))
        self.assertEqual(self.p.get_stock(self.wh), D('3'))
        self.assertEqual(self.p.get_stock(dest), D('0'))
        self.assertFalse(StockMovement.objects.filter(product=self.p, movement_type__startswith='TRANSFER').exists())

    def test_transfer_to_same_warehouse_rejected(self):
        stock_in(self.p, self.wh, 3, '2')
        with self.assertRaises(ValueError):
            transfer_stock(self.p, self.wh, self.wh, D('1'))

    def test_last_purchase_price_only_updated_for_in(self):
        add_inventory_batch(self.p, self.wh, D('5'), D('7.5'))
        self.p.refresh_from_db()
        self.assertEqual(self.p.last_purchase_price, D('7.5'))
        add_inventory_batch(self.p, self.wh, D('1'), D('99'), movement_type='RETURN_IN')
        add_inventory_batch(self.p, self.wh, D('1'), D('88'), movement_type='ADJUSTMENT')
        self.p.refresh_from_db()
        self.assertEqual(self.p.last_purchase_price, D('7.5'))

    def test_service_products_have_no_stock_effect(self):
        svc = make_product(self.w.category, 'خدمة', product_type='SERVICE')
        self.assertEqual(consume_fifo_batches(svc, self.wh, D('3')), [])
        self.assertIsNone(add_inventory_batch(svc, self.wh, D('3'), D('1')))
