"""إيجار المعدات: ترحيل مرة واحدة على معدة متاحة، العقد المرحّل ثابت، والإرجاع باسم اللي استلم وفي الوردية."""
import datetime
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.tenant.accounting.models import JournalEntry
from apps.tenant.core.tests.factories import build_world, make_user
from apps.tenant.invoicing.models import POSSession
from apps.tenant.invoicing.shift_services import shift_cash_summary
from apps.tenant.rentals.models import Equipment, Rental


class RentalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.clerk = make_user('rent_clerk', role='CASHIER', branch=cls.w.branch)

    def setUp(self):
        self.eq = Equipment.objects.create(name='خلاطة', code=f'EQ-{Equipment.objects.count() + 1}',
                                           daily_rate=D('100'))

    def rent(self, days=2, method=Rental.PAYMENT_CASH, equipment=None):
        r = Rental.objects.create(equipment=equipment or self.eq, customer=self.w.customer, days_rented=days,
                                  payment_method=method, treasury=self.w.treasury if method == Rental.PAYMENT_CASH else None,
                                  created_by=self.w.admin)
        r.post_rental(user=self.w.admin)
        r.refresh_from_db()
        return r

    def test_posts_once_and_only_on_available_equipment(self):
        r = self.rent()
        self.assertEqual(r.accrual_journal_entry.status, JournalEntry.POSTED)
        self.assertEqual(r.payment_journal_entry.get_totals(), (D('200'), D('200')))
        r.post_rental()                                                 # second call: nothing new
        self.assertEqual(JournalEntry.objects.filter(reference__startswith=f'RNT-{r.pk}-').count(), 2)
        other = Rental.objects.create(equipment=self.eq, customer=self.w.customer, days_rented=1,
                                      payment_method=Rental.PAYMENT_CREDIT, created_by=self.w.admin)
        with self.assertRaisesMessage(ValidationError, 'مش متاحة'):
            other.post_rental()                                          # already rented

    def test_zero_days_refused_and_posted_contract_frozen(self):
        eq2 = Equipment.objects.create(name='سقالة', code='EQ-Z', daily_rate=D('50'))
        zero = Rental.objects.create(equipment=eq2, customer=self.w.customer, days_rented=0,
                                     payment_method=Rental.PAYMENT_CREDIT, created_by=self.w.admin)
        with self.assertRaisesMessage(ValidationError, 'يوم أو أكتر'):
            zero.post_rental()
        r = self.rent()
        r.days_rented = 10
        with self.assertRaisesMessage(ValidationError, 'اترحّل'):
            r.save()
        r.refresh_from_db()
        self.assertEqual(r.total_amount, D('200'))

    def test_return_booked_by_receiver_and_counted_in_shift(self):
        r = self.rent(method=Rental.PAYMENT_CREDIT)
        session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.clerk)
        r.return_equipment(refund_amount='50', penalty_amount='20', refund_method='cash', treasury=self.w.treasury,
                           user=self.clerk)
        r.refresh_from_db()
        self.assertEqual(r.status, Rental.STATUS_RETURNED)
        self.assertEqual(r.settlement_journal_entry.created_by, self.clerk)
        self.assertEqual(r.penalty_journal_entry.created_by, self.clerk)
        self.eq.refresh_from_db()
        self.assertEqual(self.eq.status, Equipment.STATUS_AVAILABLE)
        self.assertEqual(shift_cash_summary(session)['total_rental_settlements'], D('-30'))   # 50 refund − 20 penalty paid out
        with self.assertRaisesMessage(ValidationError, 'مسبقاً'):
            r.return_equipment(user=self.clerk)

    def test_opening_balance_booked_once(self):
        eq = Equipment.objects.create(name='مولد', code='EQ-O', daily_rate=D('300'), purchase_cost=D('5000'))
        eq.post_opening_balance(self.w.admin)
        eq.post_opening_balance(self.w.admin)
        self.assertEqual(JournalEntry.objects.filter(reference__startswith=f'OPENING-EQ-{eq.pk}').count(), 1)
        eq.refresh_from_db()
        self.assertEqual(eq.opening_journal_entry.get_totals(), (D('5000'), D('5000')))
