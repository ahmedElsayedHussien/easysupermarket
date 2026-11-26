"""العهد النقدية: صرف، تسوية، باقي يتحول سلفة، صرف زيادة."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import Custody, JournalEntry, JournalItem
from apps.tenant.core.tests.factories import account, build_world, login, make_user


class CustodyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('custody_worker', role='CASHIER', branch=cls.w.branch)
        cls.emp = cls.worker.employee_profile

    def setUp(self):
        login(self.client, self.w.admin)
        e = JournalEntry.objects.create(date=datetime.date(2025, 1, 1), reference='CAP-CUS', description='رأس مال')
        JournalItem.objects.create(entry=e, account=self.w.treasury.ensure_account(), debit=D('5000'), credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=D('5000'))
        e.post()
        from apps.tenant.accounting.custody_services import expense_accounts
        self.exp_acc = expense_accounts().first()

    def give(self, amount='1000'):
        self.client.post(reverse('accounting:custody_list'), {
            'employee': self.emp.id, 'amount': amount, 'treasury': self.w.treasury.id, 'purpose': 'أدوات نظافة'})
        return Custody.objects.latest('id')

    def settle(self, c, lines, returned='0', to_loan=False):
        data = {'exp_account[]': [str(self.exp_acc.id)] * len(lines), 'exp_amount[]': lines,
                'exp_desc[]': [''] * len(lines), 'returned': returned, 'return_treasury': self.w.treasury.id}
        if to_loan:
            data['leftover_to_loan'] = '1'
        return self.client.post(reverse('accounting:custody_detail', args=[c.id]), data)

    def test_payout_and_full_settlement(self):
        c = self.give()
        self.assertTrue(c.payout_entry.items.filter(account=account('1135'), debit=D('1000')).exists())
        self.settle(c, ['700'], returned='300')
        c.refresh_from_db()
        self.assertEqual(c.status, Custody.SETTLED)
        items = c.settlement_entry.items
        self.assertTrue(items.filter(account=self.exp_acc, debit=D('700')).exists())
        self.assertTrue(items.filter(account=account('1135'), credit=D('1000')).exists())

    def test_leftover_must_be_returned_or_become_loan(self):
        from apps.tenant.hr.models import EmployeeLoan
        c = self.give()
        self.settle(c, ['600'])
        c.refresh_from_db()
        self.assertEqual(c.status, Custody.OPEN)                       # 400 unaccounted: refused
        self.settle(c, ['600'], to_loan=True)
        c.refresh_from_db()
        self.assertEqual(c.status, Custody.SETTLED)
        loan = EmployeeLoan.objects.get(employee=self.emp, source=EmployeeLoan.CUSTODY)
        self.assertEqual(loan.amount, D('400.00'))
        self.assertTrue(c.settlement_entry.items.filter(account=account('1135'), credit=D('600')).exists())

    def test_overspend_paid_back_from_treasury(self):
        c = self.give('500')
        self.settle(c, ['650'])
        c.refresh_from_db()
        items = c.settlement_entry.items
        self.assertTrue(items.filter(account=account('1135'), credit=D('500')).exists())
        self.assertTrue(items.filter(account=self.w.treasury.ensure_account(), credit=D('150')).exists())

    def test_cancel_and_permission(self):
        c = self.give()
        self.client.post(reverse('accounting:custody_detail', args=[c.id]), {'action': 'cancel'})
        c.refresh_from_db()
        self.assertEqual(c.status, Custody.CANCELLED)
        self.assertTrue(c.payout_entry.is_reversed)
        login(self.client, make_user('no_custody', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('accounting:custody_list')).status_code, 302)
