"""ربط السندات بالفواتير ومتبقي كل فاتورة آجلة."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.allocation_services import open_invoices
from apps.tenant.accounting.models import Voucher
from apps.tenant.core.tests.factories import build_world, login, make_invoice, stock_in


class AllocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        stock_in(cls.w.product, cls.w.warehouse, 100, D('5'))

    def setUp(self):
        login(self.client, self.w.admin)
        import datetime
        self.inv1 = make_invoice(self.w, lines=[(self.w.product, 10, D('10'))], payment_type='CREDIT',
                                 date=datetime.date(2026, 1, 5))     # 100
        self.inv2 = make_invoice(self.w, lines=[(self.w.product, 20, D('10'))], payment_type='CREDIT',
                                 date=datetime.date(2026, 2, 5))     # 200

    def receipt(self, amount, **alloc):
        data = {'amount': amount, 'partner': self.w.customer.id, 'payment_method': 'CASH',
                'treasury': self.w.treasury.id, 'description': 'تحصيل', 'date': '2026-03-01'}
        data.update({f'alloc_{k}': v for k, v in alloc.items()})
        self.client.post(reverse('accounting:receipt_create'), data)
        return Voucher.objects.filter(voucher_type=Voucher.RECEIPT).latest('id')

    def outstanding(self):
        rows = open_invoices([self.w.customer], 'SALE')[self.w.customer.id]['rows']
        return {r['invoice'].id: r['outstanding'] for r in rows}

    def test_unallocated_payment_settles_oldest_first(self):
        self.assertEqual(self.outstanding(), {self.inv1.id: D('100'), self.inv2.id: D('200')})
        v = self.receipt('150')
        self.assertEqual(v.status, Voucher.POSTED)
        self.assertEqual(self.outstanding(), {self.inv2.id: D('150')})   # inv1 paid, 50 on inv2

    def test_allocated_payment_goes_to_chosen_invoice(self):
        v = self.receipt('150', **{str(self.inv2.id): '150'})
        self.assertEqual(v.allocations.get().invoice_id, self.inv2.id)
        self.assertEqual(self.outstanding(), {self.inv1.id: D('100'), self.inv2.id: D('50')})

    def test_allocation_above_open_amount_is_refused_but_voucher_kept(self):
        v = self.receipt('150', **{str(self.inv1.id): '150'})
        self.assertFalse(v.allocations.exists())
        self.assertEqual(self.outstanding(), {self.inv2.id: D('150')})

    def test_cancelled_voucher_reopens_invoice(self):
        v = self.receipt('100', **{str(self.inv1.id): '100'})
        v.cancel_voucher(user=self.w.admin)
        self.assertEqual(self.outstanding(), {self.inv1.id: D('100'), self.inv2.id: D('200')})

    def test_api_invoice_detail_and_aging(self):
        r = self.client.get(reverse('accounting:partner_open_invoices'), {'type': 'RECEIPT', 'partner': self.w.customer.id})
        self.assertEqual(len(r.json()['rows']), 2)
        self.receipt('100', **{str(self.inv1.id): '100'})
        r = self.client.get(reverse('invoicing:invoice_detail', args=[self.inv1.id]))
        self.assertEqual(r.context['credit_status']['outstanding'], D('0'))
        r = self.client.get(reverse('partners:aging_report'))
        row = next(x for x in r.context['rows'] if x['partner'] == self.w.customer)
        self.assertEqual(row['open_total'], D('200'))
