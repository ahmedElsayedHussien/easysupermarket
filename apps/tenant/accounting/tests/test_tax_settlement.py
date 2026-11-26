"""التسوية الضريبية: إقفال القيمة المضافة وسداد المستحق."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import JournalEntry, JournalItem, TaxSettlement
from apps.tenant.accounting.tax_settlement import position
from apps.tenant.core.tests.factories import account, build_world, login, make_user


def entry(day, *lines):
    e = JournalEntry.objects.create(date=day, reference=f'TX-{JournalEntry.objects.count()}', description='اختبار')
    for acc, dr, cr in lines:
        JournalItem.objects.create(entry=e, account=acc, debit=D(dr), credit=D(cr))
    e.post()


class TaxSettlementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)
        cash = self.w.treasury.ensure_account()
        d = datetime.date(2026, 1, 15)
        entry(d, (cash, '10000', '0'), (account('3100'), '0', '10000'))
        entry(d, (cash, '1140', '0'), (account('4100'), '0', '1000'), (account('2130'), '0', '140'))   # sale + VAT
        entry(d, (account('1140'), '500', '0'), (account('1190'), '70', '0'), (cash, '0', '570'))      # purchase + VAT

    def test_close_and_pay(self):
        self.client.post(reverse('accounting:tax_settlement'), {'action': 'close_vat', 'period_end': '2026-01-31'})
        t = TaxSettlement.objects.get(kind=TaxSettlement.VAT_CLOSE)
        self.assertEqual((t.output_vat, t.input_vat, t.amount), (D('140'), D('70'), D('70')))
        p = position(datetime.date(2026, 2, 1))
        self.assertEqual((p['output_vat'], p['input_vat'], p['vat_payable']), (D('0'), D('0'), D('70')))
        self.client.post(reverse('accounting:tax_settlement'), {'action': 'vat_pay', 'amount': '70',
                                                                'source': f't:{self.w.treasury.id}'})
        pay = TaxSettlement.objects.get(kind=TaxSettlement.VAT_PAY)
        self.assertTrue(pay.journal_entry.items.filter(account=account('2135'), debit=D('70')).exists())
        self.assertEqual(position()['vat_payable'], D('0'))

    def test_input_bigger_is_carried_forward(self):
        entry(datetime.date(2026, 1, 20), (account('1190'), '200', '0'), (self.w.treasury.ensure_account(), '0', '200'))
        self.client.post(reverse('accounting:tax_settlement'), {'action': 'close_vat', 'period_end': '2026-01-31'})
        t = TaxSettlement.objects.get(kind=TaxSettlement.VAT_CLOSE)
        self.assertEqual(t.amount, D('0'))
        p = position(datetime.date(2026, 2, 1))
        self.assertEqual((p['output_vat'], p['input_vat']), (D('0'), D('130')))     # 270 - 140 carried

    def test_overpay_refused_and_permission(self):
        self.client.post(reverse('accounting:tax_settlement'), {'action': 'vat_pay', 'amount': '50',
                                                                'source': f't:{self.w.treasury.id}'})
        self.assertFalse(TaxSettlement.objects.filter(kind=TaxSettlement.VAT_PAY).exists())   # nothing payable yet
        login(self.client, make_user('no_tax', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('accounting:tax_settlement')).status_code, 302)
