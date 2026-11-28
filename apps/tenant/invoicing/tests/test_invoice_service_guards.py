"""إلغاء / تعديل سداد فاتورة وردية مقفولة ممنوع، السداد المقسم بباقي آجل، وحركات السيريال في كارت الصنف."""
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.tenant.core.tests.factories import build_world, make_invoice, make_partner, make_product, stock_in
from apps.tenant.inventory.models import SerialItem, StockMovement
from apps.tenant.invoicing.models import Invoice, InvoiceLine, POSSession
from apps.tenant.services.invoice_service import cancel_invoice, update_invoice_payment_method


class InvoiceServiceGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, D('4'))

    def test_closed_shift_invoice_not_cancelled_or_repaid(self):
        session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        inv = make_invoice(self.w, lines=[(self.w.product, 2, 10)], pos_session=session)
        POSSession.objects.filter(pk=session.pk).update(status=POSSession.CLOSED)
        with self.assertRaisesMessage(ValidationError, 'وردية'):
            cancel_invoice(inv.id, self.w.admin)
        with self.assertRaisesMessage(ValidationError, 'وردية'):
            update_invoice_payment_method(inv.id, 'CARD', user=self.w.admin)
        self.assertEqual(Invoice.objects.get(pk=inv.pk).status, Invoice.POSTED)

    def test_split_rest_goes_on_account(self):
        customer = make_partner('عميل مقسم', 'CUSTOMER')
        inv = make_invoice(self.w, lines=[(self.w.product, 10, 10)], partner=customer)          # 100 cash
        with self.assertRaisesMessage(ValueError, 'لا يساوي'):            # a shortfall is not credit by itself
            update_invoice_payment_method(inv.id, 'SPLIT', treasury_id=self.w.treasury.id, paid_cash=D('60'),
                                          user=self.w.admin)
        update_invoice_payment_method(inv.id, 'SPLIT', treasury_id=self.w.treasury.id, paid_cash=D('60'),
                                      paid_credit=D('40'), user=self.w.admin)
        inv.refresh_from_db()
        self.assertEqual((inv.paid_cash, inv.paid_credit), (D('60'), D('40')))
        customer.refresh_from_db()
        self.assertEqual(customer.customer_balance, D('40'))                                   # from the ledger
        update_invoice_payment_method(inv.id, 'CREDIT', user=self.w.admin)
        inv.refresh_from_db()
        self.assertEqual((inv.paid_cash, inv.paid_credit), (D('0'), D('100')))

    def test_serial_moves_on_the_card_and_sold_serial_blocks_purchase_cancel(self):
        phone = make_product(self.w.category, 'موبايل سيريال', barcode='6221000000925', product_type='SERIALIZED')
        serial = SerialItem.objects.create(product=phone, imei_1='356938035643810', actual_cost=D('5000'))
        pur = make_invoice(self.w, invoice_type='PURCHASE', lines=[], confirm=False)
        InvoiceLine.objects.create(invoice=pur, product=phone, quantity=D('1'), unit_price=D('5000'),
                                   serial_item=serial)
        pur.recalculate_totals()
        Invoice.objects.filter(pk=pur.pk).update(paid_cash=pur.total_amount)
        from apps.tenant.services.invoice_service import confirm_invoice
        confirm_invoice(pur.id)
        sale = make_invoice(self.w, lines=[], confirm=False)
        InvoiceLine.objects.create(invoice=sale, product=phone, quantity=D('1'), unit_price=D('6000'),
                                   serial_item=serial)
        sale.recalculate_totals()
        Invoice.objects.filter(pk=sale.pk).update(paid_cash=sale.total_amount)
        confirm_invoice(sale.id)
        types = list(StockMovement.objects.filter(product=phone).values_list('movement_type', flat=True))
        self.assertEqual(sorted(types), ['IN', 'OUT'])
        with self.assertRaisesMessage(ValueError, 'اتباع'):
            cancel_invoice(pur.id, self.w.admin)
        self.assertTrue(SerialItem.objects.filter(pk=serial.pk).exists())
