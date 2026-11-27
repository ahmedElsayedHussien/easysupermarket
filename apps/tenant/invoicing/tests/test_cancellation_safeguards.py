"""
Tests for P1-2: Inventory cancellation safeguards and cost reconciliation.
- Reject purchase invoice cancellation if any part of its batch was consumed/sold.
- Reject sales return cancellation if any part of its return batch was resold.
- Return purchase cancellation restores stock at the exact line return price with movement CANCEL_IN.
- Ledger 1140 matches inventory batches across all operations (assertInventoryMatchesLedger).
"""
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.core.tests.factories import (
    assertInventoryMatchesLedger,
    build_world,
    make_invoice,
    make_product,
)
from apps.tenant.inventory.models import InventoryBatch, StockMovement
from apps.tenant.invoicing.models import Invoice, InvoiceLine
from apps.tenant.services.invoice_service import cancel_invoice, confirm_invoice


class CancellationSafeguardsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.p = make_product(cls.w.category, 'منتج فحص الإلغاءات', sale_price=D('100'), tax_rate=D('0'))

    def test_cancel_purchase_with_consumed_batch_is_rejected(self):
        """Cancelling a purchase invoice whose batch was partially consumed is rejected."""
        # 1. Purchase 10 units at 50
        purchase = make_invoice(
            self.w, invoice_type='PURCHASE', lines=[(self.p, 10, 50)], partner=self.w.supplier
        )
        assertInventoryMatchesLedger(self)

        # 2. Sell 4 units
        sale = make_invoice(
            self.w, invoice_type='SALE', lines=[(self.p, 4, 100)], partner=self.w.customer
        )
        assertInventoryMatchesLedger(self)

        # 3. Attempting to cancel purchase must be rejected
        with self.assertRaises(ValueError) as ctx:
            cancel_invoice(purchase.id, self.w.admin)
        self.assertIn('مرتجع مشتريات', str(ctx.exception))
        self.assertIn('بيع أو استهلاك جزء', str(ctx.exception))

        # Ledger and inventory still match
        assertInventoryMatchesLedger(self)

    def test_cancel_purchase_unconsumed_succeeds_and_matches_ledger(self):
        """Cancelling a purchase invoice whose batch is intact succeeds cleanly and matches 1140."""
        purchase = make_invoice(
            self.w, invoice_type='PURCHASE', lines=[(self.p, 10, 50)], partner=self.w.supplier
        )
        assertInventoryMatchesLedger(self)

        cancel_invoice(purchase.id, self.w.admin)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, Invoice.CANCELLED)
        self.assertEqual(self.p.get_stock(self.w.warehouse), D('0'))
        assertInventoryMatchesLedger(self)

    def test_cancel_purchase_keeps_batch_at_zero_and_protects_it(self):
        """P1-3: Batch is never deleted on cancellation, stays at quantity 0, and PROTECT prevents deletion."""
        from django.db.models import ProtectedError
        purchase = make_invoice(
            self.w, invoice_type='PURCHASE', lines=[(self.p, 10, 50)], partner=self.w.supplier
        )
        line = purchase.lines.get()
        batch = InventoryBatch.objects.get(source_invoice_line=line)
        self.assertEqual(batch.quantity_remaining, D('10'))

        # Cancel purchase
        cancel_invoice(purchase.id, self.w.admin)

        # 1. Batch must NOT be deleted from DB
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D('0'))

        # 2. Movement must still be linked to this batch (not NULL)
        mv = StockMovement.objects.filter(batch=batch, movement_type='OUT').first()
        self.assertIsNotNone(mv)
        self.assertEqual(mv.batch_id, batch.id)

        # 3. StockMovement.batch must have on_delete=PROTECT: deleting batch with movements raises ProtectedError
        with self.assertRaises(ProtectedError):
            batch.delete()

        assertInventoryMatchesLedger(self)

    def test_cancel_return_sale_with_resold_stock_is_rejected(self):
        """Cancelling a customer sales return whose stock was resold is rejected."""
        # 1. Customer returns 5 units
        ret_sale = make_invoice(
            self.w, invoice_type='RETURN_SALE', lines=[(self.p, 5, 100)], partner=self.w.customer
        )
        assertInventoryMatchesLedger(self)

        # 2. Resell 2 units
        sale = make_invoice(
            self.w, invoice_type='SALE', lines=[(self.p, 2, 100)], partner=self.w.customer
        )
        assertInventoryMatchesLedger(self)

        # 3. Attempting to cancel the sales return must be rejected
        with self.assertRaises(ValueError) as ctx:
            cancel_invoice(ret_sale.id, self.w.admin)
        self.assertIn('إعادة بيع أو صرف جزء', str(ctx.exception))

        assertInventoryMatchesLedger(self)

    def test_cancel_return_purchase_uses_line_cost_and_cancel_in(self):
        """Cancelling a return purchase restores stock at the exact return cost with CANCEL_IN."""
        # 1. Initial purchase: 10 units at 50
        purchase = make_invoice(
            self.w, invoice_type='PURCHASE', lines=[(self.p, 10, 50)], partner=self.w.supplier
        )
        assertInventoryMatchesLedger(self)

        # 2. Return 4 units to supplier at 50
        ret_purchase = make_invoice(
            self.w, invoice_type='RETURN_PURCHASE', lines=[(self.p, 4, 50)], partner=self.w.supplier
        )
        assertInventoryMatchesLedger(self)

        # 3. Simulate cost changes on product (e.g. last purchase price or average cost altered)
        self.p.last_purchase_price = D('99')
        self.p.save(update_fields=['last_purchase_price'])

        # 4. Cancel the return purchase
        cancel_invoice(ret_purchase.id, self.w.admin)
        ret_purchase.refresh_from_db()
        self.assertEqual(ret_purchase.status, Invoice.CANCELLED)

        # Check movement type and cost
        mv = StockMovement.objects.filter(
            product=self.p, reference=f'إلغاء مرتجع مشتريات {ret_purchase.invoice_number}'
        ).first()
        self.assertIsNotNone(mv)
        self.assertEqual(mv.movement_type, 'CANCEL_IN')
        self.assertEqual(mv.unit_cost, D('50'))

        # Crucial check: 1140 matches inventory batches perfectly
        assertInventoryMatchesLedger(self)
