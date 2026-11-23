"""Non-admin users only see documents of their own branch."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import Expense, Voucher
from apps.tenant.core.tests.factories import (
    account, build_world, login, make_branch, make_treasury, make_user,
)

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
        login(self.client, self.w.admin)
        resp = self.client.get(reverse('accounting:expense_detail', args=[self.expense_b.pk]))
        self.assertEqual(resp.status_code, 200)
