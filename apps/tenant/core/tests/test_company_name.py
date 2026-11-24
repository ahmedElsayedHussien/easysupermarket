"""The organisation name on printed documents comes from Settings (store_name)."""
import io
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.company import company_profile
from apps.tenant.core.models import SystemSetting
from apps.tenant.core.tests.factories import build_world, login, stock_in
from apps.tenant.invoicing.models import POSSession, Quotation, QuotationLine

ORG = 'شركة النور للتجارة'
LEGACY = ('شركتك المحترمة', 'EasySupermarket', 'مسار لإدارة المبيعات', 'مسار ERP')


class CompanyNameTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        s = SystemSetting.get_settings()
        s.store_name, s.tax_number = ORG, '123-456-789'
        s.save()
        login(self.client, self.w.admin)

    def assert_org(self, resp):
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, ORG)
        for legacy in LEGACY:
            self.assertNotContains(resp, legacy)

    def test_profile_and_legacy_alias(self):
        self.assertEqual(company_profile()['name'], ORG)
        self.assertEqual(SystemSetting.get_settings().company_name, ORG)

    def test_quotation_print(self):
        stock_in(self.w.product, self.w.warehouse, 10, '5')
        q = Quotation.objects.create(partner=self.w.customer, branch=self.w.branch,
                                     warehouse=self.w.warehouse, created_by=self.w.admin)
        QuotationLine.objects.create(quotation=q, product=self.w.product, quantity=D('1'), unit_price=D('10'))
        q.recalculate_totals()
        r = self.client.get(reverse('invoicing:quotation_print', args=[q.id]))
        self.assert_org(r)
        self.assertContains(r, '123-456-789')

    def test_partner_statement_and_excel(self):
        self.assert_org(self.client.get(reverse('partners:partner_ledger', args=[self.w.customer.id])))
        r = self.client.get(reverse('partners:partner_ledger_export_excel', args=[self.w.customer.id]))
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
        self.assertEqual(ws['A1'].value, ORG)

    def test_shift_report_header(self):
        s = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        self.assert_org(self.client.get(reverse('core:report_shift_detail', args=[s.id])))

    def test_falls_back_to_branch_name_when_not_set(self):
        s = SystemSetting.get_settings()
        s.store_name = ''
        s.save()
        stock_in(self.w.product, self.w.warehouse, 10, '5')
        q = Quotation.objects.create(partner=self.w.customer, branch=self.w.branch,
                                     warehouse=self.w.warehouse, created_by=self.w.admin)
        r = self.client.get(reverse('invoicing:quotation_print', args=[q.id]))
        self.assertNotContains(r, 'شركتك المحترمة')
        self.assertContains(r, self.w.branch.name)


class ReceiptPhoneAndTitleTests(TestCase):
    """Store phone from Settings (else the branch's) and the right document name."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.w.branch.phone, cls.w.branch.address = '0100000001', 'شارع 1 المعادي'
        cls.w.branch.save()

    def setUp(self):
        login(self.client, self.w.admin)
        stock_in(self.w.product, self.w.warehouse, 20, '5')

    def _receipt(self, inv, size='a4'):
        return self.client.get(reverse('invoicing:receipt', args=[inv.id]) + f'?size={size}')

    def test_branch_phone_when_settings_empty(self):
        from apps.tenant.core.tests.factories import make_invoice
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10)], payment_type='CASH')
        r = self._receipt(inv)
        self.assertContains(r, '0100000001')
        self.assertContains(r, 'شارع 1 المعادي')

    def test_settings_phone_wins(self):
        from apps.tenant.core.tests.factories import make_invoice
        s = SystemSetting.get_settings()
        s.store_phone, s.store_address = '0225551234', 'ميدان التحرير'
        s.save()
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10)], payment_type='CASH')
        for size in ('a4', '80mm'):
            r = self._receipt(inv, size)
            self.assertContains(r, '0225551234')
            self.assertContains(r, 'ميدان التحرير')
            self.assertNotContains(r, '0100000001')

    def test_sale_and_return_titles(self):
        import json
        from apps.tenant.core.tests.factories import make_invoice
        from apps.tenant.invoicing.models import Invoice
        sale = make_invoice(self.w, lines=[(self.w.product, 3, 10)], payment_type='CASH')
        r = self._receipt(sale)
        self.assertContains(r, 'فاتورة مبيعات')
        self.assertNotContains(r, 'مرتجع مبيعات')
        line = sale.lines.first()
        data = self.client.post(reverse('invoicing:process_invoice_return', args=[sale.id]),
                                data=json.dumps({'items': [{'line_id': line.id, 'quantity': '1'}],
                                                 'payment_type': 'CASH', 'treasury_id': self.w.treasury.id}),
                                content_type='application/json').json()
        ret = Invoice.objects.get(pk=data['invoice_id'])
        for size in ('a4', '80mm'):
            r = self._receipt(ret, size)
            self.assertContains(r, 'مرتجع مبيعات')
            self.assertContains(r, sale.invoice_number)          # original invoice reference
            self.assertNotContains(r, 'فاتورة مبيعات رسمية')
            self.assertNotContains(r, 'فاتورة مبيعات ضريبية')
            self.assertNotContains(r, 'مسددة بالكامل')
