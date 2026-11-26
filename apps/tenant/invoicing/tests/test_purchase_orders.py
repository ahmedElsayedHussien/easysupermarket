"""أوامر الشراء: الاقتراح من النواقص، الإنشاء، والاستلام بفاتورة شراء."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_product, make_user, stock_in
from apps.tenant.invoicing.models import Invoice, PurchaseOrder
from apps.tenant.invoicing.purchase_orders import reorder_suggestions


class PurchaseOrderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.p = make_product(cls.w.category, 'زيت 1 لتر', sale_price=D('80'), min_stock_level=D('10'),
                             max_stock_level=D('50'), last_purchase_price=D('60'))

    def setUp(self):
        login(self.client, self.w.admin)

    def test_suggestion_quantity_and_supplier(self):
        stock_in(self.p, self.w.warehouse, 4, D('60'))
        from apps.tenant.core.tests.factories import make_invoice
        make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.p, 1, D('60'))])      # last supplier
        rows = reorder_suggestions(self.w.warehouse)
        row = next(r for r in rows if r['product'] == self.p)
        self.assertEqual(row['quantity'], D('45'))                     # 50 - 5 in stock
        self.assertEqual(row['supplier'], self.w.supplier)
        self.client.post(reverse('invoicing:purchase_order_suggest'), {
            'warehouse': self.w.warehouse.id, 'pick': [self.p.id], f'qty_{self.p.id}': '45',
            f'supplier_{self.p.id}': self.w.supplier.id})
        order = PurchaseOrder.objects.get()
        self.assertEqual(order.lines.get().quantity, D('45'))
        row = next(r for r in reorder_suggestions(self.w.warehouse) if r['product'] == self.p) \
            if any(r['product'] == self.p for r in reorder_suggestions(self.w.warehouse)) else None
        self.assertIsNone(row)                                          # already on order

    def test_create_receive_partial_then_full(self):
        self.client.post(reverse('invoicing:purchase_order_create'), {
            'supplier': self.w.supplier.id, 'warehouse': self.w.warehouse.id,
            'product[]': [self.p.id], 'qty[]': ['10'], 'price[]': ['60'], 'uom[]': ['']})
        order = PurchaseOrder.objects.get()
        r = self.client.get(reverse('invoicing:purchase_invoice') + f'?po={order.id}')
        self.assertContains(r, f'name="po_id" value="{order.id}"')
        self.assertIn('"quantity": "10', r.context['posted_lines_json'])
        base = {'invoice_type': 'PURCHASE', 'supplier_id': self.w.supplier.id, 'warehouse_id': self.w.warehouse.id,
                'branch_id': self.w.branch.id, 'payment_type': 'CREDIT', 'po_id': order.id,
                'product_id[]': [self.p.id], 'price[]': ['60'], 'uom_id[]': [''], 'expiry_date[]': ['']}
        self.client.post(reverse('invoicing:purchase_invoice'), {**base, 'quantity[]': ['4']})
        order.refresh_from_db()
        self.assertEqual((order.status, order.lines.get().received_quantity), (PurchaseOrder.PARTIAL, D('4')))
        self.client.post(reverse('invoicing:purchase_invoice'), {**base, 'quantity[]': ['6']})
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.RECEIVED)
        self.assertEqual(Invoice.objects.filter(purchase_order=order).count(), 2)

    def test_detail_actions_and_permission(self):
        self.client.post(reverse('invoicing:purchase_order_create'), {
            'supplier': self.w.supplier.id, 'warehouse': self.w.warehouse.id,
            'product[]': [self.p.id], 'qty[]': ['3'], 'price[]': ['60.50'], 'uom[]': ['']})
        order = PurchaseOrder.objects.get()
        r = self.client.get(reverse('invoicing:purchase_order_detail', args=[order.id]))
        self.assertEqual(r.context['total'], D('181.50'))
        self.client.post(reverse('invoicing:purchase_order_detail', args=[order.id]), {'action': 'sent'})
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.SENT)
        login(self.client, make_user('no_po', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('invoicing:purchase_order_list')).status_code, 302)
