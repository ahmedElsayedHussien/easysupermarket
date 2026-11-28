"""Sales by payment method with the closed-shift corrections as an adjustment: recorded + adjustment = the books.

A cash sale of 100 really paid by card, shift closed short 100, then corrected: the shift still shows cash 100 /
card 0 as it closed, the adjustment is cash -100 / card +100, and card after the correction (100) is exactly what
the card bank holds in the ledger."""
import io
from decimal import Decimal as D

import openpyxl
from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, make_bank_account, make_invoice, stock_in
from apps.tenant.invoicing.closed_shift_correction import correct_payment, correction_adjustments
from apps.tenant.invoicing.models import ClosedShiftPaymentCorrection, POSSession
from apps.tenant.invoicing.shift_services import post_shift_variance


def _balance(acc):
    return sum((i.debit - i.credit for i in acc.journal_items.filter(entry__status='POSTED')), D('0'))


class ShiftReportCorrectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.bank = make_bank_account('ماكينة فيزا الفرع')

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, D('4'))
        self.session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        self.sale = make_invoice(self.w, lines=[(self.w.product, 10, 10)], pos_session=self.session)   # cash 100
        POSSession.objects.filter(pk=self.session.pk).update(status=POSSession.CLOSED, difference=D('-100'))
        self.session.refresh_from_db()
        post_shift_variance(self.session, 'EXPENSE', self.w.admin)
        self.session.refresh_from_db()
        correct_payment(self.sale, ClosedShiftPaymentCorrection.CARD, self.w.admin, 'فيزا', bank_account=self.bank)
        login(self.client, self.w.admin)

    def test_adjustment_per_method(self):
        adj = correction_adjustments(self.session.payment_corrections.all())
        self.assertEqual((adj['cash'], adj['card'], adj['ewallet'], adj['bank'], adj['digital']),
                         (D('-100'), D('100'), D('0'), D('0'), D('100')))
        self.assertTrue(adj['any'])

    def test_shift_detail_shows_recorded_adjustment_and_the_books(self):
        r = self.client.get(reverse('core:report_shift_detail', args=[self.session.pk]))
        rows = {label: (rec, adj, after) for label, rec, adj, after in r.context['correction_rows']}
        self.assertEqual(rows['كاش'], (D('100'), D('-100'), D('0')))
        self.assertEqual(rows['فيزا / كارت'], (D('0'), D('100'), D('100')))
        self.assertEqual(rows['فيزا / كارت'][2], _balance(self.bank.account))      # matches the ledger
        self.assertContains(r, 'بعد التصحيح (زي الدفاتر)')

    def test_shifts_list_and_excel_carry_the_adjustment(self):
        url = reverse('core:report_shifts')
        r = self.client.get(url)
        row = next(i for i in r.context['sessions_data'] if i['session'].id == self.session.id)
        self.assertEqual((row['correction']['cash'], row['correction']['digital']), (D('-100'), D('100')))
        self.assertEqual(row['total_cash_sales'], D('100'))                       # the shift itself as closed
        self.assertContains(r, 'تسوية تصحيحات')
        x = self.client.get(url, {'export': 'excel'})
        ws = openpyxl.load_workbook(io.BytesIO(x.content)).active
        rows = list(ws.iter_rows(values_only=True))
        header = next(h for h in rows if h and h[0] == 'رقم الوردية')
        data = next(d for d in rows if d and d[0] == f'#{self.session.id}')
        got = dict(zip(header, data))
        self.assertEqual(float(got['تسوية تصحيحات: كاش']), -100.0)
        self.assertEqual(float(got['إلكتروني بعد التصحيح']), 100.0)
        self.assertEqual(float(got['كاش بعد التصحيح']), 0.0)

    def test_shift_without_corrections_shows_nothing_extra(self):
        other = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        r = self.client.get(reverse('core:report_shift_detail', args=[other.pk]))
        self.assertNotIn('correction_rows', r.context)
        r = self.client.get(reverse('core:report_shifts'))
        row = next(i for i in r.context['sessions_data'] if i['session'].id == other.id)
        self.assertIsNone(row['correction'])


class PeriodTotalsByEntryDateTests(TestCase):
    """Period totals per method count a correction by the date of ITS entry, not its shift's: a 30/11 shift
    corrected on 3/12 shows in December — exactly where the bank account moves."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.bank = make_bank_account('ماكينة فيزا الفرع')

    def test_november_shift_corrected_in_december(self):
        import datetime
        from types import SimpleNamespace
        from unittest import mock
        from django.utils import timezone
        stock_in(self.w.product, self.w.warehouse, 50, D('4'))
        nov30 = datetime.date(2026, 11, 30)
        session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        POSSession.objects.filter(pk=session.pk).update(
            start_time=timezone.make_aware(datetime.datetime(2026, 11, 30, 10, 0)))
        sale = make_invoice(self.w, lines=[(self.w.product, 10, 10)], pos_session=session, date=nov30)  # cash 100
        POSSession.objects.filter(pk=session.pk).update(status=POSSession.CLOSED, difference=D('-100'))
        session.refresh_from_db()
        post_shift_variance(session, 'EXPENSE', self.w.admin)

        class Dec3(datetime.date):
            @classmethod
            def today(cls):
                return datetime.date(2026, 12, 3)
        with mock.patch('apps.tenant.accounting.posting.datetime', SimpleNamespace(date=Dec3)):
            c = correct_payment(sale, ClosedShiftPaymentCorrection.CARD, self.w.admin, 'فيزا',
                                bank_account=self.bank)
        self.assertEqual(c.journal_entry.date, datetime.date(2026, 12, 3))

        login(self.client, self.w.admin)

        def card_total(date_from, date_to):
            r = self.client.get(reverse('core:report_shifts'), {'date_from': date_from, 'date_to': date_to})
            return {label: total for label, _rec, _adj, total in r.context['period_methods']}['فيزا / كارت']

        def bank_movement(date_from, date_to):
            items = self.bank.account.journal_items.filter(entry__status='POSTED', entry__date__gte=date_from,
                                                           entry__date__lte=date_to)
            return sum((i.debit - i.credit for i in items), D('0'))

        self.assertEqual(card_total('2026-11-01', '2026-11-30'), D('0'))
        self.assertEqual(card_total('2026-12-01', '2026-12-31'), D('100'))
        self.assertEqual(card_total('2026-11-01', '2026-11-30'), bank_movement('2026-11-01', '2026-11-30'))
        self.assertEqual(card_total('2026-12-01', '2026-12-31'), bank_movement('2026-12-01', '2026-12-31'))
        # the shift's own row still carries its correction, whatever the correction's date
        r = self.client.get(reverse('core:report_shifts'), {'date_from': '2026-11-01', 'date_to': '2026-11-30'})
        row = next(i for i in r.context['sessions_data'] if i['session'].id == session.id)
        self.assertEqual(row['correction']['card'], D('100'))
        self.assertContains(r, 'محسوبة بتاريخ قيد التصحيح')
