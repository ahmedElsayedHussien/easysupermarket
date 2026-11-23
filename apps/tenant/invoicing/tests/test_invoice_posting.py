"""confirm_invoice / cancel_invoice / returns: stock and ledger correctness."""
import json
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import JournalEntry
from apps.tenant.core.tests.factories import (
    account_net, build_world, entry_totals, login, make_invoice, stock_in,
)
from apps.tenant.invoicing.models import Invoice, InvoiceLine
from apps.tenant.services.invoice_service import cancel_invoice, confirm_invoice
from apps.tenant.services.journal_service import reverse_journal_entry


class SaleInvoiceConfirmTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 10, '6')
        # 3 x 10 + 14% VAT = 34.20 ; COGS = 3 x 6 = 18
        self.inv = make_invoice(self.w, 'SALE', lines=[(self.w.product, 3, 10, 14)], payment_type='CASH')
        self.je = self.inv.journal_entry

    def test_posted_and_stock_decremented(self):
        self.assertEqual(self.inv.status, Invoice.POSTED)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('7'))

    def test_journal_entry_balanced(self):
        self.assertEqual(self.je.status, JournalEntry.POSTED)
        dr, cr = entry_totals(self.je)
        self.assertEqual(dr, cr)
        self.assertEqual(self.inv.total_amount, D('34.2000'))

    def test_vat_credited_to_output_vat_2130(self):
        self.assertEqual(account_net(self.je, '2130'), D('-4.2'))
        self.assertEqual(account_net(self.je, '1190'), D('0'))

    def test_revenue_and_cogs_posted(self):
        self.assertEqual(account_net(self.je, '4100'), D('-30'))
        self.assertEqual(account_net(self.je, '5100'), D('18'))
        self.assertEqual(account_net(self.je, '1140'), D('-18'))
        self.assertEqual(self.inv.lines.get().cogs_amount, D('18'))

    def test_cash_goes_to_treasury_sub_account_not_parent(self):
        sub = f'1110-{self.w.treasury.id}'
        self.assertEqual(self.w.treasury.account.code, sub)
        self.assertEqual(account_net(self.je, sub), D('34.2'))
        self.assertEqual(account_net(self.je, '1110'), D('0'))

    def test_cash_without_explicit_treasury_uses_branch_single_treasury(self):
        inv = make_invoice(self.w, 'SALE', lines=[(self.w.product, 1, 10)], payment_type='CASH', treasury=None)
        self.assertEqual(account_net(inv.journal_entry, f'1110-{self.w.treasury.id}'), D('10'))
        self.assertEqual(account_net(inv.journal_entry, '1110'), D('0'))

    def test_credit_sale_goes_to_customer_receivable(self):
        inv = make_invoice(self.w, 'SALE', lines=[(self.w.product, 1, 10)], payment_type='CREDIT')
        self.assertEqual(account_net(inv.journal_entry, f'1130-{self.w.customer.id}'), D('10'))

    def test_confirm_twice_refused(self):
        with self.assertRaises(ValueError):
            confirm_invoice(self.inv.id)

    def test_insufficient_stock_rolls_back(self):
        inv = make_invoice(self.w, 'SALE', lines=[(self.w.product, 50, 10)], confirm=False)
        with self.assertRaises(ValueError):
            confirm_invoice(inv.id)
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invoice.DRAFT)
        self.assertIsNone(inv.journal_entry_id)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('7'))


class PurchaseInvoiceConfirmTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_credit_purchase(self):
        # 10 x 5 + 14% VAT = 57
        inv = make_invoice(self.w, 'PURCHASE', lines=[(self.w.product, 10, 5, 14)], payment_type='CREDIT')
        je = inv.journal_entry
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('10'))
        dr, cr = entry_totals(je)
        self.assertEqual(dr, cr)
        self.assertEqual(account_net(je, '1140'), D('50'))
        self.assertEqual(account_net(je, '1190'), D('7'))      # input VAT
        self.assertEqual(account_net(je, '2130'), D('0'))      # never on output VAT
        self.assertEqual(account_net(je, f'2110-{self.w.supplier.id}'), D('-57'))
        self.w.product.refresh_from_db()
        self.assertEqual(self.w.product.last_purchase_price, D('5'))

    def test_cash_purchase_credits_treasury_sub_account(self):
        inv = make_invoice(self.w, 'PURCHASE', lines=[(self.w.product, 2, 5)], payment_type='CASH')
        self.assertEqual(account_net(inv.journal_entry, f'1110-{self.w.treasury.id}'), D('-10'))


class ReturnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 20, '4')
        # Two lines of the SAME product: the return must hit the right one.
        self.sale = make_invoice(self.w, 'SALE', lines=[(self.w.product, 5, 10), (self.w.product, 3, 10)],
                                 payment_type='CREDIT')
        self.line_a, self.line_b = self.sale.lines.order_by('id')
        login(self.client, self.w.admin)

    def _return(self, line, qty, payment_type='CREDIT'):
        resp = self.client.post(
            reverse('invoicing:process_invoice_return', args=[self.sale.id]),
            data=json.dumps({'items': [{'line_id': line.id, 'quantity': str(qty)}], 'payment_type': payment_type}),
            content_type='application/json',
        )
        return resp.json()

    def test_cannot_return_more_than_sold(self):
        data = self._return(self.line_b, 4)
        self.assertEqual(data['status'], 'error')
        self.assertFalse(Invoice.objects.filter(invoice_type=Invoice.RETURN_SALE).exists())
        self.line_b.refresh_from_db()
        self.assertEqual(self.line_b.returned_quantity, D('0'))

    def test_cannot_exceed_after_partial_return(self):
        self.assertEqual(self._return(self.line_b, 2)['status'], 'success')
        self.assertEqual(self._return(self.line_b, 2)['status'], 'error')
        self.line_b.refresh_from_db()
        self.assertEqual(self.line_b.returned_quantity, D('2'))

    def test_return_sets_original_line_and_restocks(self):
        data = self._return(self.line_b, 2)
        self.assertEqual(data['status'], 'success')
        ret = Invoice.objects.get(pk=data['invoice_id'])
        self.assertEqual(ret.status, Invoice.POSTED)
        rline = ret.lines.get()
        self.assertEqual(rline.original_line_id, self.line_b.id)
        self.line_a.refresh_from_db()
        self.line_b.refresh_from_db()
        self.assertEqual(self.line_a.returned_quantity, D('0'))
        self.assertEqual(self.line_b.returned_quantity, D('2'))
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('14'))  # 20 - 8 + 2
        je = ret.journal_entry
        dr, cr = entry_totals(je)
        self.assertEqual(dr, cr)
        self.assertEqual(account_net(je, '4120'), D('20'))   # sales returns (contra revenue) debited
        self.assertEqual(account_net(je, '4100'), D('0'))

    def test_cancel_return_restores_returned_quantity_on_right_line(self):
        data = self._return(self.line_b, 2)
        cancel_invoice(data['invoice_id'], user=self.w.admin)
        self.line_a.refresh_from_db()
        self.line_b.refresh_from_db()
        self.assertEqual(self.line_a.returned_quantity, D('0'))
        self.assertEqual(self.line_b.returned_quantity, D('0'))
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('12'))

    def test_cannot_cancel_original_with_active_return(self):
        self._return(self.line_b, 1)
        with self.assertRaises(ValueError):
            cancel_invoice(self.sale.id)

    def test_return_requires_admin(self):
        self.client.logout()
        login(self.client, self.w.cashier)
        resp = self.client.post(
            reverse('invoicing:process_invoice_return', args=[self.sale.id]),
            data=json.dumps({'items': [{'line_id': self.line_b.id, 'quantity': '1'}]}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)


class CancelInvoiceReversalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 10, '6')
        self.inv = make_invoice(self.w, 'SALE', lines=[(self.w.product, 4, 10, 14)], payment_type='CASH')
        self.original = self.inv.journal_entry

    def test_cancel_posts_reversal_and_keeps_original(self):
        cancel_invoice(self.inv.id, user=self.w.admin)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, Invoice.CANCELLED)
        self.assertIsNone(self.inv.journal_entry_id)
        self.original.refresh_from_db()
        self.assertEqual(self.original.status, JournalEntry.POSTED)
        rev = JournalEntry.objects.get(reversal_of=self.original)
        self.assertEqual(rev.status, JournalEntry.POSTED)
        orig_items = sorted((i.account_id, i.debit, i.credit) for i in self.original.items.all())
        rev_items = sorted((i.account_id, i.credit, i.debit) for i in rev.items.all())
        self.assertEqual(orig_items, rev_items)
        for code in ('4100', '2130', '5100', '1140', f'1110-{self.w.treasury.id}'):
            self.assertEqual(account_net(self.original, code) + account_net(rev, code), D('0'), code)

    def test_cancel_restores_stock(self):
        cancel_invoice(self.inv.id)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('10'))

    def test_cannot_reverse_twice(self):
        reverse_journal_entry(self.original)
        with self.assertRaises(ValidationError):
            reverse_journal_entry(self.original)

    def test_cannot_reverse_a_reversal(self):
        rev = reverse_journal_entry(self.original)
        with self.assertRaises(ValidationError):
            reverse_journal_entry(rev)

    def test_cancel_twice_refused(self):
        cancel_invoice(self.inv.id)
        with self.assertRaises(ValueError):
            cancel_invoice(self.inv.id)

    def test_cancel_purchase_removes_its_stock(self):
        p = make_invoice(self.w, 'PURCHASE', lines=[(self.w.product2, 5, 20)], payment_type='CREDIT')
        self.assertEqual(self.w.product2.get_stock(self.w.warehouse), D('5'))
        cancel_invoice(p.id)
        self.assertEqual(self.w.product2.get_stock(self.w.warehouse), D('0'))
        self.assertTrue(JournalEntry.objects.filter(reversal_of_id=p.journal_entry_id).exists())
