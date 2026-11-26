"""باركودات متعددة للصنف: البحث بأي باركود، ومنع التكرار."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_product
from apps.tenant.inventory.barcodes import find_by_barcode
from apps.tenant.inventory.models import ProductBarcode, ProductUoM, UnitOfMeasure


class ExtraBarcodeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.p = make_product(cls.w.category, 'شاي ليبتون 100 فتلة', sale_price=D('60'), barcode='6221000000017')
        box, _ = UnitOfMeasure.objects.get_or_create(name='كرتونة')
        cls.box = ProductUoM.objects.create(product=cls.p, uom=box, conversion_factor=D('12'), barcode='6221000000024')

    def setUp(self):
        login(self.client, self.w.admin)

    def test_find_by_any_barcode(self):
        ProductBarcode.objects.create(product=self.p, barcode='5000000000011', note='مورد تاني')
        ProductBarcode.objects.create(product=self.p, barcode='5000000000028', uom=self.box)
        self.assertEqual(find_by_barcode('6221000000017'), (self.p, None))
        self.assertEqual(find_by_barcode('6221000000024'), (self.p, self.box))
        self.assertEqual(find_by_barcode('5000000000011'), (self.p, None))
        self.assertEqual(find_by_barcode('5000000000028'), (self.p, self.box))
        self.assertEqual(find_by_barcode('999'), (None, None))

    def test_pos_scan_extra_barcode(self):
        ProductBarcode.objects.create(product=self.p, barcode='5000000000028', uom=self.box)
        r = self.client.get(reverse('invoicing:product_by_barcode'), {'barcode': '5000000000028',
                                                                      'warehouse_id': self.w.warehouse.id})
        data = r.json()
        self.assertTrue(data['found'])
        self.assertEqual(data['product']['id'], self.p.id)
        self.assertEqual(str(data['product']['selected_uom_id']), str(self.box.id))

    def post_form(self, extra, extra_uoms=None, barcode='6221000000017'):
        data = {
            'name': self.p.name, 'product_type': self.p.product_type, 'barcode': barcode,
            'category': self.w.category.id, 'sale_price': '60', 'min_sale_price': '0', 'tax_rate': '14',
            'withholding_tax_rate': '0', 'min_stock_level': '0', 'max_stock_level': '0', 'is_active': 'on',
            'uoms-TOTAL_FORMS': '0', 'uoms-INITIAL_FORMS': '0', 'uoms-MIN_NUM_FORMS': '0', 'uoms-MAX_NUM_FORMS': '1000',
            'extra_barcode[]': extra, 'extra_barcode_uom[]': extra_uoms or [''] * len(extra),
            'extra_barcode_note[]': [''] * len(extra),
        }
        return self.client.post(reverse('inventory:product_update', args=[self.p.id]), data)

    def test_form_saves_and_rejects_duplicates(self):
        other = make_product(self.w.category, 'شاي العروسة', barcode='6223000000015')
        r = self.post_form(['5000000000011', '6223000000015'])           # other product's barcode
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'مستخدم بالفعل')
        self.assertFalse(ProductBarcode.objects.exists())
        r = self.post_form(['5000000000011', '5000000000028'], ['', str(self.box.id)])
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        self.assertEqual(set(self.p.extra_barcodes.values_list('barcode', flat=True)), {'5000000000011', '5000000000028'})
        self.assertEqual(self.p.extra_barcodes.get(barcode='5000000000028').uom, self.box)
        self.post_form(['5000000000011'])                                    # removing a row deletes it
        self.assertEqual(self.p.extra_barcodes.count(), 1)
        # the other product cannot take an extra barcode as its main barcode
        r = self.client.get(reverse('inventory:product_list'), {'q': '5000000000011'})
        self.assertContains(r, self.p.name)
        from apps.tenant.inventory.barcodes import validate_main_barcode
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            validate_main_barcode('5000000000011', product_id=other.id)


class StocktakeSheetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_blind_sheet_hides_stock_full_shows_it(self):
        from apps.tenant.core.tests.factories import stock_in
        stock_in(self.w.product, self.w.warehouse, 37, D('5'))
        login(self.client, self.w.admin)
        url = reverse('inventory:stocktake_sheet')
        r = self.client.get(url, {'warehouse': self.w.warehouse.id})
        self.assertTrue(r.context['blind'])
        self.assertContains(r, self.w.product.name)
        self.assertNotContains(r, '<th style="width:12%;">الرصيد</th>')
        r = self.client.get(url, {'warehouse': self.w.warehouse.id, 'mode': 'full'})
        row = next(x for x in r.context['rows'] if x['product'] == self.w.product)
        self.assertEqual(row['stock'], D('37'))
