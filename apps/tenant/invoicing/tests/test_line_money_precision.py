"""P3-4 (3/6): invoice and quotation lines carry money to 2 places; the header is the sum of those lines.

3 units × 33.3333 with 10% discount and 14% VAT:
    gross 99.9999 -> discount 10.00 -> subtotal 90.00 -> VAT 12.60 -> total 102.60
and a customer who pays 102.60 owes exactly 0.00."""
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.core.money import line_amounts
from apps.tenant.core.tests.factories import build_world, make_invoice, stock_in


def two_places(value):
    return value == value.quantize(D('0.01'))


class LineMoneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, D('20'))

    def credit_sale(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 3, D('33.3333'), 14)], payment_type='CREDIT', confirm=False)
        line = inv.lines.get()
        line.discount_pct = D('10')
        line.save()
        inv.recalculate_totals()
        from apps.tenant.services.invoice_service import confirm_invoice
        confirm_invoice(inv.id)
        inv.refresh_from_db()
        return inv, inv.lines.get()

    def test_the_line_and_the_total_have_two_places(self):
        inv, line = self.credit_sale()
        self.assertEqual((line.discount_amount, line.subtotal, line.tax_amount, line.total_amount),
                         (D('10.00'), D('90.00'), D('12.60'), D('102.60')))
        self.assertEqual(line.unit_price, D('33.3333'))                   # unit price keeps its 4 places
        self.assertEqual((inv.subtotal, inv.tax_amount, inv.total_amount), (D('90.00'), D('12.60'), D('102.60')))
        for v in (inv.subtotal, inv.discount_amount, inv.tax_amount, inv.wht_amount, inv.total_amount):
            self.assertTrue(two_places(v), v)

    def test_customer_paying_the_total_owes_exactly_zero(self):
        from apps.tenant.accounting.models import Voucher
        from apps.tenant.partners.balances import ledger_balance_map
        inv, _ = self.credit_sale()
        customer = self.w.customer
        customer.refresh_from_db()
        self.assertEqual(ledger_balance_map([customer])[customer.pk][0], D('102.60'))
        v = Voucher.objects.create(voucher_type=Voucher.RECEIPT, amount=inv.total_amount, partner=customer,
                                   account=customer.receivable_account, payment_method=Voucher.CASH,
                                   treasury=self.w.treasury)
        v.confirm_voucher()
        self.assertEqual(ledger_balance_map([customer])[customer.pk][0], D('0.00'))

    def test_header_is_the_sum_of_the_rounded_lines(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 1, D('0.3333'), 14)] * 3, payment_type='CREDIT')
        lines = list(inv.lines.all())
        self.assertEqual([l.total_amount for l in lines], [D('0.38')] * 3)   # 0.33 + 0.05 VAT, each line
        self.assertEqual(inv.total_amount, D('1.14'))                        # not money(3 × 0.379962) = 1.14 by luck
        self.assertEqual(inv.total_amount, sum((l.total_amount for l in lines), D('0')))

    def test_quotation_line_same_rule(self):
        from apps.tenant.invoicing.models import Quotation, QuotationLine
        q = Quotation.objects.create(partner=self.w.customer, branch=self.w.branch, warehouse=self.w.warehouse)
        ql = QuotationLine.objects.create(quotation=q, product=self.w.product, quantity=D('3'),
                                          unit_price=D('33.3333'), discount_pct=D('10'), tax_rate=D('14'))
        self.assertEqual((ql.subtotal, ql.tax_amount, ql.total_amount), (D('90.00'), D('12.60'), D('102.60')))
        q.recalculate_totals()
        q.refresh_from_db()
        self.assertEqual(q.total_amount, D('102.60'))

    def test_line_amounts_rounds_once_from_exact_inputs(self):
        a = line_amounts(D('1'), D('0.125'), tax_rate=D('0'))
        self.assertEqual(a['subtotal'], D('0.13'))                           # half away from zero
        a = line_amounts(D('2'), D('10'), discount_fixed=D('50'))             # a fixed discount never above gross
        self.assertEqual((a['discount'], a['subtotal'], a['total']), (D('20.00'), D('0.00'), D('0.00')))


class VanCartMatchesTheInvoiceTests(TestCase):
    """price_cart (what the rep sees) and the saved invoice: the same figures, to the piaster."""

    @classmethod
    def setUpTestData(cls):
        from apps.tenant.core.tests.factories import make_user
        from apps.tenant.invoicing import van_services as van
        from apps.tenant.invoicing.models import SalesRep
        cls.w = build_world()
        cls.rep_user = make_user('van_rep_p34', role='SALES_REP', branch=cls.w.branch, perms=('invoicing.use_rep_app',))
        cls.rep = SalesRep.objects.create(employee=cls.rep_user.employee_profile, source_warehouse=cls.w.warehouse)
        van.ensure_rep_setup(cls.rep, create_van=True, create_treasury=True)
        cls.rep.refresh_from_db()

    def test_cart_and_invoice_agree(self):
        from apps.tenant.invoicing import van_services as van
        stock_in(self.w.product, self.w.warehouse, 100, D('2'))
        stock_in(self.w.product2, self.w.warehouse, 100, D('2'))
        order = van.create_loading_request(self.rep, [{'product_id': self.w.product.id, 'qty': 30},
                                                      {'product_id': self.w.product2.id, 'qty': 30}], self.rep_user)
        van.approve_loading(order, {l.id: l.requested_qty for l in order.lines.all()}, self.w.admin)
        van.issue_loading(order, self.w.admin)
        rows = [{'product_id': self.w.product.id, 'qty': '3', 'disc': '7', 'disc_type': 'pct'},
                {'product_id': self.w.product2.id, 'qty': '7', 'disc': '1.333', 'disc_type': 'amt'}]
        cart = van.price_cart(self.rep, self.rep_user, rows, None, D('3.5'), 'pct')
        inv = van.rep_sale(self.rep, self.rep_user, rows, discount_pct=D('3.5'), discount_type='pct')
        saved = {l.product_id: l for l in inv.lines.all()}
        for l in cart['lines']:
            s = saved[l['product'].id]
            self.assertEqual((l['discount'], l['subtotal'], l['tax'], l['wht'], l['total']),
                             (s.discount_amount, s.subtotal, s.tax_amount, s.wht_amount, s.total_amount))
        self.assertEqual(cart['total'], inv.total_amount)
        self.assertTrue(two_places(cart['total']))


class AmountFieldsTests(TestCase):
    def test_line_and_header_amounts_are_money_fields(self):
        from apps.tenant.core.money import MoneyField
        from apps.tenant.invoicing.models import Invoice, InvoiceLine, Quotation, QuotationLine
        for model in (Invoice, InvoiceLine, Quotation, QuotationLine):
            for name in ('subtotal', 'discount_amount', 'tax_amount', 'wht_amount', 'total_amount'):
                f = model._meta.get_field(name)
                self.assertIsInstance(f, MoneyField, f'{model.__name__}.{name}')
                self.assertEqual((f.max_digits, f.decimal_places), (18, 2))
        for name in ('unit_price', 'quantity', 'cogs_amount'):                     # unit values keep 4 places
            self.assertEqual(InvoiceLine._meta.get_field(name).decimal_places, 4)
