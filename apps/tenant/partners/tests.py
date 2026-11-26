"""Partner statement in detailed (items) mode."""
import importlib
from decimal import Decimal as D

from django.apps import apps as django_apps
from django.test import TestCase

from apps.tenant.core.tests.factories import build_world, make_invoice, stock_in
from apps.tenant.invoicing.models import InvoiceLine
from apps.tenant.partners.services import get_partner_full_statement
from apps.tenant.partners.views import _detail_lines_with_adjustment


class StatementItemTotalsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, '4')
        # Like the July 2026 invoices: amounts right, stored line total 0
        self.inv = make_invoice(self.w, lines=[(self.w.product, 3, 10, 14)], payment_type='CREDIT')
        InvoiceLine.objects.filter(invoice=self.inv).update(total_amount=0)

    def _rows(self):
        stmt = get_partner_full_statement(self.w.customer)
        t = next(t for t in stmt['transactions'] if t.get('lines'))
        return list(_detail_lines_with_adjustment(t))

    def test_item_total_from_its_parts_and_no_adjustment(self):
        rows = self._rows()
        self.assertEqual(len(rows), 1)                     # no "تسوية إجمالي الفاتورة" row
        line, debit, credit = rows[0]
        self.assertEqual(line['total_amount'], D('34.20'))  # 30 + 14% VAT
        self.assertEqual(debit, D('34.20'))
        self.assertNotIn('is_adjustment', line)

    def test_unit_name_is_arabic_not_code(self):
        line = self._rows()[0][0]
        self.assertNotEqual(line['uom_name'], 'PIECE')
        self.assertTrue(line['uom_name'])

    def test_backfill_migration_repairs_stored_totals(self):
        mig = importlib.import_module('apps.tenant.invoicing.migrations.0020_backfill_invoiceline_total')
        mig.backfill(django_apps, None)
        self.assertEqual(InvoiceLine.objects.get(invoice=self.inv).total_amount, D('34.20'))


class PartnerCardTests(TestCase):
    """Tax / national id checked on the card; the e-invoice address saved; legacy Payment frozen once posted."""
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def post_card(self, **extra):
        from django.urls import reverse
        from apps.tenant.core.tests.factories import login
        login(self.client, self.w.admin)
        data = {'name': self.w.customer.name, 'partner_type': 'CUSTOMER', 'opening_balance': '0',
                'opening_balance_type': 'DEBIT', 'phone': '', 'mobile': '', 'email': '', 'national_id': '',
                'tax_id': '', 'vat_rate': '14', 'wht_rate': '1'}
        data.update(extra)
        return self.client.post(reverse('partners:partner_edit', args=[self.w.customer.id]), data)

    def test_ids_validated_and_address_saved(self):
        from apps.tenant.partners.models import Partner
        self.post_card(tax_id='12345')
        self.assertEqual(Partner.objects.get(pk=self.w.customer.id).tax_id, self.w.customer.tax_id)   # refused
        self.post_card(tax_id='123-456-789', national_id='2900101123456', )                         # 13 digits
        self.assertNotEqual(Partner.objects.get(pk=self.w.customer.id).tax_id, '123456789')
        self.post_card(tax_id='123-456-789', eta_governate='القاهرة', eta_region_city='مدينة نصر',
                       address='شارع عباس العقاد', eta_building_number='5')
        c = Partner.objects.get(pk=self.w.customer.id)
        self.assertEqual((c.tax_id, c.eta_governate, c.eta_region_city, c.address, c.eta_building_number),
                         ('123456789', 'القاهرة', 'مدينة نصر', 'شارع عباس العقاد', '5'))

    def test_legacy_payment_frozen_when_posted(self):
        import datetime
        from django.core.exceptions import ValidationError
        from apps.tenant.partners.models import Payment
        p = Payment.objects.create(payment_type=Payment.RECEIPT, partner=self.w.customer, branch=self.w.branch,
                                   amount=D('10'), date=datetime.date(2025, 6, 1), status=Payment.POSTED)
        self.assertTrue(p.reference.startswith('PAY-2025-'))
        p.amount = D('99')
        with self.assertRaises(ValidationError):
            p.save()
        with self.assertRaises(ValidationError):
            p.delete()
