"""
Test P1-1: Inventory vs Ledger 1140 reconciliation.
- inventory_ledger_check management command
- assertInventoryMatchesLedger helper
- Accounting dashboard warning when inventory differs from ledger 1140
"""
from decimal import Decimal as D
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import Account, JournalEntry, JournalItem
from apps.tenant.core.tests.factories import (
    assertInventoryMatchesLedger,
    build_world,
    login,
    make_product,
)
from apps.tenant.inventory.models import InventoryBatch, Warehouse


class InventoryLedgerCheckTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.p = make_product(cls.w.category, 'منتج اختبار المطابقة', sale_price=D('100'))

    def setUp(self):
        login(self.client, self.w.admin)

    def test_assert_inventory_matches_ledger_success(self):
        """When batches equal 1140 ledger, helper passes cleanly."""
        assertInventoryMatchesLedger(self)

    def test_assert_inventory_matches_ledger_failure_on_discrepancy(self):
        """When batches diverge from 1140 ledger, helper raises AssertionError."""
        # Create an orphaned batch with no corresponding accounting entry
        InventoryBatch.objects.create(
            product=self.p,
            warehouse=self.w.warehouse,
            quantity_original=D('10'),
            quantity_remaining=D('10'),
            unit_cost=D('50'),
        )
        with self.assertRaises(AssertionError) as ctx:
            assertInventoryMatchesLedger(self)
        self.assertIn("فارق بين تقييم المخزون وحساب 1140", str(ctx.exception))

    def test_management_command_output(self):
        """inventory_ledger_check command prints comparison and detects mismatch."""
        out = StringIO()
        call_command('inventory_ledger_check', stdout=out)
        self.assertIn("مطابقة", out.getvalue())

        # Now introduce a mismatch
        InventoryBatch.objects.create(
            product=self.p,
            warehouse=self.w.warehouse,
            quantity_original=D('5'),
            quantity_remaining=D('5'),
            unit_cost=D('40'),
        )
        out_err = StringIO()
        call_command('inventory_ledger_check', stdout=out_err)
        self.assertIn("فارق", out_err.getvalue())

    def test_dashboard_shows_warning_when_discrepancy_exists(self):
        """Accounting dashboard shows an alert if inventory value differs from 1140 by > 0.01."""
        # Clean state -> no alert
        r = self.client.get(reverse('accounting:accounting_dashboard'))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("فارق بين المخزون وحساب 1140", r.content.decode('utf-8'))

        # Introduce a discrepancy of 200 EGP
        InventoryBatch.objects.create(
            product=self.p,
            warehouse=self.w.warehouse,
            quantity_original=D('4'),
            quantity_remaining=D('4'),
            unit_cost=D('50'),
        )
        r = self.client.get(reverse('accounting:accounting_dashboard'))
        self.assertEqual(r.status_code, 200)
        self.assertIn("فارق بين المخزون وحساب 1140", r.content.decode('utf-8'))
