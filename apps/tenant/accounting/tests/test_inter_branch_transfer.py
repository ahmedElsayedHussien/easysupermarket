"""
Two-step inter-branch transfers:
send (source -> 1195 cash in transit) then receive at the other branch
(1195 -> destination, shortfall -> 5400), or reject (money back to source).
"""
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.accounting.models import InternalTransfer
from apps.tenant.core.tests.factories import (
    account, build_world, ledger_totals, login, make_branch, make_treasury, make_user,
)


def balance(treasury):
    treasury.refresh_from_db()
    return treasury.account.current_balance


class InterBranchTransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.treasury2 = make_treasury(cls.branch2, 'خزينة 2', opening_balance=D('0'))
        cls.cashier2 = make_user('cashier2', role='CASHIER', branch=cls.branch2,
                                 perms=('accounting.view_internaltransfer', 'accounting.change_internaltransfer'))
        cls.sender = make_user('sender1', role='CASHIER', branch=cls.w.branch,
                               perms=('accounting.view_internaltransfer', 'accounting.change_internaltransfer',
                                      'accounting.add_internaltransfer'))

    def setUp(self):
        self.w.treasury.ensure_account()
        self.treasury2.ensure_account()
        # 1000 cash in the main branch treasury
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        import datetime
        e = JournalEntry.objects.create(date=datetime.date.today(), reference='SEED-CASH', description='seed', status=JournalEntry.POSTED)
        JournalItem.objects.create(entry=e, account=self.w.treasury.account, debit=D('1000'), credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=D('1000'))
        e.post()
        self.t = InternalTransfer.objects.create(
            amount=D('300'), from_account_type='TREASURY', from_treasury=self.w.treasury,
            to_account_type='TREASURY', to_treasury=self.treasury2, created_by=self.sender,
        )

    def _send(self):
        c = login(self.client, self.sender)
        c.post(f'/accounting/transfers/{self.t.pk}/confirm/')
        self.t.refresh_from_db()

    def test_inter_branch_detected(self):
        self.assertTrue(self.t.is_inter_branch)

    def test_send_moves_money_to_transit(self):
        self._send()
        self.assertEqual(self.t.status, InternalTransfer.IN_TRANSIT)
        self.assertEqual(balance(self.w.treasury), D('700'))
        self.assertEqual(balance(self.treasury2), D('0'))            # not received yet
        self.assertEqual(account('1195').current_balance, D('300'))
        self.assertEqual(self.t.sent_by, self.sender)

    def test_receive_full_amount(self):
        self._send()
        c = login(self.client, self.cashier2)
        c.post(f'/accounting/transfers/{self.t.pk}/receive/', {'received_amount': '300'})
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, InternalTransfer.POSTED)
        self.assertEqual(balance(self.treasury2), D('300'))
        self.assertEqual(account('1195').current_balance, D('0'))
        self.assertEqual(self.t.received_by, self.cashier2)
        d, cr = ledger_totals()
        self.assertEqual(d, cr)

    def test_receive_with_shortage(self):
        self._send()
        shortage_before = account('5400').current_balance
        c = login(self.client, self.cashier2)
        c.post(f'/accounting/transfers/{self.t.pk}/receive/', {'received_amount': '280', 'receive_note': 'ناقص 20'})
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, InternalTransfer.POSTED)
        self.assertEqual(balance(self.treasury2), D('280'))
        self.assertEqual(account('5400').current_balance - shortage_before, D('20'))
        self.assertEqual(account('1195').current_balance, D('0'))

    def test_cannot_receive_more_than_sent(self):
        self._send()
        c = login(self.client, self.cashier2)
        c.post(f'/accounting/transfers/{self.t.pk}/receive/', {'received_amount': '301'})
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, InternalTransfer.IN_TRANSIT)

    def test_reject_returns_money_to_sender(self):
        self._send()
        c = login(self.client, self.cashier2)
        c.post(f'/accounting/transfers/{self.t.pk}/reject/', {'receive_note': 'لم يصل'})
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, InternalTransfer.REJECTED)
        self.assertEqual(balance(self.w.treasury), D('1000'))
        self.assertEqual(account('1195').current_balance, D('0'))

    def test_reject_requires_reason(self):
        self._send()
        c = login(self.client, self.cashier2)
        c.post(f'/accounting/transfers/{self.t.pk}/reject/', {'receive_note': ''})
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, InternalTransfer.IN_TRANSIT)

    def test_sender_branch_cannot_receive(self):
        self._send()
        c = login(self.client, self.sender)
        r = c.post(f'/accounting/transfers/{self.t.pk}/receive/', {'received_amount': '300'})
        self.assertEqual(r.status_code, 404)
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, InternalTransfer.IN_TRANSIT)

    def test_receiving_branch_cannot_send(self):
        c = login(self.client, self.cashier2)
        c.post(f'/accounting/transfers/{self.t.pk}/confirm/')
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, InternalTransfer.DRAFT)

    def test_incoming_list_shows_pending_for_receiver_only(self):
        self._send()
        c = login(self.client, self.cashier2)
        r = c.get('/accounting/transfers/?incoming=1')
        self.assertEqual([t.pk for t in r.context['transfers']], [self.t.pk])
        self.assertEqual(r.context['incoming_count'], 1)
        c = login(self.client, self.sender)
        r = c.get('/accounting/transfers/?incoming=1')
        self.assertEqual(r.context['incoming_count'], 0)

    def test_sender_can_cancel_in_transit(self):
        self._send()
        c = login(self.client, self.sender)
        c.post(f'/accounting/transfers/{self.t.pk}/cancel/')
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, InternalTransfer.CANCELLED)
        self.assertEqual(balance(self.w.treasury), D('1000'))

    def test_cancel_after_receipt_reverses_both_steps(self):
        self._send()
        login(self.client, self.cashier2).post(f'/accounting/transfers/{self.t.pk}/receive/', {'received_amount': '300'})
        self.t.refresh_from_db()
        self.t.cancel_transfer(user=self.w.admin)
        self.assertEqual(balance(self.w.treasury), D('1000'))
        self.assertEqual(balance(self.treasury2), D('0'))
        self.assertEqual(account('1195').current_balance, D('0'))

    def test_same_branch_transfer_stays_single_step(self):
        t3 = make_treasury(self.w.branch, 'خزينة فرعية')
        t = InternalTransfer.objects.create(
            amount=D('100'), from_account_type='TREASURY', from_treasury=self.w.treasury,
            to_account_type='TREASURY', to_treasury=t3, created_by=self.sender,
        )
        self.assertFalse(t.is_inter_branch)
        t.confirm_transfer(user=self.sender)
        self.assertEqual(t.status, InternalTransfer.POSTED)

    def test_cashier_can_pick_other_branch_as_destination(self):
        from apps.tenant.accounting.forms import InternalTransferForm
        c = login(self.client, self.sender)
        rq = c.get('/').wsgi_request
        f = InternalTransferForm(user=rq.user)
        self.assertIn(self.treasury2.id, set(f.fields['to_treasury'].queryset.values_list('id', flat=True)))
        self.assertEqual(set(f.fields['from_treasury'].queryset.values_list('id', flat=True)), {self.w.treasury.id})

    def test_detail_page_shows_receive_form_to_receiver_only(self):
        self._send()
        r = login(self.client, self.cashier2).get(f'/accounting/transfers/{self.t.pk}/')
        self.assertContains(r, 'تأكيد الاستلام')
        r = login(self.client, self.sender).get(f'/accounting/transfers/{self.t.pk}/')
        self.assertNotContains(r, 'تأكيد الاستلام')
        self.assertContains(r, 'بالطريق')

