"""ميزان المراجعة (افتتاحي + حركة = ختامي) وترتيب دفتر الأستاذ."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import JournalEntry, JournalItem
from apps.tenant.core.tests.factories import account, build_world, login


class LedgerReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.cash = cls.w.treasury.ensure_account()
        cls.capital = account('3100')

    def setUp(self):
        login(self.client, self.w.admin)

    def entry(self, date, amount, ref, reverse_side=False):
        e = JournalEntry.objects.create(date=date, reference=ref, description=ref)
        a, b = (self.capital, self.cash) if reverse_side else (self.cash, self.capital)
        JournalItem.objects.create(entry=e, account=a, debit=amount, credit=0)
        JournalItem.objects.create(entry=e, account=b, debit=0, credit=amount)
        e.post()
        return e

    def test_trial_balance_carries_opening_into_closing(self):
        self.entry(datetime.date(2025, 1, 5), D('1000'), 'OLD')             # before the period
        self.entry(datetime.date(2025, 2, 10), D('300'), 'IN')
        self.entry(datetime.date(2025, 2, 20), D('100'), 'OUT', reverse_side=True)
        self.entry(datetime.date(2025, 3, 5), D('999'), 'AFTER')            # after the period: ignored
        r = self.client.get(reverse('core:report_trial_balance'),
                            {'start_date': '2025-02-01', 'end_date': '2025-02-28'})
        rows = {a['code']: a for a in r.context['accounts']}
        cash = rows[self.cash.code]
        self.assertEqual((cash['opening_debit'], cash['debit'], cash['credit'], cash['net_debit']),
                         (D('1000'), D('300'), D('100'), D('1200')))
        cap = rows[self.capital.code]
        self.assertEqual((cap['opening_credit'], cap['net_credit']), (D('1000'), D('1200')))
        self.assertTrue(r.context['is_balanced'])
        self.assertEqual(r.context['total_opening_debit'], r.context['total_opening_credit'])

    def test_general_ledger_running_balance(self):
        self.entry(datetime.date(2025, 1, 5), D('1000'), 'OLD')
        self.entry(datetime.date(2025, 2, 10), D('300'), 'A')
        self.entry(datetime.date(2025, 2, 10), D('50'), 'B', reverse_side=True)
        r = self.client.get(reverse('core:report_general_ledger'),
                            {'account_id': self.cash.pk, 'start_date': '2025-02-01', 'end_date': '2025-02-28'})
        self.assertEqual(r.context['opening_balance'], D('1000'))
        self.assertEqual([t['balance'] for t in r.context['transactions']], [D('1300'), D('1250')])
        self.assertEqual(r.context['final_balance'], D('1250'))
