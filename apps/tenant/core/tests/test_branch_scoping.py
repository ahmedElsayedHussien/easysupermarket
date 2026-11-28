"""Non-admin users only see documents of their own branch."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import BankAccount, EWallet, Expense, Treasury, Voucher
from apps.tenant.core.tests.factories import (
    account, add_perms, build_world, login, make_bank_account, make_branch,
    make_ewallet, make_treasury, make_user, make_warehouse,
)
from apps.tenant.inventory.models import Warehouse
from apps.tenant.maintenance.models import MaintenanceTicket

VIEW_PERMS = ('accounting.view_expense', 'accounting.view_voucher')


class BranchScopingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch_b = make_branch('فرع ب', code='BR-B')
        cls.treasury_b = make_treasury(cls.branch_b, 'خزينة ب')
        cls.user_a = make_user('clerk_a', role='ACCOUNTANT', branch=cls.w.branch, perms=VIEW_PERMS)
        cls.user_nobranch = make_user('clerk_none', role='ACCOUNTANT', branch=None, perms=VIEW_PERMS)
        cls.expense_b = Expense.objects.create(
            branch=cls.branch_b, expense_account=account('5210'),
            payment_account=cls.treasury_b.account, amount=D('100'), description='مصروف فرع ب',
        )
        cls.expense_a = Expense.objects.create(
            branch=cls.w.branch, expense_account=account('5210'),
            payment_account=cls.w.treasury.account, amount=D('50'), description='مصروف فرع أ',
        )
        cls.voucher_b = Voucher.objects.create(
            voucher_type=Voucher.RECEIPT, amount=D('70'), account=account('4200'),
            payment_method=Voucher.CASH, treasury=cls.treasury_b,
        )

    def test_voucher_branch_attributed_from_treasury(self):
        self.assertEqual(self.voucher_b.branch_id, self.branch_b.id)

    def test_branch_a_user_cannot_see_branch_b_expense(self):
        login(self.client, self.user_a)
        resp = self.client.get(reverse('accounting:expense_detail', args=[self.expense_b.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_branch_a_user_sees_own_expense(self):
        login(self.client, self.user_a)
        resp = self.client.get(reverse('accounting:expense_detail', args=[self.expense_a.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_branch_a_user_cannot_see_branch_b_voucher(self):
        login(self.client, self.user_a)
        resp = self.client.get(reverse('accounting:voucher_detail', args=[self.voucher_b.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_branch_a_expense_list_excludes_branch_b(self):
        login(self.client, self.user_a)
        resp = self.client.get(reverse('accounting:expense_list'))
        self.assertEqual(resp.status_code, 200)
        ids = {e.pk for e in resp.context['expenses']}
        self.assertEqual(ids, {self.expense_a.pk})

    def test_user_without_branch_gets_empty_lists(self):
        login(self.client, self.user_nobranch)
        resp = self.client.get(reverse('accounting:expense_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context['expenses']), [])
        resp = self.client.get(reverse('accounting:receipt_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context['vouchers']), [])

    def test_admin_sees_all_branches(self):
        # Admins follow the header branch: "all branches" shows everything...
        login(self.client, self.w.admin)
        self.client.get('/?branch=all')
        resp = self.client.get(reverse('accounting:expense_detail', args=[self.expense_b.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_admin_on_one_branch_sees_only_that_branch(self):
        # ...while a specific header branch hides other branches' records.
        login(self.client, self.w.admin)
        self.client.get(f'/?branch={self.w.branch.id}')
        resp = self.client.get(reverse('accounting:expense_detail', args=[self.expense_b.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_deliver_ticket_rejects_other_branch_treasury_and_rep_cash_box(self):
        from apps.tenant.invoicing import van_services as van
        from apps.tenant.invoicing.models import SalesRep

        rep_user = make_user('rep_branch_a', role='SALES_REP', branch=self.w.branch)
        rep = SalesRep.objects.create(employee=rep_user.employee_profile, source_warehouse=self.w.warehouse)
        van.ensure_rep_setup(rep, create_van=True, create_treasury=True)
        rep.refresh_from_db()

        ticket = MaintenanceTicket.objects.create(
            branch=self.w.branch, customer=self.w.customer,
            device_model='Laptop A', issue_description='power', labor_cost=D('100'),
        )
        add_perms(self.user_a, 'maintenance.change_maintenanceticket')
        login(self.client, self.user_a)

        # Other branch's treasury must be rejected
        resp = self.client.post(
            reverse('maintenance:deliver_ticket', args=[ticket.pk]),
            {'payment_method': 'CASH', 'treasury_id': self.treasury_b.pk},
        )
        self.assertIn(resp.status_code, (400, 404))
        ticket.refresh_from_db()
        self.assertNotEqual(ticket.status, MaintenanceTicket.STATUS_DELIVERED)

        # Same branch sales rep cash box must also be rejected
        resp = self.client.post(
            reverse('maintenance:deliver_ticket', args=[ticket.pk]),
            {'payment_method': 'CASH', 'treasury_id': rep.treasury_id},
        )
        self.assertIn(resp.status_code, (400, 404))
        ticket.refresh_from_db()
        self.assertNotEqual(ticket.status, MaintenanceTicket.STATUS_DELIVERED)

        # Own branch business treasury succeeds
        resp = self.client.post(
            reverse('maintenance:deliver_ticket', args=[ticket.pk]),
            {'payment_method': 'CASH', 'treasury_id': self.w.treasury.pk},
        )
        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, MaintenanceTicket.STATUS_DELIVERED)

    def test_get_warehouse_products_requires_permission_and_branch_scope(self):
        warehouse_b = make_warehouse(self.branch_b, 'مخزن ب', code='WH-B')
        no_perm_user = make_user('branch_a_noperms', role='CASHIER', branch=self.w.branch)
        login(self.client, no_perm_user)

        resp = self.client.get(reverse('maintenance:get_warehouse_products', args=[self.w.warehouse.pk]))
        self.assertNotEqual(resp.status_code, 200)

        add_perms(self.user_a, 'maintenance.view_maintenanceticket')
        login(self.client, self.user_a)

        resp_b = self.client.get(reverse('maintenance:get_warehouse_products', args=[warehouse_b.pk]))
        self.assertEqual(resp_b.status_code, 404)

        resp_a = self.client.get(reverse('maintenance:get_warehouse_products', args=[self.w.warehouse.pk]))
        self.assertEqual(resp_a.status_code, 200)

    def test_unscoped_get_object_or_404_endpoints_enforce_branch_scope(self):
        warehouse_b = make_warehouse(self.branch_b, 'مخزن حذف ب', code='WH-DEL-B')
        bank = make_bank_account('بنك الشركة')
        wallet = make_ewallet('محفظة الشركة')
        add_perms(
            self.user_a,
            'accounting.delete_treasury',
            'accounting.delete_bankaccount',
            'accounting.delete_ewallet',
            'inventory.delete_warehouse',
            'inventory.add_stockadjustment',
        )
        login(self.client, self.user_a)

        resp_tr = self.client.post(reverse('accounting:treasury_delete', args=[self.treasury_b.pk]))
        self.assertEqual(resp_tr.status_code, 404)
        self.assertTrue(Treasury.objects.filter(pk=self.treasury_b.pk).exists())

        resp_bk = self.client.post(reverse('accounting:bank_delete', args=[bank.pk]))
        self.assertEqual(resp_bk.status_code, 404)
        self.assertTrue(BankAccount.objects.filter(pk=bank.pk).exists())

        resp_ew = self.client.post(reverse('accounting:ewallet_delete', args=[wallet.pk]))
        self.assertEqual(resp_ew.status_code, 404)
        self.assertTrue(EWallet.objects.filter(pk=wallet.pk).exists())

        resp_wh = self.client.post(reverse('inventory:warehouse_delete', args=[warehouse_b.pk]))
        self.assertEqual(resp_wh.status_code, 404)
        self.assertTrue(Warehouse.objects.get(pk=warehouse_b.pk).is_active)

        resp_adj = self.client.post(
            reverse('inventory:adjustment_create'),
            {
                'warehouse': warehouse_b.pk,
                'adjustment_type': 'IN',
                'date': '2026-11-28',
                'product_id[]': [self.w.product.pk],
                'quantity[]': ['1'],
                'unit_cost[]': ['5'],
            },
        )
        self.assertEqual(resp_adj.status_code, 404)
