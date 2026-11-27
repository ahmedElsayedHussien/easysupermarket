"""
Tests for P1-5: Protection of inventory history, warehouses, branches, and account trees against CASCADE deletion.
- Warehouse with batches/movements cannot be deleted (PROTECT); deletion view deactivates it (is_active=False).
- Branch with warehouses cannot be deleted (PROTECT); deletion view deactivates it.
- Product with batches/movements cannot be deleted (PROTECT).
- Account with children cannot be deleted (PROTECT).
"""
from decimal import Decimal as D

from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import Account
from apps.tenant.core.models import Branch
from apps.tenant.core.tests.factories import (
    assertInventoryMatchesLedger,
    build_world,
    login,
    make_branch,
    make_product,
    make_warehouse,
)
from apps.tenant.inventory.models import InventoryBatch, Product, StockMovement, Warehouse
from apps.tenant.services.opening_stock_service import update_or_create_product_opening_stock


class CascadeProtectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)

    def test_warehouse_with_opening_stock_protected_against_direct_deletion(self):
        """Direct deletion of a warehouse with opening stock raises ProtectedError."""
        wh = make_warehouse(self.w.branch, 'مستودع فرعي جديد', code='WH-SUB-1')
        p = make_product(self.w.category, 'منتج لمستودع فرعي', sale_price=D('50'))
        update_or_create_product_opening_stock(p, wh, D('10'), D('30'), user=self.w.admin)
        assertInventoryMatchesLedger(self)

        # Direct deletion must raise ProtectedError instead of wiping batches and movements
        with self.assertRaises(ProtectedError):
            wh.delete()

        # Data remains intact
        self.assertTrue(Warehouse.objects.filter(pk=wh.pk).exists())
        self.assertTrue(InventoryBatch.objects.filter(warehouse=wh).exists())
        self.assertTrue(StockMovement.objects.filter(warehouse=wh).exists())
        assertInventoryMatchesLedger(self)

    def test_warehouse_delete_view_deactivates_warehouse_when_protected(self):
        """Warehouse delete view sets is_active=False when ProtectedError occurs."""
        wh = make_warehouse(self.w.branch, 'مستودع للحذف', code='WH-DEL-1')
        p = make_product(self.w.category, 'منتج مستودع الحذف', sale_price=D('40'))
        update_or_create_product_opening_stock(p, wh, D('5'), D('20'), user=self.w.admin)

        r = self.client.post(reverse('inventory:warehouse_delete', args=[wh.id]))
        self.assertEqual(r.status_code, 302)

        wh.refresh_from_db()
        self.assertFalse(wh.is_active, "Warehouse should be soft-deactivated instead of deleted")
        self.assertTrue(InventoryBatch.objects.filter(warehouse=wh).exists())
        assertInventoryMatchesLedger(self)

    def test_branch_with_warehouses_protected_against_deletion(self):
        """Branch with warehouses cannot be deleted directly, and view deactivates it."""
        b = make_branch('فرع طنطا', code='TANTA')
        make_warehouse(b, 'مستودع طنطا', code='WH-TANTA')

        # Direct deletion raises ProtectedError
        with self.assertRaises(ProtectedError):
            b.delete()

        # Delete view soft-deactivates branch
        r = self.client.post(reverse('core:branch_delete', args=[b.id]))
        self.assertEqual(r.status_code, 302)
        b.refresh_from_db()
        self.assertFalse(b.is_active, "Branch should be soft-deactivated instead of deleted")

    def test_product_with_batches_protected_against_deletion(self):
        """Product with inventory batches/movements cannot be deleted (ProtectedError)."""
        p = make_product(self.w.category, 'منتج محمي من الحذف', sale_price=D('60'))
        update_or_create_product_opening_stock(p, self.w.warehouse, D('8'), D('25'), user=self.w.admin)

        with self.assertRaises(ProtectedError):
            p.delete()

        self.assertTrue(Product.objects.filter(pk=p.pk).exists())
        assertInventoryMatchesLedger(self)

    def test_account_parent_protected_against_deletion_when_it_has_children(self):
        """An Account that has children in the chart of accounts tree cannot be deleted (ProtectedError)."""
        parent_acc = Account.objects.create(code='9990', name='حساب أب تجريبي', account_type=Account.EXPENSE)
        child_acc = Account.objects.create(code='9991', name='حساب فرعي تجريبي', account_type=Account.EXPENSE, parent=parent_acc)

        with self.assertRaises(ProtectedError):
            parent_acc.delete()

        self.assertTrue(Account.objects.filter(pk=parent_acc.pk).exists())
        self.assertTrue(Account.objects.filter(pk=child_acc.pk).exists())
