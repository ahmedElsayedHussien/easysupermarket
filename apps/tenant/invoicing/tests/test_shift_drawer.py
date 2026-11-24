"""Cash drawer of a POS shift: opening from the treasury, one drawer per cashier,
closing difference booked to the ledger, cash drop at close."""
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import InternalTransfer, JournalEntry, JournalItem
from apps.tenant.core.tests.factories import (
    account, build_world, login, make_bank_account, make_branch, make_treasury, make_user,
)
from apps.tenant.invoicing.models import POSSession
from apps.tenant.invoicing.shift_services import treasury_book_balance


def put_cash(treasury, amount):
    """Posts cash into the drawer (Dr treasury / Cr capital)."""
    acc = treasury.ensure_account()
    e = JournalEntry.objects.create(date='2026-01-01', reference=f'CASH-{treasury.pk}-{amount}',
                                    description='cash', status=JournalEntry.DRAFT)
    JournalItem.objects.create(entry=e, account=acc, debit=amount, credit=0)
    JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=amount)
    e.post()


class ShiftDrawerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        put_cash(cls.w.treasury, D('3000'))

    def _open(self, user, treasury=None):
        c = login(self.client, user)
        return c, c.post(reverse('invoicing:pos_open_shift'), {'treasury_id': (treasury or self.w.treasury).pk})

    def _close(self, c, session, actual, **extra):
        return c.post(reverse('invoicing:pos_close_shift'),
                      {'session_id': session.pk, 'actual_balance': str(actual), **extra})

    # --- opening -------------------------------------------------------------
    def test_opening_balance_is_treasury_book_balance(self):
        c, r = self._open(self.w.cashier)
        self.assertEqual(r.status_code, 302)
        s = POSSession.objects.get(user=self.w.cashier, status=POSSession.OPEN)
        self.assertEqual(s.opening_balance, D('3000'))

    def test_opening_balance_posted_value_is_ignored(self):
        c = login(self.client, self.w.cashier)
        c.post(reverse('invoicing:pos_open_shift'), {'treasury_id': self.w.treasury.pk, 'opening_balance': '0'})
        self.assertEqual(POSSession.objects.get(user=self.w.cashier).opening_balance, D('3000'))

    def test_one_drawer_one_cashier(self):
        POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        c, r = self._open(self.w.cashier)
        self.assertFalse(POSSession.objects.filter(user=self.w.cashier).exists())
        page = c.get(reverse('invoicing:pos_open_shift'))
        self.assertContains(page, 'مشغول')

    def test_other_drawer_still_allowed(self):
        other = make_treasury(self.w.branch, 'درج 2')
        POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        self._open(self.w.cashier, other)
        self.assertTrue(POSSession.objects.filter(user=self.w.cashier, treasury=other, status='OPEN').exists())

    # --- closing variance ----------------------------------------------------
    def test_matching_close_posts_nothing(self):
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self._close(c, s, '3000')
        s.refresh_from_db()
        self.assertEqual(s.status, 'CLOSED')
        self.assertIsNone(s.variance_entry)
        self.assertEqual(treasury_book_balance(self.w.treasury), D('3000'))

    def test_shortage_by_cashier_goes_to_expense(self):
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        before = account('5400').current_balance
        # a cashier can't choose to charge it to himself/others: ignored
        self._close(c, s, '2950', shortage_charge_to='CASHIER')
        s.refresh_from_db()
        self.assertEqual(s.difference, D('-50'))
        self.assertEqual(s.variance_charged_to, 'EXPENSE')
        self.assertEqual(account('5400').current_balance - before, D('50'))
        # book = physical cash -> next shift opens on 2950
        self.assertEqual(treasury_book_balance(self.w.treasury), D('2950'))
        _, r = self._open(self.w.cashier)
        self.assertEqual(POSSession.objects.get(user=self.w.cashier, status='OPEN').opening_balance, D('2950'))

    def test_admin_charges_shortage_to_cashier_at_close(self):
        s = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.cashier,
                                      opening_balance=D('3000'))
        c = login(self.client, self.w.admin)
        before = account('1135').current_balance
        self._close(c, s, '2900', shortage_charge_to='CASHIER')
        s.refresh_from_db()
        self.assertEqual(s.variance_charged_to, 'CASHIER')
        self.assertEqual(account('1135').current_balance - before, D('100'))
        self.assertEqual(treasury_book_balance(self.w.treasury), D('2900'))

    def test_overage_credits_variance_account(self):
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        before = account('5400').current_balance
        self._close(c, s, '3020')
        self.assertEqual(account('5400').current_balance - before, D('-20'))
        self.assertEqual(treasury_book_balance(self.w.treasury), D('3020'))

    def test_admin_recharges_expense_shortage_to_cashier_later(self):
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self._close(c, s, '2970')
        exp_before, emp_before = account('5400').current_balance, account('1135').current_balance
        a = login(self.client, self.w.admin)
        r = a.post(reverse('invoicing:pos_shift_charge_cashier', args=[s.pk]))
        self.assertEqual(r.status_code, 302)
        s.refresh_from_db()
        self.assertEqual(s.variance_charged_to, 'CASHIER')
        self.assertEqual(account('5400').current_balance - exp_before, D('-30'))
        self.assertEqual(account('1135').current_balance - emp_before, D('30'))
        self.assertEqual(treasury_book_balance(self.w.treasury), D('2970'))
        # twice -> refused, nothing changes
        a.post(reverse('invoicing:pos_shift_charge_cashier', args=[s.pk]))
        self.assertEqual(account('1135').current_balance - emp_before, D('30'))

    def test_cashier_cannot_recharge(self):
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self._close(c, s, '2970')
        c.post(reverse('invoicing:pos_shift_charge_cashier', args=[s.pk]))
        s.refresh_from_db()
        self.assertEqual(s.variance_charged_to, 'EXPENSE')

    # --- cash drop at close --------------------------------------------------
    def test_deposit_to_main_treasury_same_branch(self):
        main = make_treasury(self.w.branch, 'الخزينة الرئيسية')
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self._close(c, s, '3000', deposit_type='TREASURY', deposit_target=main.pk, deposit_amount='2500')
        s.refresh_from_db()
        self.assertEqual(s.status, 'CLOSED')
        tr = InternalTransfer.objects.get(pos_session=s)
        self.assertEqual(tr.status, InternalTransfer.POSTED)
        self.assertEqual(treasury_book_balance(self.w.treasury), D('500'))
        self.assertEqual(treasury_book_balance(main), D('2500'))

    def test_deposit_to_bank(self):
        bank = make_bank_account('بنك مصر')
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self._close(c, s, '2990', deposit_type='BANK', deposit_target=bank.pk, deposit_amount='2000')
        self.assertEqual(treasury_book_balance(self.w.treasury), D('990'))  # after the 10 shortage

    def test_deposit_to_other_branch_is_in_transit(self):
        other = make_treasury(make_branch('فرع 2'), 'خزينة فرع 2')
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self._close(c, s, '3000', deposit_type='TREASURY', deposit_target=other.pk, deposit_amount='1000')
        self.assertEqual(InternalTransfer.objects.get(pos_session=s).status, InternalTransfer.IN_TRANSIT)
        self.assertEqual(treasury_book_balance(self.w.treasury), D('2000'))

    def test_deposit_more_than_drawer_rolls_back_close(self):
        main = make_treasury(self.w.branch, 'الخزينة الرئيسية')
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self._close(c, s, '3000', deposit_type='TREASURY', deposit_target=main.pk, deposit_amount='5000')
        s.refresh_from_db()
        self.assertEqual(s.status, 'OPEN')
        self.assertFalse(InternalTransfer.objects.exists())

    def test_close_page_renders_options(self):
        s = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.cashier)
        r = login(self.client, self.w.admin).get(f"{reverse('invoicing:pos_close_shift')}?session_id={s.pk}")
        self.assertContains(r, 'توريد نقدية من الدرج')
        self.assertContains(r, 'shortage_charge_to')
        r = login(self.client, self.w.cashier).get(f"{reverse('invoicing:pos_close_shift')}?session_id={s.pk}")
        self.assertNotContains(r, 'name="shortage_charge_to"')

    def test_shift_report_shows_variance(self):
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self._close(c, s, '2970')
        r = login(self.client, self.w.admin).get(reverse('core:report_shift_detail', args=[s.pk]))
        self.assertContains(r, 'مقيد في الحسابات')
        self.assertContains(r, 'تحميل العجز على الكاشير')

    def test_mid_shift_cash_drop_is_not_a_shortage(self):
        main = make_treasury(self.w.branch, 'الخزينة الرئيسية')
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        tr = InternalTransfer.objects.create(amount=D('1000'), from_account_type='TREASURY', from_treasury=self.w.treasury,
                                             to_account_type='TREASURY', to_treasury=main)
        tr.confirm_transfer(user=self.w.admin)
        r = c.get(f"{reverse('invoicing:pos_close_shift')}?session_id={s.pk}")
        self.assertEqual(r.context['expected_balance'], D('2000'))
        self._close(c, s, '2000')
        s.refresh_from_db()
        self.assertEqual(s.difference, D('0'))
        self.assertEqual(treasury_book_balance(self.w.treasury), D('2000'))

    def test_inter_branch_transfer_received_during_shift_counts(self):
        other_branch = make_branch('فرع 2')
        src = make_treasury(other_branch, 'خزينة فرع 2')
        put_cash(src, D('800'))
        tr = InternalTransfer.objects.create(amount=D('800'), from_account_type='TREASURY', from_treasury=src,
                                             to_account_type='TREASURY', to_treasury=self.w.treasury)
        tr.confirm_transfer(user=self.w.admin)           # sent BEFORE the shift
        c, _ = self._open(self.w.cashier)
        s = POSSession.objects.get(user=self.w.cashier)
        self.assertEqual(s.opening_balance, D('3000'))   # still in transit
        tr.refresh_from_db()
        tr.receive_transfer(user=self.w.admin)            # arrives DURING the shift
        r = c.get(f"{reverse('invoicing:pos_close_shift')}?session_id={s.pk}")
        self.assertEqual(r.context['expected_balance'], D('3800'))
