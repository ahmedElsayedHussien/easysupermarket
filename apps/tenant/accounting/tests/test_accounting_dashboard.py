import datetime
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.accounting.models import JournalEntry, JournalItem
from apps.tenant.core.tests.factories import account, build_world, login, make_partner


def post_entry(debit_acc, credit_acc, amount, date=None, ref='T'):
    e = JournalEntry.objects.create(date=date or datetime.date.today(), reference=ref, description=ref,
                                    status=JournalEntry.POSTED)
    JournalItem.objects.create(entry=e, account=debit_acc, debit=amount, credit=0)
    JournalItem.objects.create(entry=e, account=credit_acc, debit=0, credit=amount)
    return e


class AccountingDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        self.w.treasury.ensure_account()
        self.w.treasury.refresh_from_db()
        cash = self.w.treasury.account
        post_entry(cash, account('4100'), D('1000'), ref='SALE-1')         # revenue 1000
        post_entry(account('4120'), cash, D('100'), ref='RET-1')           # sales return -100
        post_entry(account('5230'), cash, D('300'), ref='RENT-1')          # rent 300
        post_entry(account('5240'), cash, D('50'), ref='UTIL-1')           # utilities 50

    def _get(self):
        return login(self.client, self.w.admin).get('/accounting/dashboard/')

    def test_month_pl(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['revenue_month'], D('900'))     # 1000 - 100 returns
        self.assertEqual(r.context['expenses_month'], D('350'))
        self.assertEqual(r.context['net_month'], D('550'))
        self.assertEqual(r.context['trend']['net'][-1], 550.0)
        self.assertEqual(r.context['ledger_diff'], D('0'))

    def test_expense_breakdown(self):
        bars = self._get().context['expense_bars']
        self.assertEqual(bars[0]['code'], '5230')
        self.assertAlmostEqual(sum(b['share'] for b in bars), 100.0, places=3)

    def test_due_cheques_alert(self):
        from apps.tenant.accounting.models import Cheque
        customer = make_partner('عميل شيكات', 'CUSTOMER')
        Cheque.objects.bulk_create([
            Cheque(cheque_type='RECEIVABLE', cheque_number='A1', amount=D('500'), partner=customer, status='UNDER_COLLECTION',
                   received_date=datetime.date.today(), due_date=datetime.date.today() + datetime.timedelta(days=3)),
            Cheque(cheque_type='RECEIVABLE', cheque_number='A2', amount=D('200'), partner=customer, status='UNDER_COLLECTION',
                   received_date=datetime.date.today(), due_date=datetime.date.today() - datetime.timedelta(days=2)),
        ])
        r = self._get()
        self.assertEqual(r.context['cheques_in']['soon_n'], 1)
        self.assertEqual(r.context['cheques_in']['over_n'], 1)
        self.assertEqual(len(r.context['upcoming_cheques']), 2)

    def test_cashier_without_permission_redirected(self):
        self.assertEqual(login(self.client, self.w.cashier).get('/accounting/dashboard/').status_code, 302)

    def test_sidebar_single_accounting_link(self):
        html = self._get().content.decode()
        self.assertIn('لوحة المحاسبة', html)
        for old in ('/accounting/expenses/" class="nav-link', '/accounting/cheques/" class="nav-link',
                    '/accounting/journals/" class="nav-link'):
            self.assertNotIn(old, html)
