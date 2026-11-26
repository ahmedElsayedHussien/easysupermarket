"""الفاتورة المرحّلة ثابتة، ومعامل الوحدة بيتحفظ على السطر وقت البيع."""
import datetime
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.tenant.core.tests.factories import build_world, make_invoice, stock_in
from apps.tenant.inventory.models import ProductUoM, UnitOfMeasure
from apps.tenant.invoicing.models import Invoice, InvoiceLine
from apps.tenant.services.invoice_service import cancel_invoice


class InvoiceIntegrityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.carton = ProductUoM.objects.create(product=cls.w.product, uom=UnitOfMeasure.objects.create(name='كرتونة'),
                                               conversion_factor=D('12'))

    def sale_in_cartons(self, cartons=2):
        inv = make_invoice(self.w, lines=[], confirm=False)
        InvoiceLine.objects.create(invoice=inv, product=self.w.product, quantity=D(cartons), unit_price=D('100'),
                                   uom=self.carton)
        inv.recalculate_totals()
        inv.paid_cash = inv.total_amount
        inv.save(update_fields=['paid_cash'])
        return inv

    def test_factor_kept_on_line_and_used_on_cancel(self):
        stock_in(self.w.product, self.w.warehouse, 100, D('5'))
        from apps.tenant.services.invoice_service import confirm_invoice
        inv = self.sale_in_cartons()
        confirm_invoice(inv.id)
        line = inv.lines.get()
        self.assertEqual((line.conversion_factor, line.base_quantity), (D('12'), D('24')))
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('76'))
        ProductUoM.objects.filter(pk=self.carton.pk).update(conversion_factor=D('6'))   # unit changed later
        cancel_invoice(inv.id, self.w.admin)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('100'))         # 24 back, not 12

    def test_clean_compares_base_units(self):
        stock_in(self.w.product, self.w.warehouse, 5, D('5'))
        inv = self.sale_in_cartons()
        Invoice.objects.filter(pk=inv.pk).update(cashier=None)      # not a manager
        line = InvoiceLine.objects.get(invoice=inv)
        with self.assertRaisesMessage(ValidationError, 'تتجاوز المخزون'):
            line.clean()                                            # 2 cartons = 24 pieces > 5

    def test_posted_invoice_and_lines_are_frozen(self):
        stock_in(self.w.product, self.w.warehouse, 100, D('5'))
        inv = make_invoice(self.w, lines=[(self.w.product, 2, 10)])
        inv.total_amount = D('1')
        with self.assertRaisesMessage(ValidationError, 'مرحّلة'):
            inv.save()
        inv.refresh_from_db()
        inv.eta_status = 'Valid'                                    # non-financial fields still update
        inv.save(update_fields=['eta_status'])
        line = inv.lines.get()
        line.unit_price = D('1')
        with self.assertRaisesMessage(ValidationError, 'مرحّلة'):
            line.save()
        with self.assertRaisesMessage(ValidationError, 'مرحّلة'):
            line.delete()
        with self.assertRaisesMessage(ValidationError, 'مرحّلة'):
            InvoiceLine.objects.create(invoice=inv, product=self.w.product, quantity=D('1'), unit_price=D('1'))
        line.refresh_from_db()
        line.returned_quantity = D('1')
        line.save(update_fields=['returned_quantity'])              # returns are allowed

    def test_number_year_follows_invoice_date(self):
        inv = make_invoice(self.w, lines=[], confirm=False, date=datetime.date(2025, 12, 31))
        self.assertTrue(inv.invoice_number.startswith('SALE-2025-'), inv.invoice_number)
