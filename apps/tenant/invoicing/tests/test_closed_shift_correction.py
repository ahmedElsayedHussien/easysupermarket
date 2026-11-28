"""تصحيح طريقة سداد لوردية مقفولة: بيعة نقدي 100 والوردية اتقفلت بعجز 100 والعميل كان دافع فيزا —
القيد بيقفل العجز (من الحساب اللي في قيد القفل نفسه)، الفيزا بتزيد 100، تقرير الوردية زي ما هو، ومرة واحدة بس."""
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import (account, build_world, login, make_bank_account, make_invoice,
                                              stock_in)
from apps.tenant.invoicing.closed_shift_correction import correct_payment
from apps.tenant.invoicing.models import ClosedShiftPaymentCorrection, POSSession
from apps.tenant.invoicing.shift_services import CHARGE_CASHIER, post_shift_variance, shift_cash_summary


def _balance(acc):
    return sum((i.debit - i.credit for i in acc.journal_items.filter(entry__status='POSTED')), D('0'))


class ClosedShiftCorrectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.bank = make_bank_account('ماكينة فيزا الفرع')

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, D('4'))

    def close_short(self, charge_to='EXPENSE'):
        session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        sale = make_invoice(self.w, lines=[(self.w.product, 10, 10)], pos_session=session)      # cash 100
        POSSession.objects.filter(pk=session.pk).update(status=POSSession.CLOSED, difference=D('-100'))
        session.refresh_from_db()
        post_shift_variance(session, charge_to, self.w.admin)                                   # shortage 100
        session.refresh_from_db()
        return session, sale

    def figures(self, session):
        return {k: v for k, v in shift_cash_summary(session).items() if isinstance(v, (D, int))}

    def test_card_correction_clears_the_shortage(self):
        session, sale = self.close_short()
        before = self.figures(session)
        shortage = account('5400')
        self.assertEqual(_balance(shortage), D('100'))
        c = correct_payment(sale, ClosedShiftPaymentCorrection.CARD, self.w.admin, 'العميل دفع فيزا',
                            bank_account=self.bank)
        self.assertEqual(c.variance_account, shortage)                  # read from the shift-close entry
        self.assertEqual(_balance(shortage), D('0'))
        self.assertEqual(_balance(self.bank.account), D('100'))
        self.assertEqual(self.figures(session), before)                 # the closed shift's report is unchanged
        sale.refresh_from_db()
        self.assertEqual(sale.payment_type, 'CASH')                     # the invoice itself is not edited
        from apps.tenant.core.models import AuditLog
        log = AuditLog.objects.filter(object_id=str(sale.pk), action=AuditLog.UPDATE).latest('id')
        self.assertEqual(log.changes['المبلغ'], '100.00')
        with self.assertRaisesMessage(ValidationError, 'مرة واحدة'):  # never twice for one invoice
            correct_payment(sale, ClosedShiftPaymentCorrection.CARD, self.w.admin, 'تاني', bank_account=self.bank)

    def test_shortage_charged_to_the_cashier_is_credited_back_there(self):
        session, sale = self.close_short(CHARGE_CASHIER)
        loan = session.shortage_loan
        self.assertEqual(loan.amount, D('100'))
        c = correct_payment(sale, ClosedShiftPaymentCorrection.CARD, self.w.admin, 'فيزا', bank_account=self.bank)
        self.assertEqual(c.variance_account, account('1135'))           # not assumed: the entry said 1135
        loan.refresh_from_db()
        self.assertEqual((loan.amount, loan.status), (D('0'), loan.SETTLED))   # nothing left to deduct from salary

    def test_screen_needs_the_permission_and_shows_the_note_on_the_shift_report(self):
        from apps.tenant.core.tests.factories import make_user
        session, sale = self.close_short()
        url = reverse('invoicing:closed_shift_payment_correction', args=[sale.id])
        login(self.client, make_user('no_perm_clerk', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(url).status_code, 302)                  # no permission -> away
        login(self.client, self.w.admin)
        r = self.client.post(url, {'method': 'CARD', 'bank_account': self.bank.id, 'reason': 'فيزا مش كاش'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(ClosedShiftPaymentCorrection.objects.filter(invoice=sale).exists())
        r = self.client.get(reverse('core:report_shift_detail', args=[session.pk]))
        self.assertContains(r, 'تصحيحات طريقة سداد بعد قفل الوردية')


class ClosedShiftCorrectionToCashTests(TestCase):
    """The other way: recorded card / wallet / transfer, the customer really paid cash -> the drawer came out
    over. Dr the overage account (from the shift-close entry) / Cr the bank the invoice was booked to."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.bank = make_bank_account('ماكينة فيزا الفرع')

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, D('4'))

    def close_over(self, overage, sales=1):
        session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        invoices = [make_invoice(self.w, lines=[(self.w.product, 10, 10)], pos_session=session,
                                 payment_type='CARD', bank_account=self.bank) for _ in range(sales)]   # card 100 each
        POSSession.objects.filter(pk=session.pk).update(status=POSSession.CLOSED, difference=D(overage))
        session.refresh_from_db()
        post_shift_variance(session, 'EXPENSE', self.w.admin)                                       # overage
        session.refresh_from_db()
        return session, invoices

    def test_card_sale_really_paid_cash_clears_the_overage_and_lowers_the_bank(self):
        session, (sale,) = self.close_over('100')
        overage = account('5400')
        self.assertEqual(_balance(overage), D('-100'))                  # credit 100 at close
        self.assertEqual(_balance(self.bank.account), D('100'))         # the sale debited the card bank
        before = {k: v for k, v in shift_cash_summary(session).items() if isinstance(v, (D, int))}
        c = correct_payment(sale, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'العميل دفع كاش')
        self.assertEqual((c.recorded_method, c.method, c.variance_account), ('CARD', 'CASH', overage))
        self.assertEqual(_balance(overage), D('0'))
        self.assertEqual(_balance(self.bank.account), D('0'))           # the bank went down by 100
        self.assertEqual({k: v for k, v in shift_cash_summary(session).items() if isinstance(v, (D, int))}, before)
        with self.assertRaisesMessage(ValidationError, 'مرة واحدة'):
            correct_payment(sale, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'تاني')

    def test_corrections_never_exceed_the_overage(self):
        session, (first, second) = self.close_over('150', sales=2)
        correct_payment(first, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'كاش')
        with self.assertRaisesMessage(ValidationError, '50.00'):        # only 50 of the overage is left
            correct_payment(second, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'كاش')

    def test_direction_must_match_the_shift_variance_and_the_invoice(self):
        session, (sale,) = self.close_over('100')
        with self.assertRaisesMessage(ValidationError, 'مفيهاش مبلغ نقدي'):     # a card sale is not "cash -> card"
            correct_payment(sale, ClosedShiftPaymentCorrection.BANK_TRANSFER, self.w.admin, 'تحويل',
                            bank_account=self.bank)
        cash_session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        cash_sale = make_invoice(self.w, lines=[(self.w.product, 1, 10)], pos_session=cash_session)
        POSSession.objects.filter(pk=cash_session.pk).update(status=POSSession.CLOSED, difference=D('10'))
        cash_session.refresh_from_db()
        post_shift_variance(cash_session, 'EXPENSE', self.w.admin)
        with self.assertRaisesMessage(ValidationError, 'اتسجلت'):               # already cash
            correct_payment(cash_sale, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'كاش')
        with self.assertRaisesMessage(ValidationError, 'مفيهاش عجز'):           # an overage shift has no shortage
            correct_payment(cash_sale, ClosedShiftPaymentCorrection.CARD, self.w.admin, 'فيزا', bank_account=self.bank)

    def test_screen_offers_only_cash_and_the_shift_report_says_which_way(self):
        session, (sale,) = self.close_over('100')
        login(self.client, self.w.admin)
        url = reverse('invoicing:closed_shift_payment_correction', args=[sale.id])
        r = self.client.get(url)
        self.assertContains(r, 'value="CASH"')
        self.assertNotContains(r, 'value="CARD"')
        self.assertNotContains(r, 'name="bank_account"')
        self.assertEqual(self.client.post(url, {'method': 'CASH', 'reason': 'دفع كاش'}).status_code, 302)
        r = self.client.get(reverse('core:report_shift_detail', args=[session.pk]))
        self.assertContains(r, 'نقدي بدل فيزا')

    def test_credits_the_bank_of_the_posted_sale_entry_not_todays_default(self):
        session, (sale,) = self.close_over('100')
        other = make_bank_account('بنك تاني')
        from apps.tenant.invoicing.models import Invoice
        Invoice.objects.filter(pk=sale.pk).update(bank_account=other)    # the invoice no longer says which bank
        c = correct_payment(sale, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'كاش')
        credited = c.journal_entry.items.get(credit__gt=0).account
        self.assertEqual(credited, self.bank.account)                    # read from the sale entry's debit line
        self.assertEqual(_balance(self.bank.account), D('0'))
        self.assertEqual(_balance(other.ensure_account()), D('0'))


class ClosedShiftCorrectionSplitTests(TestCase):
    """A SPLIT invoice: only the leg that was recorded wrong is corrected — never the whole invoice."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.bank = make_bank_account('ماكينة فيزا الفرع')

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, D('4'))

    def split_sale(self, difference):
        session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        sale = make_invoice(self.w, lines=[(self.w.product, 10, 10)], pos_session=session, payment_type='SPLIT',
                            treasury=self.w.treasury, bank_account=self.bank,
                            paid_cash=D('60'), paid_card=D('40'))                      # 100 = 60 cash + 40 card
        POSSession.objects.filter(pk=session.pk).update(status=POSSession.CLOSED, difference=D(difference))
        session.refresh_from_db()
        post_shift_variance(session, 'EXPENSE', self.w.admin)
        session.refresh_from_db()
        return session, sale

    def test_card_leg_really_paid_cash(self):
        session, sale = self.split_sale('40')
        with self.assertRaisesMessage(ValidationError, 'اختار الجزء'):          # which leg must be said
            correct_payment(sale, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'كاش')
        with self.assertRaisesMessage(ValidationError, '40.00'):                # never more than the card leg
            correct_payment(sale, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'كاش',
                            wrong_method='CARD', amount='100')
        c = correct_payment(sale, ClosedShiftPaymentCorrection.CASH, self.w.admin, 'كاش', wrong_method='CARD')
        self.assertEqual((c.recorded_method, c.amount), ('CARD', D('40.00')))   # the card leg, not the 100 total
        self.assertEqual(_balance(account('5400')), D('0'))
        self.assertEqual(_balance(self.bank.account), D('0'))

    def test_cash_leg_really_paid_by_card_part_of_it(self):
        session, sale = self.split_sale('-25')
        c = correct_payment(sale, ClosedShiftPaymentCorrection.CARD, self.w.admin, 'جزء فيزا',
                            bank_account=self.bank, amount='25')
        self.assertEqual((c.recorded_method, c.amount), ('CASH', D('25.00')))
        self.assertEqual(_balance(account('5400')), D('0'))
        self.assertEqual(_balance(self.bank.account), D('65'))                  # 40 booked + 25 corrected
