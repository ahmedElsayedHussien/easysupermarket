"""تعديل رصيد أول المدة من شاشة الصنف: الصنف يفضل ظاهر في شاشة الأصناف ونقطة البيع."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_product
from apps.tenant.inventory.models import Product


class OpeningEditVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.p = make_product(cls.w.category, 'زيت كريستال 1 لتر', sale_price=D('80'), barcode='6221000000901')

    def setUp(self):
        login(self.client, self.w.admin)

    def edit(self, qty, cost='50'):
        data = {
            'name': self.p.name, 'product_type': self.p.product_type, 'barcode': '6221000000901',
            'category': self.w.category.id, 'sale_price': '80', 'min_sale_price': '0', 'tax_rate': '14',
            'withholding_tax_rate': '0', 'min_stock_level': '0', 'max_stock_level': '0', 'is_active': 'on',
            'uoms-TOTAL_FORMS': '0', 'uoms-INITIAL_FORMS': '0', 'uoms-MIN_NUM_FORMS': '0', 'uoms-MAX_NUM_FORMS': '1000',
            'opening_quantity': qty, 'opening_unit_cost': cost, 'opening_warehouse': self.w.warehouse.id,
        }
        return self.client.post(reverse('inventory:product_update', args=[self.p.id]), data)

    def assert_visible(self):
        p = Product.objects.get(pk=self.p.pk)
        self.assertTrue(p.is_active)
        self.assertContains(self.client.get(reverse('inventory:product_list')), self.p.name)
        r = self.client.get(reverse('invoicing:product_by_barcode'), {'barcode': '6221000000901',
                                                                      'warehouse_id': self.w.warehouse.id})
        self.assertTrue(r.json().get('found'), r.content[:300])

    def test_create_then_change_opening(self):
        r = self.edit('10')
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        self.assert_visible()
        self.assertEqual(self.p.get_stock(self.w.warehouse), D('10'))
        r = self.edit('25', '55')
        self.assertEqual(r.status_code, 302)
        self.assert_visible()
        self.assertEqual(self.p.get_stock(self.w.warehouse), D('25'))
        r = self.edit('5')
        self.assert_visible()
        self.assertEqual(self.p.get_stock(self.w.warehouse), D('5'))

    def test_opening_list_shows_value_and_edit_page_keeps_product(self):
        self.edit('10', '50')
        r = self.client.get(reverse('inventory:opening_stock_list'))
        self.assertContains(r, '500')                                     # 10 x 50 (was empty)
        r = self.client.post(reverse('inventory:product_opening_stock', args=[self.p.id]),
                             {'warehouse': self.w.warehouse.id, 'quantity': '30', 'unit_cost': '50', 'reason': 'x'})
        self.assertEqual(r.status_code, 302)
        self.assert_visible()
        self.assertEqual(self.p.get_stock(self.w.warehouse), D('30'))

    def test_edits_keep_the_stock_card_right(self):
        from django.db.models import Sum
        from apps.tenant.inventory.models import StockMovement
        from apps.tenant.services.opening_stock_service import update_or_create_product_opening_stock as upd
        upd(self.p, self.w.warehouse, D('10'), D('50'), user=self.w.admin)
        upd(self.p, self.w.warehouse, D('6'), D('50'), user=self.w.admin)      # 10 -> 6
        n = StockMovement.objects.filter(product=self.p).count()
        upd(self.p, self.w.warehouse, D('6'), D('70'), user=self.w.admin)      # cost only
        self.assertEqual(StockMovement.objects.filter(product=self.p).count(), n)   # no quantity movement
        mv = StockMovement.objects.filter(product=self.p, warehouse=self.w.warehouse)
        ins = mv.filter(movement_type__in=['OPENING_BALANCE', 'OPENING_ADJ_IN']).aggregate(s=Sum('quantity'))['s']
        outs = mv.filter(movement_type='OPENING_ADJ_OUT').aggregate(s=Sum('quantity'))['s'] or 0
        self.assertEqual(ins - outs, D('6'))                                     # the card = the stock
        self.assertEqual(self.p.get_stock(self.w.warehouse), D('6'))

    def test_serialized_refused(self):
        from django.core.exceptions import ValidationError
        from apps.tenant.services.opening_stock_service import update_or_create_product_opening_stock as upd
        phone = make_product(self.w.category, 'موبايل تجربة', barcode='6221000000918', product_type='SERIALIZED')
        with self.assertRaisesMessage(ValidationError, 'السيريال'):
            upd(phone, self.w.warehouse, D('3'), D('5000'), user=self.w.admin)

    def test_opening_edit_is_not_wastage_and_card_is_right(self):
        from apps.tenant.services.opening_stock_service import update_or_create_product_opening_stock as upd
        upd(self.p, self.w.warehouse, D('10'), D('50'), user=self.w.admin)
        upd(self.p, self.w.warehouse, D('6'), D('50'), user=self.w.admin)       # 10 -> 6
        r = self.client.get(reverse('core:report_wastage'))
        self.assertEqual(r.context['by_product'], [])
        self.assertEqual(r.context['totals']['lost'], 0)
        r = self.client.get(reverse('core:report_item_movement'), {'product_id': self.p.id})
        self.assertEqual(r.context['closing_balance'], D('6'))

    def test_cancel_after_cost_edit_keeps_stock_value_equal_to_1140(self):
        from django.db.models import F, Sum
        from apps.tenant.core.tests.factories import account, assertInventoryMatchesLedger, make_invoice
        from apps.tenant.inventory.models import InventoryBatch
        from apps.tenant.services.invoice_service import cancel_invoice
        from apps.tenant.services.opening_stock_service import update_or_create_product_opening_stock as upd
        upd(self.p, self.w.warehouse, D('10'), D('50'), user=self.w.admin)
        sale = make_invoice(self.w, lines=[(self.p, 4, 80)])                    # 4 leave at 50
        upd(self.p, self.w.warehouse, D('10'), D('70'), user=self.w.admin)      # cost edit: 6 left at 70
        cancel_invoice(sale.id, self.w.admin)
        stock_value = InventoryBatch.objects.filter(product=self.p).aggregate(
            v=Sum(F('quantity_remaining') * F('unit_cost')))['v']
        inv = account('1140')
        ledger = sum((i.debit - i.credit for i in inv.journal_items.filter(entry__status='POSTED')), D('0'))
        others = sum((b.quantity_remaining * b.unit_cost for b in InventoryBatch.objects.exclude(product=self.p)), D('0'))
        self.assertEqual(stock_value, D('620'))                                  # 6 x 70 + 4 x 50
        self.assertEqual(ledger - others, stock_value)
        assertInventoryMatchesLedger(self)

    def test_opening_info_never_takes_another_warehouse_entry(self):
        from django.contrib.contenttypes.models import ContentType
        from apps.tenant.accounting.models import JournalEntry
        from apps.tenant.services.opening_stock_service import (get_product_opening_info,
                                                                update_or_create_product_opening_stock as upd)
        w1 = self.w.warehouse
        upd(self.p, w1, D('5'), D('10'), user=self.w.admin)
        # a newer entry of "warehouse <w1.id>1" (e.g. 1 -> 11): its reference starts with ours
        JournalEntry.objects.create(date=__import__('datetime').date.today(), reference=f'OPN-PRD-{self.p.id}-{w1.id}1',
                                    description='مخزن تاني', content_type=ContentType.objects.get_for_model(self.p),
                                    object_id=self.p.id)
        info = get_product_opening_info(self.p, w1)
        self.assertEqual(info['journal_entry'].reference, f'OPN-PRD-{self.p.id}-{w1.id}')
