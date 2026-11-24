"""Product import from Excel: warehouse (and so branch) per sheet or per row."""
import io
from decimal import Decimal as D

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_branch, make_user, make_warehouse
from apps.tenant.inventory.models import Product

HEAD = ['اسم المنتج', 'الباركود', 'سعر البيع', 'سعر التكلفة', 'الكمية', 'القسم', 'الوحدة', 'حد إعادة الطلب', 'المخزن']


def xlsx(rows, head=HEAD):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(head)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile('items.xlsx', buf.getvalue(),
                              content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


class ImportWarehouseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.b2 = make_branch('فرع الإسكندرية')
        cls.wh2 = make_warehouse(cls.b2, 'مخزن الإسكندرية', code='WH-ALX')

    def setUp(self):
        login(self.client, self.w.admin)

    def _post(self, f, warehouse=None):
        data = {'excel_file': f, 'duplicate_action': 'update'}
        if warehouse is not None:
            data['warehouse'] = warehouse.id
        return self.client.post(reverse('inventory:product_bulk_import'), data, follow=True)

    def stock(self, name, wh):
        return Product.objects.get(name=name).get_stock(wh)

    def test_screen_defaults_to_working_branch_warehouse(self):
        r = self.client.get(reverse('inventory:product_bulk_import'))
        self.assertEqual(r.context['default_warehouse'], self.w.warehouse)
        self.assertContains(r, 'فرع: فرع الإسكندرية')

    def test_whole_sheet_to_selected_warehouse(self):
        self._post(xlsx([['صنف أ', '', 10, 6, 5, '', 'قطعة', '', '']]), self.w.warehouse)
        self.assertEqual(self.stock('صنف أ', self.w.warehouse), D('5'))

    def test_column_splits_rows_across_branches(self):
        f = xlsx([
            ['صنف ب', '111', 10, 6, 4, '', 'قطعة', '', 'مخزن الإسكندرية'],   # by name
            ['صنف ج', '222', 12, 7, 3, '', 'قطعة', '', 'WH-ALX'],            # by code
            ['صنف د', '333', 15, 9, 2, '', 'قطعة', '', ''],                  # empty -> screen choice
        ])
        self._post(f, self.w.warehouse)
        self.assertEqual(self.stock('صنف ب', self.wh2), D('4'))
        self.assertEqual(self.stock('صنف ب', self.w.warehouse), D('0'))
        self.assertEqual(self.stock('صنف ج', self.wh2), D('3'))
        self.assertEqual(self.stock('صنف د', self.w.warehouse), D('2'))

    def test_unknown_warehouse_imports_nothing(self):
        r = self._post(xlsx([['صنف هـ', '', 10, 6, 5, '', 'قطعة', '', 'مخزن غير موجود']]), self.w.warehouse)
        self.assertContains(r, 'مخازن غير معروفة')
        self.assertFalse(Product.objects.filter(name='صنف هـ').exists())

    def test_user_cannot_import_into_other_branch(self):
        user = make_user('storekeeper', role='CASHIER', branch=self.w.branch,
                         perms=('inventory.add_product', 'inventory.view_product'))
        login(self.client, user)
        r = self.client.get(reverse('inventory:product_bulk_import'))
        self.assertNotIn(self.wh2, list(r.context['warehouses']))
        r = self._post(xlsx([['صنف و', '', 10, 6, 5, '', 'قطعة', '', 'مخزن الإسكندرية']]), self.w.warehouse)
        self.assertContains(r, 'مخازن غير معروفة')
        self.assertFalse(Product.objects.filter(name='صنف و').exists())

    def test_header_named_warehouse_name_is_not_the_product_name(self):
        head = ['اسم المنتج', 'سعر البيع', 'الكمية', 'اسم المخزن']
        self._post(xlsx([['صنف ز', 10, 6, 'WH-ALX']], head=head), self.w.warehouse)
        self.assertTrue(Product.objects.filter(name='صنف ز').exists())
        self.assertEqual(self.stock('صنف ز', self.wh2), D('6'))

    def test_template_has_warehouse_column_and_list(self):
        r = self.client.get(reverse('inventory:product_template_download'))
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        self.assertIn('المخزن', wb.active['I1'].value)
        names = [c.value for c in wb['المخازن']['A'][1:]]
        self.assertIn('مخزن الإسكندرية', names)



class ProductTaxCodeTests(TestCase):
    """GS1 code from an official barcode; tax codes in a collapsed section of the form."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_is_official_gtin(self):
        from apps.tenant.inventory.models import is_official_gtin
        self.assertTrue(is_official_gtin('5449000000996'))      # Coca-Cola EAN-13
        self.assertTrue(is_official_gtin('96385074'))           # EAN-8
        self.assertFalse(is_official_gtin('2009352861589'))     # in-store (starts with 2)
        self.assertFalse(is_official_gtin('4006381333932'))     # wrong check digit
        self.assertFalse(is_official_gtin('ABC123'))
        self.assertFalse(is_official_gtin('100000000007'))     # demo / generated number

    def test_no_gs1_autofill_egs_only_policy(self):
        p = Product.objects.create(name='كوكاكولا', barcode='5449000000996', category=self.w.category,
                                   sale_price=D('10'))
        self.assertFalse(p.gs1_code)

    def test_form_section_and_code_sentence(self):
        cat = self.w.category
        cat.egs_code, cat.egs_status = 'EG-123456789-CAT1', 'APPROVED'
        cat.save()
        p = self.w.product
        p.gs1_code, p.egs_code, p.egs_status = '', '', ''
        p.save(update_fields=['gs1_code', 'egs_code', 'egs_status'])
        login(self.client, self.w.admin)
        r = self.client.get(reverse('inventory:product_update', args=[p.id]))
        self.assertContains(r, 'أكواد الضرائب (متقدم)')
        self.assertNotContains(r, 'name="gs1_code"')
        self.assertContains(r, 'EG-123456789-CAT1')
