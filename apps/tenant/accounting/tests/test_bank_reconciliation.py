"""التسوية البنكية: الفرق، الإنهاء بفرق صفر، والحركات المفتوحة للتسوية الجاية."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import BankReconciliation, JournalEntry, JournalItem
from apps.tenant.core.tests.factories import account, build_world, login, make_bank_account, make_user


class BankReconciliationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.bank = make_bank_account('بنك مصر')

    def setUp(self):
        login(self.client, self.w.admin)
        acc = self.bank.ensure_account()
        self.moves = []
        for day, dr, cr in [(1, D('1000'), D('0')), (5, D('0'), D('300')), (9, D('500'), D('0'))]:
            e = JournalEntry.objects.create(date=datetime.date(2026, 3, day), reference=f'BNK-{day}', description='حركة بنك')
            self.moves.append(JournalItem.objects.create(entry=e, account=acc, debit=dr, credit=cr))
            JournalItem.objects.create(entry=e, account=account('3100'), debit=cr, credit=dr)
            e.post()

    def start(self, balance, date='2026-03-31'):
        self.client.post(reverse('accounting:bank_reconciliation_list'), {
            'bank': self.bank.id, 'statement_date': date, 'statement_balance': balance})
        return BankReconciliation.objects.latest('id')

    def test_finish_only_with_zero_difference_and_open_items_carry_over(self):
        rec = self.start('700')                       # the bank saw +1000 and -300, not the +500 yet
        url = reverse('accounting:bank_reconciliation_detail', args=[rec.id])
        self.client.post(url, {'action': 'finish', 'items': [self.moves[0].id]})
        rec.refresh_from_db()
        self.assertEqual((rec.status, rec.difference), (BankReconciliation.DRAFT, D('-300')))
        self.client.post(url, {'action': 'finish', 'items': [self.moves[0].id, self.moves[1].id]})
        rec.refresh_from_db()
        self.assertEqual((rec.status, rec.difference, rec.book_balance), (BankReconciliation.DONE, D('0'), D('1200')))
        rec2 = self.start('1200', '2026-04-30')
        r = self.client.get(reverse('accounting:bank_reconciliation_detail', args=[rec2.id]))
        self.assertEqual([row['item'].id for row in r.context['snap']['rows']], [self.moves[2].id])
        self.assertEqual(r.context['snap']['previous_cleared'], D('700'))

    def test_one_draft_per_bank_reopen_and_permission(self):
        rec = self.start('700')
        self.start('700')
        self.assertEqual(BankReconciliation.objects.count(), 1)
        url = reverse('accounting:bank_reconciliation_detail', args=[rec.id])
        self.client.post(url, {'action': 'finish', 'items': [self.moves[0].id, self.moves[1].id]})
        self.client.post(url, {'action': 'reopen'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, BankReconciliation.DRAFT)
        login(self.client, make_user('no_bankrec', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('accounting:bank_reconciliation_list')).status_code, 302)
