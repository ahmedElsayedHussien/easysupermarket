"""قائمة التدفقات النقدية."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.cash_flow import cash_flow
from apps.tenant.accounting.models import JournalEntry, JournalItem
from apps.tenant.core.tests.factories import account, build_world, login, make_bank_account, make_invoice, stock_in


def entry(day, *lines):
    e = JournalEntry.objects.create(date=day, reference=f'CF-{JournalEntry.objects.count()}', description='اختبار')
    for acc, dr, cr in lines:
        JournalItem.objects.create(entry=e, account=acc, debit=D(dr), credit=D(cr))
    e.post()
    return e


class CashFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_sections_internal_transfer_and_reconciliation(self):
        cash = self.w.treasury.ensure_account()
        bank = make_bank_account('بنك').ensure_account()
        d0, d1 = datetime.date(2026, 1, 1), datetime.date(2026, 2, 10)
        entry(d0, (cash, '5000', '0'), (account('3100'), '0', '5000'))                 # capital before the period
        entry(d1, (cash, '1200', '0'), (account('4100'), '0', '1200'))                 # sale
        entry(d1, (account('5230'), '400', '0'), (cash, '0', '400'))                   # rent
        entry(d1, (account('1210'), '700', '0'), (cash, '0', '700'))                   # fixed asset
        entry(d1, (bank, '1000', '0'), (cash, '0', '1000'))                            # treasury -> bank
        r = cash_flow(datetime.date(2026, 2, 1), datetime.date(2026, 2, 28))
        nets = {s['key']: s['net'] for s in r['sections']}
        self.assertEqual(nets, {'operating': D('800'), 'investing': D('-700'), 'financing': D('0')})
        self.assertEqual((r['opening'], r['change'], r['closing']), (D('5000'), D('100'), D('5100')))
        self.assertTrue(r['balanced'])

    def test_screen(self):
        stock_in(self.w.product, self.w.warehouse, 10, D('5'))
        make_invoice(self.w, lines=[(self.w.product, 2, D('10'))])
        login(self.client, self.w.admin)
        r = self.client.get(reverse('core:report_cash_flow'))
        self.assertContains(r, 'قائمة التدفقات النقدية')
        self.assertTrue(r.context['balanced'])
