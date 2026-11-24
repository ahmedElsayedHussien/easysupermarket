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
