"""إقفال السنة المالية."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import FiscalYearClose, JournalEntry, JournalItem
from apps.tenant.core.models import SystemSetting
from apps.tenant.core.tests.factories import account, build_world, login, make_user


def entry(day, *lines):
    e = JournalEntry.objects.create(date=day, reference=f'FYT-{JournalEntry.objects.count()}', description='اختبار')
    for acc, dr, cr in lines:
        JournalItem.objects.create(entry=e, account=acc, debit=D(dr), credit=D(cr))
    e.post()


class FiscalYearTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)
        cash = self.w.treasury.ensure_account()
        entry(datetime.date(2024, 3, 1), (cash, '5000', '0'), (account('4100'), '0', '5000'))
        entry(datetime.date(2024, 6, 1), (account('5230'), '1200', '0'), (cash, '0', '1200'))
        entry(datetime.date(2025, 2, 1), (cash, '900', '0'), (account('4100'), '0', '900'))

    def test_close_zeroes_pnl_moves_profit_and_locks(self):
        from apps.tenant.accounting.models import Account
        self.client.post(reverse('accounting:fiscal_years'), {'year': '2024'})
        c = FiscalYearClose.objects.get(year=2024)
        self.assertEqual((c.revenue, c.expenses, c.net_profit), (D('5000'), D('1200'), D('3800')))
        self.assertTrue(c.closing_entry.items.filter(account=account('3200'), credit=D('3800')).exists())
        self.assertEqual(SystemSetting.get_settings().accounting_lock_date, datetime.date(2024, 12, 31))
        pnl_2024 = JournalItem.objects.filter(entry__date__year=2024, entry__status='POSTED',
                                              account__account_type__in=[Account.REVENUE, Account.EXPENSE])
        self.assertEqual(sum((i.debit - i.credit for i in pnl_2024), D('0')), D('0'))
        with self.assertRaises(Exception):      # 2024 is locked now
            entry(datetime.date(2024, 7, 1), (account('5230'), '10', '0'), (self.w.treasury.ensure_account(), '0', '10'))

    def test_order_reopen_and_permission(self):
        self.client.post(reverse('accounting:fiscal_years'), {'year': '2025'})
        self.assertFalse(FiscalYearClose.objects.filter(year=2025).exists())       # 2024 must close first
        self.client.post(reverse('accounting:fiscal_years'), {'year': '2024'})
        self.client.post(reverse('accounting:fiscal_years'), {'year': '2024', 'action': 'reopen'})
        self.assertFalse(FiscalYearClose.objects.exists())
        self.assertIsNone(SystemSetting.get_settings().accounting_lock_date)
        r = self.client.get(reverse('accounting:fiscal_years'))
        row = next(x for x in r.context['rows'] if x['year'] == 2024)
        self.assertEqual(row['net_profit'], D('3800'))
        login(self.client, make_user('no_fy', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('accounting:fiscal_years')).status_code, 302)
