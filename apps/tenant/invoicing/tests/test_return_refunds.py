"""Sales returns: refund method may differ from the sale's, the entry follows the chosen
method, and money refunds never exceed what was really paid on the invoice."""
import json
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import Voucher
from apps.tenant.core.tests.factories import (
    account_net, build_world, entry_totals, login, make_branch, make_ewallet, make_invoice,
    make_treasury, stock_in,
)
from apps.tenant.invoicing.models import Invoice, POSSession
from apps.tenant.invoicing.return_rules import money_refund_allowance


class ReturnRefundTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, '4')
        login(self.client, self.w.admin)

    def _return(self, inv, qty, payment_type, **extra):
        line = inv.lines.order_by('id').first()
        payload = {'items': [{'line_id': line.id, 'quantity': str(qty)}], 'payment_type': payment_type}
        payload.update(extra)
        return self.client.post(reverse('invoicing:process_invoice_return', args=[inv.id]),
                                data=json.dumps(payload), content_type='application/json').json()

    def _ret(self, data):
        self.assertEqual(data['status'], 'success', data)
        return Invoice.objects.get(pk=data['invoice_id'])

    def _receipt(self, amount):
        v = Voucher.objects.create(voucher_type=Voucher.RECEIPT, amount=D(amount), partner=self.w.customer,
                                   account=self.w.customer.receivable_account, payment_method=Voucher.CASH,
                                   treasury=self.w.treasury)
        v.confirm_voucher()

    # --- refund method different from the sale ------------------------------
    def test_cash_sale_refunded_to_ewallet(self):
        wallet = make_ewallet('فودافون كاش')
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10, 14)], payment_type='CASH')
        ret = self._ret(self._return(sale, 2, 'EWALLET', ewallet_id=wallet.id))   # 2 x 10 + 14% = 22.80
        je = ret.journal_entry
        dr, cr = entry_totals(je)
        self.assertEqual(dr, cr)
        self.assertEqual(account_net(je, '4120'), D('20'))                         # sales returns
        wallet.refresh_from_db()
        self.assertEqual(account_net(je, wallet.account.code), D('-22.80'))       # money out of the wallet
        self.assertEqual(account_net(je, f'1110-{self.w.treasury.id}'), D('0'))   # drawer untouched
        self.assertEqual(account_net(je, '1140'), D('8'))                         # stock back at cost 4

    def test_cash_sale_refunded_as_customer_credit(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CASH')
        before = self.w.customer.customer_balance
        self._ret(self._return(sale, 1, 'CREDIT'))
        self.w.customer.__dict__.pop('_cached_customer_balance', None)
        self.assertEqual(self.w.customer.customer_balance - before, D('-10'))     # store credit

    # --- money can't exceed what was paid ---------------------------------------
    def test_unpaid_credit_sale_cannot_be_refunded_in_cash(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CREDIT')
        self.assertEqual(money_refund_allowance(sale), D('0'))
        data = self._return(sale, 2, 'CASH')
        self.assertEqual(data['status'], 'error')
        self.assertIn('رصيد دائن', data['message'])
        self.assertFalse(Invoice.objects.filter(invoice_type=Invoice.RETURN_SALE).exists())
        sale.lines.first().refresh_from_db()
        self.assertEqual(sale.lines.first().returned_quantity, D('0'))   # rolled back
        self._ret(self._return(sale, 2, 'CREDIT'))                       # the right way

    def test_credit_sale_paid_off_later_can_be_refunded_in_cash(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CREDIT')   # owes 50
        self._receipt('50')                                                                  # paid
        self.assertEqual(money_refund_allowance(sale), D('50'))
        self._ret(self._return(sale, 2, 'CASH'))
        self.assertEqual(money_refund_allowance(sale), D('30'))

    def test_partially_paid_credit_sale(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CREDIT')   # owes 50
        self._receipt('20')
        self.assertEqual(money_refund_allowance(sale), D('20'))
        self.assertEqual(self._return(sale, 3, 'CASH')['status'], 'error')                   # 30 > 20
        self._ret(self._return(sale, 2, 'CASH'))                                              # 20 ok
        self.assertEqual(self._return(sale, 1, 'CASH')['status'], 'error')                   # nothing left

    def test_split_sale_cash_part_only(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 10, 10)], payment_type='SPLIT', confirm=False)
        sale.paid_cash, sale.paid_credit = D('40'), D('60')
        sale.save(update_fields=['paid_cash', 'paid_credit'])
        from apps.tenant.services.invoice_service import confirm_invoice
        confirm_invoice(sale.id)
        sale.refresh_from_db()
        self.assertEqual(money_refund_allowance(sale), D('40'))
        self.assertEqual(self._return(sale, 5, 'CASH')['status'], 'error')                   # 50 > 40
        ret = self._ret(self._return(sale, 5, 'SPLIT', split_payments={'cash': 40, 'credit': 10}))
        dr, cr = entry_totals(ret.journal_entry)
        self.assertEqual(dr, cr)
        self.assertEqual(money_refund_allowance(sale), D('0'))
        self._ret(self._return(sale, 1, 'CREDIT'))

    def test_previous_credit_returns_do_not_unlock_cash(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CREDIT')
        self._ret(self._return(sale, 2, 'CREDIT'))          # balance drops 20, nothing was paid
        self.assertEqual(money_refund_allowance(sale), D('0'))

    # --- shift & branch -----------------------------------------------------
    def test_return_without_shift_not_attached_to_closed_shift(self):
        shift = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.cashier,
                                          status=POSSession.CLOSED)
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CASH', pos_session=shift)
        ret = self._ret(self._return(sale, 1, 'CASH', treasury_id=self.w.treasury.id))
        self.assertIsNone(ret.pos_session_id)

    def test_cash_refund_from_open_shift_drawer(self):
        drawer = make_treasury(self.w.branch, 'درج 2')
        POSSession.objects.create(branch=self.w.branch, treasury=drawer, user=self.w.admin)
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CASH')
        ret = self._ret(self._return(sale, 1, 'CASH', treasury_id=self.w.treasury.id))
        self.assertEqual(ret.treasury_id, drawer.id)
        self.assertEqual(account_net(ret.journal_entry, f'1110-{drawer.id}'), D('-10'))

    def test_treasury_of_other_branch_rejected(self):
        other = make_treasury(make_branch('فرع 2'), 'خزينة فرع 2')
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CASH')
        self.client.cookies.load({})  # header branch = admin's own branch (main)
        data = self._return(sale, 1, 'CASH', treasury_id=other.id)
        self.assertEqual(data['status'], 'error')
        self.assertIn('الفرع الحالي', data['message'])

    def test_detail_page_shows_allowance(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 5, 10)], payment_type='CREDIT')
        r = self.client.get(reverse('invoicing:invoice_detail', args=[sale.id]))
        self.assertContains(r, 'المتاح رده نقداً')
        self.assertEqual(r.context['money_refund_allowance'], D('0'))
