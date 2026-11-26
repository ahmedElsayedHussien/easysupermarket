"""Returns: from the invoice, or — for goods sold / bought before the system — without
one (sales: own screen with a price ceiling; purchases: the purchase screen)."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_branch, make_invoice, make_user, make_warehouse, stock_in
from apps.tenant.invoicing.models import Invoice


class SalesReturnWithoutInvoiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)

    def post(self, price, qty='2', warehouse=None, payment='CREDIT'):
        return self.client.post(reverse('invoicing:sales_return_no_invoice'), {
            'partner_id': self.w.customer.id, 'warehouse_id': (warehouse or self.w.warehouse).id,
            'payment_type': payment, 'treasury_id': self.w.treasury.id,
            'product_ids[]': [self.w.product.id], 'quantities[]': [qty], 'prices[]': [price], 'uom_ids[]': [''],
        })

    def test_creates_posted_return_and_stock_in(self):
        before = self.w.product.get_stock(self.w.warehouse)
        r = self.post(str(self.w.product.sale_price))
        ret = Invoice.objects.get(invoice_type=Invoice.RETURN_SALE)
        self.assertRedirects(r, reverse('invoicing:invoice_detail', args=[ret.id]), fetch_redirect_response=False)
        self.assertEqual(ret.status, Invoice.POSTED)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), before + 2)

    def test_price_above_current_sale_price_refused(self):
        self.post(str(self.w.product.sale_price + D('1')))
        self.assertFalse(Invoice.objects.filter(invoice_type=Invoice.RETURN_SALE).exists())

    def test_other_branch_warehouse_refused_for_branch_user(self):
        b2 = make_branch('فرع بعيد')
        wh2 = make_warehouse(b2, 'مخزن بعيد')
        user = make_user('ret_user', role='CASHIER', branch=self.w.branch,
                         perms=('invoicing.add_sales_return_no_invoice', 'invoicing.view_invoice'))
        login(self.client, user)
        self.post(str(self.w.product.sale_price), warehouse=wh2)
        self.assertFalse(Invoice.objects.filter(invoice_type=Invoice.RETURN_SALE).exists())

    def test_needs_its_own_permission(self):
        user = make_user('ret_from_invoice_only', role='CASHIER', branch=self.w.branch,
                         perms=('invoicing.add_sales_return', 'invoicing.view_invoice'))
        login(self.client, user)
        self.assertEqual(self.client.get(reverse('invoicing:sales_return_no_invoice')).status_code, 302)
        r = self.client.get(reverse('invoicing:sales_invoice_list'))
        self.assertNotContains(r, reverse('invoicing:sales_return_no_invoice'))

    def test_buttons_on_invoice_lists(self):
        r = self.client.get(reverse('invoicing:sales_return_no_invoice'))
        self.assertContains(r, 'مرتجع مبيعات بدون فاتورة (رصيد أول المدة)')
        self.assertContains(self.client.get(reverse('invoicing:sales_invoice_list')), 'مرتجع مبيعات بدون فاتورة')
        r = self.client.get(reverse('invoicing:purchase_invoice_list'))
        self.assertContains(r, '?type=RETURN_PURCHASE')
        r = self.client.get(reverse('invoicing:purchase_invoice') + '?type=RETURN_PURCHASE')
        self.assertContains(r, '<option value="RETURN_PURCHASE" selected')


class ReturnFromInvoicePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        stock_in(cls.w.product, cls.w.warehouse, 20, D('5'))

    def test_purchase_return_from_invoice_needs_purchase_permission(self):
        import json
        purchase = make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.w.product, 3, D('5'))])
        seller = make_user('seller_returns', role='CASHIER', branch=self.w.branch,
                           perms=('invoicing.view_invoice', 'invoicing.add_sales_return'))
        login(self.client, seller)
        r = self.client.post(reverse('invoicing:process_invoice_return', args=[purchase.id]),
                             json.dumps({'items': [{'line_id': purchase.lines.first().id, 'quantity': 1}]}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_invoice_detail_shows_return_button_by_permission(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 2, D('10'))])
        seller = make_user('seller_detail', role='CASHIER', branch=self.w.branch,
                           perms=('invoicing.view_invoice', 'invoicing.add_sales_return'))
        login(self.client, seller)
        r = self.client.get(reverse('invoicing:invoice_detail', args=[sale.id]))
        self.assertContains(r, 'openReturnModal()')
        self.assertNotContains(r, 'onclick="cancelInvoice(')
