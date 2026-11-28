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
        self.assertEqual(c.shortage_account, shortage)                  # read from the shift-close entry
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
        self.assertEqual(c.shortage_account, account('1135'))           # not assumed: the entry said 1135
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
