"""
Tests for P1-4: Reconciliation of negative stock deficit cost differences upon offset.
When negative stock is consumed at an estimated unit cost, and a subsequent purchase/stock-in
arrives at a different actual cost, _offset_deficits must post an adjustment entry
(COGS vs 1140) for the difference so that Inventory Batches equal Account 1140 in the ledger.
"""
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.core.tests.factories import (
    assertInventoryMatchesLedger,
    build_world,
    make_invoice,
    make_product,
    system_settings,
)
from apps.tenant.inventory.models import InventoryBatch
from apps.tenant.accounting.models import JournalEntry


class DeficitCostReconciliationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        system_settings(allow_negative_stock=True)
        # Product with estimated purchase price = 50
        cls.p = make_product(cls.w.category, 'منتج عجز سالب', sale_price=D('100'), tax_rate=D('0'),
                             last_purchase_price=D('50'))

    def test_offset_deficits_posts_cogs_adjustment_when_actual_cost_higher(self):
        """When purchase cost (70) > estimated cost (50), diff (+20/unit) debits COGS and credits 1140."""
        # 1. Sell 5 units at negative stock (estimated cost 50, COGS=250, 1140=-250)
        sale = make_invoice(self.w, invoice_type='SALE', lines=[(self.p, 5, 100)], partner=self.w.customer)
        self.assertEqual(self.p.get_stock(self.w.warehouse), D('-5'))
        assertInventoryMatchesLedger(self)

        # 2. Purchase 10 units at actual cost 70 (1140 debited 700)
        purchase = make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.p, 10, 70)], partner=self.w.supplier)
        self.assertEqual(self.p.get_stock(self.w.warehouse), D('5'))

        # Check deficit was filled
        deficit = InventoryBatch.objects.get(product=self.p, batch_number='DEFICIT')
        self.assertEqual(deficit.quantity_remaining, D('0'))

        # Verify an adjustment entry was posted for the difference: (70 - 50) * 5 = 100 EGP
        adj_entry = JournalEntry.objects.filter(
            reference__startswith='ADJ-DEF-', status=JournalEntry.POSTED
        ).first()
        self.assertIsNotNone(adj_entry, "Expected an adjustment entry for deficit cost variance")

        # Crucial: Inventory Batches value MUST match Ledger 1140 exactly
        assertInventoryMatchesLedger(self)

    def test_offset_deficits_posts_cogs_adjustment_when_actual_cost_lower(self):
        """When purchase cost (35) < estimated cost (50), diff (-15/unit) debits 1140 and credits COGS."""
        p2 = make_product(self.w.category, 'منتج عجز رخيص', sale_price=D('100'), tax_rate=D('0'),
                          last_purchase_price=D('50'))

        # 1. Sell 4 units at negative stock (estimated 50)
        make_invoice(self.w, invoice_type='SALE', lines=[(p2, 4, 100)], partner=self.w.customer)
        assertInventoryMatchesLedger(self)

        # 2. Purchase 10 units at actual cost 35
        make_invoice(self.w, invoice_type='PURCHASE', lines=[(p2, 10, 35)], partner=self.w.supplier)

        # Crucial: Inventory Batches value MUST match Ledger 1140 exactly
        assertInventoryMatchesLedger(self)

    def test_failed_adjustment_entry_fails_the_purchase(self):
        """The ADJ-DEF entry can't be posted (dated today, period closed up to today): the purchase is
        refused as a whole — never saved with the stock and 1140 apart."""
        import datetime
        from apps.tenant.invoicing.models import Invoice
        p3 = make_product(self.w.category, 'منتج عجز مقفول', sale_price=D('100'), tax_rate=D('0'),
                          last_purchase_price=D('50'))
        make_invoice(self.w, invoice_type='SALE', lines=[(p3, 3, 100)], partner=self.w.customer)
        today = datetime.date.today()
        system_settings(accounting_lock_date=today)
        purchase = make_invoice(self.w, invoice_type='PURCHASE', lines=[(p3, 10, 70)], partner=self.w.supplier,
                                confirm=False, date=today + datetime.timedelta(days=1))
        from apps.tenant.services.invoice_service import confirm_invoice
        with self.assertRaises(Exception):
            confirm_invoice(purchase.id)
        system_settings(accounting_lock_date=None)
        self.assertEqual(Invoice.objects.get(pk=purchase.pk).status, Invoice.DRAFT)
        self.assertEqual(p3.get_stock(self.w.warehouse), D('-3'))          # nothing of the purchase stayed
        assertInventoryMatchesLedger(self)
