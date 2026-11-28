"""Vouchers, expenses, opening balances and accounting reports."""
import json
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import Expense, JournalEntry, Voucher
from apps.tenant.core.tests.factories import (
    account, account_net, build_world, entry_totals, ledger_totals, login, make_bank_account,
    make_ewallet, make_invoice, make_treasury, stock_in,
)


class VoucherTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        self.v = Voucher.objects.create(
            voucher_type=Voucher.RECEIPT, amount=D('150'), partner=self.w.customer,
            account=self.w.customer.receivable_account, payment_method=Voucher.CASH, treasury=self.w.treasury,
        )

    def test_confirm_posts_balanced_entry_on_treasury_sub_account(self):
        self.v.confirm_voucher()
        self.v.refresh_from_db()
        self.assertEqual(self.v.status, Voucher.POSTED)
        je = self.v.journal_entry
        self.assertEqual(je.status, JournalEntry.POSTED)
        dr, cr = entry_totals(je)
        self.assertEqual(dr, cr)
        self.assertEqual(account_net(je, f'1110-{self.w.treasury.id}'), D('150'))
        self.assertEqual(account_net(je, self.w.customer.receivable_account.code), D('-150'))

    def test_double_confirm_refused(self):
        self.v.confirm_voucher()
        stale = Voucher.objects.get(pk=self.v.pk)
        stale.status = Voucher.DRAFT  # stale in-memory copy (double click)
        with self.assertRaises(ValidationError):
            stale.confirm_voucher()
        self.assertEqual(JournalEntry.objects.filter(reference__startswith=self.v.voucher_number).count(), 1)

    def test_cancel_posts_reversal(self):
        self.v.confirm_voucher()
        original = Voucher.objects.get(pk=self.v.pk).journal_entry
        self.v.cancel_voucher(user=self.w.admin)
        self.v.refresh_from_db()
        self.assertEqual(self.v.status, Voucher.CANCELLED)
        self.assertIsNone(self.v.journal_entry_id)
        original.refresh_from_db()
        self.assertEqual(original.status, JournalEntry.POSTED)
        rev = JournalEntry.objects.get(reversal_of=original)
        code = f'1110-{self.w.treasury.id}'
        self.assertEqual(account_net(original, code) + account_net(rev, code), D('0'))
        with self.assertRaises(ValidationError):
            self.v.cancel_voucher()

    def test_confirm_view_twice_posts_once(self):
        login(self.client, self.w.admin)
        url = reverse('accounting:voucher_confirm', args=[self.v.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(JournalEntry.objects.filter(reference__startswith=self.v.voucher_number).count(), 1)


class ExpenseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_expense_vat_goes_to_input_vat_1190(self):
        e = Expense.objects.create(branch=self.w.branch, expense_account=account('5210'),
                                   payment_account=self.w.treasury.account, amount=D('100'),
                                   vat_percent=D('14'), description='كهرباء')
        e.confirm_expense()
        e.refresh_from_db()
        je = e.journal_entry
        dr, cr = entry_totals(je)
        self.assertEqual(dr, cr)
        self.assertEqual(account_net(je, '1190'), D('14'))
        self.assertEqual(account_net(je, self.w.treasury.account.code), D('-114'))
        with self.assertRaises(ValidationError):
            Expense.objects.get(pk=e.pk).confirm_expense()


class OpeningBalanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def _check(self, obj, prefix, amount):
        je = JournalEntry.objects.get(reference=f'{prefix}-{obj.pk}')
        self.assertEqual(je.status, JournalEntry.POSTED)
        dr, cr = entry_totals(je)
        self.assertEqual(dr, cr)
        self.assertEqual(dr, amount)
        self.assertEqual(account_net(je, obj.account.code), amount)
        self.assertEqual(account_net(je, '3999'), -amount)
        self.assertEqual(obj.current_balance, amount)

    def test_treasury_opening_balance(self):
        t = make_treasury(self.w.branch, 'خزينة افتتاحية', opening_balance=D('1000'))
        self.assertEqual(t.account.code, f'1110-{t.pk}')
        self._check(t, 'OPENING-TR', D('1000'))

    def test_bank_opening_balance(self):
        b = make_bank_account('بنك مصر', opening_balance=D('2500'))
        self.assertEqual(b.account.code, f'1120-{b.pk}')
        self._check(b, 'OPENING-BK', D('2500'))

    def test_ewallet_opening_balance(self):
        e = make_ewallet('فودافون كاش', opening_balance=D('300'))
        self._check(e, 'OPENING-EW', D('300'))

    def test_zero_opening_balance_books_nothing(self):
        t = make_treasury(self.w.branch, 'خزينة صفر')
        self.assertFalse(JournalEntry.objects.filter(reference=f'OPENING-TR-{t.pk}').exists())


class AccountingReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        w = self.w
        make_invoice(w, 'PURCHASE', lines=[(w.product, 20, 5, 14)], payment_type='CREDIT')
        make_treasury(w.branch, 'خزينة 2', opening_balance=D('500'))
        self.sale = make_invoice(w, 'SALE', lines=[(w.product, 10, 10, 14), (w.product, 2, 10, 14)],
                                 payment_type='CREDIT')
        e = Expense.objects.create(branch=w.branch, expense_account=account('5210'),
                                   payment_account=w.treasury.account, amount=D('40'), description='x')
        e.confirm_expense()
        v = Voucher.objects.create(voucher_type=Voucher.RECEIPT, amount=D('50'), partner=w.customer,
                                   account=w.customer.receivable_account, payment_method=Voucher.CASH,
                                   treasury=w.treasury)
        v.confirm_voucher()
        login(self.client, w.admin)
        line = self.sale.lines.order_by('id').last()
        resp = self.client.post(reverse('invoicing:process_invoice_return', args=[self.sale.id]),
                                data=json.dumps({'items': [{'line_id': line.id, 'quantity': '2'}],
                                                 'payment_type': 'CREDIT'}),
                                content_type='application/json')
        self.assertEqual(resp.json()['status'], 'success', resp.json())

    def test_ledger_balanced(self):
        dr, cr = ledger_totals()
        self.assertEqual(dr, cr)
        for je in JournalEntry.objects.filter(status='POSTED'):
            self.assertTrue(je.is_balanced(), je.reference)

    def test_trial_balance_view_balanced(self):
        resp = self.client.get(reverse('accounting:trial_balance'))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['is_balanced'])
        self.assertEqual(resp.context['total_debit'], resp.context['total_credit'])
        self.assertGreater(resp.context['total_debit'], 0)

    def test_income_statement_net_sales_subtracts_returns(self):
        resp = self.client.get(reverse('accounting:income_statement'))
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context
        self.assertEqual(ctx['total_sales'], D('120.00'))
        self.assertEqual(ctx['total_contra_revenue'], D('20.00'))
        self.assertEqual(ctx['net_sales'], D('100.00'))
        # COGS: 12 sold @5 - 2 returned @5 = 50
        self.assertEqual(ctx['total_cogs'], D('50.00'))
        self.assertEqual(ctx['net_income'], D('100.00') - D('50.00') - D('40.00'))

    def test_balance_sheet_balances(self):
        resp = self.client.get(reverse('accounting:balance_sheet'))
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context
        self.assertEqual(ctx['total_assets'], ctx['total_liabilities_and_equity'])
