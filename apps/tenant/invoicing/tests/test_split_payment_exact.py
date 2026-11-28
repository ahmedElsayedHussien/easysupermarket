"""P3-4 (4/6): a split payment equals the invoice total EXACTLY — no 0.05 tolerance anywhere.

The only rounding left is the POS cash rounding (rounding_amount -> 5115)."""
from decimal import Decimal as D

from django.core.exceptions import ValidationError

from apps.tenant.core.tests.factories import make_bank_account, make_invoice, make_product, stock_in, system_settings
from apps.tenant.invoicing.models import Invoice
from apps.tenant.invoicing.tests.test_pos_settings import _POSBase


class PosSplitExactTests(_POSBase):
    def setUp(self):
        super().setUp()
        self.bank = make_bank_account('ماكينة فيزا')
        self.p = make_product(self.w.category, 'صنف 10.33', sale_price=D('10.33'), tax_rate=D('0'))
        stock_in(self.p, self.w.warehouse, 20, D('5'))

    def split(self, cash, card):
        return self._sale([self._item(product=self.p, quantity=1, unit_price='10.33')], payment_type='SPLIT',
                          bank_account_id=self.bank.id,
                          payments={'cash': cash, 'card': card, 'visa_bank_id': self.bank.id}).json()

    def test_a_piaster_short_is_refused(self):
        system_settings(pos_cash_rounding=0)
        data = self.split('5.32', '5')                       # 10.32 of 10.33
        self.assertFalse(data['success'])
        self.assertIn('أقل من إجمالي الفاتورة', data['error'])
        self.assertFalse(Invoice.objects.filter(payment_type='SPLIT').exists())

    def test_card_a_piaster_over_the_total_is_refused(self):
        system_settings(pos_cash_rounding=0)
        data = self.split('0', '10.34')
        self.assertFalse(data['success'])
        self.assertIn('يتجاوز إجمالي الفاتورة', data['error'])

    def test_exact_legs_pass_and_the_ledger_balances(self):
        system_settings(pos_cash_rounding=0)
        data = self.split('5.33', '5')
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(payment_type='SPLIT')
        self.assertEqual((inv.total_amount, inv.paid_cash, inv.paid_card), (D('10.33'), D('5.33'), D('5.00')))
        self.assertEqual(inv.journal_entry.status, 'POSTED')

    def test_cash_rounding_is_the_one_rounding_left(self):
        system_settings(pos_cash_rounding=D('0.50'))
        data = self.split('5.50', '5')                       # cash due 5.33 -> 5.50
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(payment_type='SPLIT')
        self.assertEqual((inv.paid_cash, inv.rounding_amount), (D('5.50'), D('0.17')))


class PaymentMethodChangeExactTests(_POSBase):
    def test_split_change_must_add_up_exactly(self):
        from apps.tenant.services.invoice_service import update_invoice_payment_method
        sale = make_invoice(self.w, lines=[(self.w.product, 1, D('10.33'))], payment_type='CREDIT')
        bank = make_bank_account('بنك')
        with self.assertRaisesMessage(ValueError, 'لا يساوي إجمالي الفاتورة'):     # 0.01 short: was let through
            update_invoice_payment_method(invoice_id=sale.id, payment_type='SPLIT', treasury_id=self.w.treasury.id,
                                          bank_account_id=bank.id, paid_cash=D('5.32'), paid_card=D('5'),
                                          paid_credit=D('0'))
        update_invoice_payment_method(invoice_id=sale.id, payment_type='SPLIT', treasury_id=self.w.treasury.id,
                                      bank_account_id=bank.id, paid_cash=D('5.33'), paid_card=D('5'),
                                      paid_credit=D('0'))
        sale.refresh_from_db()
        self.assertEqual((sale.paid_cash, sale.paid_card), (D('5.33'), D('5.00')))


class JournalNeverAbsorbsAGapTests(_POSBase):
    def test_sale_entry_refuses_legs_that_do_not_add_up(self):
        from apps.tenant.services.journal_service import post_sale_invoice
        sale = make_invoice(self.w, lines=[(self.w.product, 1, D('10.33'))], payment_type='SPLIT',
                            treasury=self.w.treasury, paid_cash=D('5.30'), paid_card=D('5'), confirm=False,
                            bank_account=make_bank_account('بنك'))
        with self.assertRaisesMessage(ValidationError, 'لا يساوي إجمالي الفاتورة'):   # 0.03: was absorbed silently
            post_sale_invoice(sale)
