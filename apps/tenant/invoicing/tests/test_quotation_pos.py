"""Quotations and the POS checkout endpoint (complete_sale)."""
import json
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_product, stock_in, system_settings
from apps.tenant.invoicing.models import Invoice, POSSession, Quotation, QuotationLine


class QuotationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 10, '5')
        self.q = Quotation.objects.create(partner=self.w.customer, branch=self.w.branch,
                                          warehouse=self.w.warehouse, created_by=self.w.admin)
        QuotationLine.objects.create(quotation=self.q, product=self.w.product, quantity=D('4'), unit_price=D('10'))
        self.q.recalculate_totals()
        login(self.client, self.w.admin)

    def test_confirm_does_not_move_stock(self):
        self.client.post(reverse('invoicing:quotation_confirm', args=[self.q.id]))
        self.q.refresh_from_db()
        self.assertEqual(self.q.status, Quotation.POSTED)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('10'))
        self.assertFalse(Invoice.objects.exists())

    def test_convert_deducts_stock_exactly_once(self):
        self.q.confirm_quotation()
        url = reverse('invoicing:quotation_convert', args=[self.q.id])
        self.client.post(url)
        self.client.post(url)  # double submit
        self.q.refresh_from_db()
        self.assertEqual(self.q.status, Quotation.CONVERTED)
        self.assertEqual(Invoice.objects.filter(invoice_type='SALE').count(), 1)
        inv = self.q.converted_invoice
        self.assertEqual(inv.status, Invoice.POSTED)
        self.assertEqual(inv.total_amount, D('40'))
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('6'))


class CompleteSaleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, '6')
        for user in (self.w.cashier, self.w.admin):
            POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=user)
        login(self.client, self.w.cashier)

    def _sale(self, cart, **extra):
        payload = {'cart': cart, 'payment_type': 'CASH', 'warehouse_id': self.w.warehouse.id,
                   'branch_id': self.w.branch.id}
        payload.update(extra)
        return self.client.post(reverse('invoicing:complete_sale'), data=json.dumps(payload),
                                content_type='application/json')

    def _item(self, **kw):
        item = {'product_id': self.w.product.id, 'quantity': 2, 'unit_price': 10, 'discount_percent': 0,
                'tax_rate': 0}
        item.update(kw)
        return item

    def test_server_recomputes_price_ignoring_client_unit_price(self):
        resp = self._sale([self._item(unit_price='0.01')])
        data = resp.json()
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(pk=data['invoice_id'])
        self.assertEqual(inv.lines.get().unit_price, D('10'))
        self.assertEqual(inv.total_amount, D('20'))
        self.assertEqual(data['total_amount'], '20.00')
        self.assertEqual(inv.status, Invoice.POSTED)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('48'))

    def test_tax_rate_comes_from_product(self):
        data = self._sale([self._item(tax_rate=99)]).json()
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(pk=data['invoice_id'])
        self.assertEqual(inv.lines.get().tax_rate, D('14'))
        self.assertEqual(data['total_amount'], str(inv.total_amount.quantize(D('0.01'))))
        self.assertEqual(data['total_amount'], '22.80')

    def test_negative_quantity_rejected(self):
        resp = self._sale([self._item(quantity=-3)])
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('50'))

    def test_return_invoice_type_rejected(self):
        resp = self._sale([self._item()], invoice_type='RETURN_SALE')
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Invoice.objects.exists())

    def test_discount_over_cap_rejected_for_cashier(self):
        system_settings(max_pos_discount_percentage=D('10'))
        resp = self._sale([self._item(discount_percent=20)])
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Invoice.objects.exists())
        resp = self._sale([self._item()], discount_percentage=25)
        self.assertEqual(resp.status_code, 403)

    def test_discount_within_cap_allowed_for_cashier(self):
        system_settings(max_pos_discount_percentage=D('10'))
        data = self._sale([self._item(discount_percent=10)]).json()
        self.assertTrue(data['success'], data)
        self.assertEqual(data['total_amount'], '18.00')

    def test_discount_over_cap_allowed_for_admin(self):
        system_settings(max_pos_discount_percentage=D('10'))
        self.client.logout()
        login(self.client, self.w.admin)
        data = self._sale([self._item(discount_percent=20)]).json()
        self.assertTrue(data['success'], data)
        self.assertEqual(data['total_amount'], '16.00')

    def test_requires_open_shift(self):
        POSSession.objects.filter(user=self.w.cashier).update(status=POSSession.CLOSED)
        data = self._sale([self._item()]).json()
        self.assertFalse(data['success'])

    def test_cash_received_less_than_total_rejected(self):
        data = self._sale([self._item()], cash_received='5').json()
        self.assertFalse(data['success'])
        self.assertFalse(Invoice.objects.exists())

    def test_user_without_add_invoice_permission_redirected(self):
        from apps.tenant.core.tests.factories import make_user
        self.client.logout()
        login(self.client, make_user('noperm', role='CASHIER', branch=self.w.branch))
        resp = self._sale([self._item()])
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Invoice.objects.exists())

    def test_open_price_product_uses_client_price(self):
        op = make_product(self.w.category, 'سعر مفتوح', sale_price=D('10'), tax_rate=D('0'), is_open_price=True)
        stock_in(op, self.w.warehouse, 5, '1')
        data = self._sale([self._item(product_id=op.id, unit_price='7.5', quantity=1)]).json()
        self.assertTrue(data['success'], data)
        self.assertEqual(data['total_amount'], '7.50')
