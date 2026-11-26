"""HR: payroll generation and attendance."""
import datetime
from decimal import Decimal as D
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.tenant.core.tests.factories import build_world, login, make_user
from apps.tenant.hr.models import Attendance, Payroll


class PayrollProcessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('worker1', role='CASHIER', branch=cls.w.branch)
        emp = cls.worker.employee_profile
        emp.base_salary = D('3000')
        emp.save()

    def setUp(self):
        login(self.client, self.w.admin)
        self.today = timezone.localdate()

    def _process(self):
        return self.client.post(reverse('hr:payroll_process'),
                                {'month': self.today.month, 'year': self.today.year})

    def test_rerun_skips_paid_payroll(self):
        emp = self.worker.employee_profile
        paid = Payroll.objects.create(employee=emp, month=self.today.month, year=self.today.year,
                                      base_pay=D('3000'), net_salary=D('1234.00'), is_paid=True)
        resp = self._process()
        self.assertEqual(resp.status_code, 302)
        paid.refresh_from_db()
        self.assertEqual(paid.net_salary, D('1234.00'))
        self.assertTrue(paid.is_paid)

    def test_run_creates_payroll_and_rerun_updates_unpaid(self):
        self._process()
        p = Payroll.objects.get(employee=self.worker.employee_profile, month=self.today.month,
                                year=self.today.year)
        self.assertEqual(p.base_pay, D('3000'))
        self.assertFalse(p.is_paid)
        Payroll.objects.filter(pk=p.pk).update(net_salary=D('1'))
        self._process()
        p.refresh_from_db()
        self.assertNotEqual(p.net_salary, D('1'))
        self.assertEqual(Payroll.objects.filter(employee=self.worker.employee_profile).count(), 1)

    def test_confirm_posts_entry_once(self):
        self._process()
        p = Payroll.objects.get(employee=self.worker.employee_profile)
        if p.net_salary <= 0:
            Payroll.objects.filter(pk=p.pk).update(net_salary=D('100'))
        url = reverse('hr:payroll_confirm', args=[p.pk])
        self.client.post(url)
        self.client.post(url)
        p.refresh_from_db()
        self.assertTrue(p.is_paid)
        self.assertIsNotNone(p.journal_entry_id)
        self.assertTrue(p.journal_entry.is_balanced())
        from apps.tenant.accounting.models import JournalEntry
        self.assertEqual(JournalEntry.objects.filter(payroll=p).count(), 1)


class AttendanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.w.branch.latitude = D('30.044420')
        cls.w.branch.longitude = D('31.235712')
        cls.w.branch.save()
        cls.worker = make_user('worker2', role='CASHIER', branch=cls.w.branch)

    def test_check_in_uses_local_date(self):
        # 23:30 UTC on 10 March = 01:30 on 11 March in Africa/Cairo (UTC+2)
        fake_now = datetime.datetime(2026, 3, 10, 23, 30, tzinfo=datetime.timezone.utc)
        login(self.client, self.worker)
        with mock.patch('django.utils.timezone.now', return_value=fake_now):
            resp = self.client.post(reverse('hr:api_attendance_check'), {
                'action': 'check_in', 'latitude': '30.044420', 'longitude': '31.235712',
            })
        self.assertEqual(resp.status_code, 200, resp.content)
        att = Attendance.objects.get(employee=self.worker.employee_profile)
        self.assertEqual(att.date, datetime.date(2026, 3, 11))

    def test_check_in_far_from_branch_refused(self):
        login(self.client, self.worker)
        resp = self.client.post(reverse('hr:api_attendance_check'), {
            'action': 'check_in', 'latitude': '31.2', 'longitude': '29.9',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Attendance.objects.exists())

    def test_double_check_in_refused(self):
        login(self.client, self.worker)
        data = {'action': 'check_in', 'latitude': '30.044420', 'longitude': '31.235712'}
        self.assertEqual(self.client.post(reverse('hr:api_attendance_check'), data).status_code, 200)
        self.assertEqual(self.client.post(reverse('hr:api_attendance_check'), data).status_code, 400)
        self.assertEqual(Attendance.objects.count(), 1)


class PayrollSettingsTests(TestCase):
    """weekly_off_days / payroll_day_basis / late_grace_minutes from SystemSetting."""
    YEAR, MONTH = 2025, 2      # 28 days; Fridays 7,14,21,28; Saturdays 1,8,15,22

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('worker3', role='CASHIER', branch=cls.w.branch)
        cls.worker.date_joined = timezone.make_aware(datetime.datetime(2020, 1, 1))
        cls.worker.save(update_fields=['date_joined'])
        emp = cls.worker.employee_profile
        emp.base_salary = D('3000')
        emp.deduction_per_hour = D('100')
        emp.save()

    def setUp(self):
        login(self.client, self.w.admin)

    def _payroll(self, **settings):
        from apps.tenant.core.tests.factories import system_settings
        system_settings(**settings)
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        return Payroll.objects.get(employee=self.worker.employee_profile, month=self.MONTH, year=self.YEAR)

    def test_default_friday_off_and_30_day_basis(self):
        p = self._payroll()
        # 24 working days absent * 3000/30
        self.assertEqual(p.deductions, D('2400.00'))
        self.assertEqual(p.net_salary, D('600.00'))

    def test_weekly_off_days_setting(self):
        p = self._payroll(weekly_off_days='4,5')     # Friday + Saturday -> 20 working days
        self.assertEqual(p.deductions, D('2000.00'))

    def test_working_days_basis(self):
        # daily rate = 3000 / 24 working days; present on one day -> 23 absent
        Attendance.objects.create(employee=self.worker.employee_profile, date=datetime.date(2025, 2, 3),
                                  check_in=timezone.make_aware(datetime.datetime(2025, 2, 3, 9, 0)))
        p = self._payroll(payroll_day_basis='WORKING_DAYS')
        self.assertEqual(p.deductions, D('2875.00'))   # 23 * 125

    def test_late_grace_minutes(self):
        emp = self.worker.employee_profile
        for day, delay in ((3, D('0.50')), (4, D('0.10'))):
            d = datetime.date(2025, 2, day)
            Attendance.objects.create(employee=emp, date=d, delay_hours=delay,
                                      check_in=timezone.make_aware(datetime.datetime(2025, 2, day, 9, 0)))
        self.assertEqual(self._payroll().total_delay_hours, D('0.60'))
        # 10 minutes grace per day: 0.50 - 0.1667 = 0.33 ; 0.10 -> 0
        p = self._payroll(late_grace_minutes=10)
        self.assertEqual(p.total_delay_hours, D('0.33'))
        self.assertEqual(p.deductions, D('2233.00'))   # 22 absent * 100 + 0.33 * 100


class ManualAttendanceTests(TestCase):
    """Manager's attendance sheet: works without the location check-in and feeds payroll
    exactly like it (same delay / overtime rules)."""
    YEAR, MONTH = 2025, 2      # 28 days; Fridays 7,14,21,28 (weekly off) -> 24 working days

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('manual_worker', role='CASHIER', branch=cls.w.branch)
        cls.worker.date_joined = timezone.make_aware(datetime.datetime(2020, 1, 1))
        cls.worker.save(update_fields=['date_joined'])
        emp = cls.worker.employee_profile
        emp.base_salary = D('3000')
        emp.deduction_per_hour = D('100')
        emp.overtime_per_hour = D('50')
        emp.shift_start_time = datetime.time(9, 0)
        emp.shift_end_time = datetime.time(17, 0)
        emp.save()
        cls.emp = emp

    def setUp(self):
        login(self.client, self.w.admin)

    def day(self, d):
        return datetime.date(self.YEAR, self.MONTH, d)

    def post_day(self, d, status, t_in='', t_out='', notes=''):
        k = self.emp.id
        return self.client.post(reverse('hr:attendance_manage'), {
            'date': self.day(d).isoformat(), f'status_{k}': status, f'in_{k}': t_in, f'out_{k}': t_out,
            f'notes_{k}': notes, f'orig_{k}': '',
        })

    def test_manual_day_computes_delay_and_overtime(self):
        self.post_day(3, 'present', '09:30', '18:15')
        att = Attendance.objects.get(employee=self.emp, date=self.day(3))
        self.assertEqual(att.source, Attendance.SOURCE_MANUAL)
        self.assertEqual(att.entered_by, self.w.admin)
        self.assertEqual(att.delay_hours, D('0.50'))
        self.assertEqual(att.overtime_hours, D('1.25'))

    def test_overnight_check_out_is_next_day(self):
        self.emp.shift_start_time, self.emp.shift_end_time = datetime.time(22, 0), datetime.time(6, 0)
        self.emp.save()
        self.post_day(3, 'present', '22:00', '07:00')
        att = Attendance.objects.get(employee=self.emp, date=self.day(3))
        self.assertEqual(att.check_out.date(), self.day(4))
        self.assertEqual(att.overtime_hours, D('1.00'))

    def test_leave_and_absent_clear_times(self):
        self.post_day(3, 'present', '09:00', '17:00')
        self.post_day(3, 'leave')
        att = Attendance.objects.get(employee=self.emp, date=self.day(3))
        self.assertEqual((att.status, att.check_in, att.delay_hours), ('leave', None, D('0.00')))

    def test_present_needs_check_in_and_future_refused(self):
        self.post_day(3, 'present')
        self.assertFalse(Attendance.objects.filter(employee=self.emp, date=self.day(3)).exists())
        k = self.emp.id
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        from apps.tenant.hr.attendance_services import AttendanceError, save_manual_day
        with self.assertRaises(AttendanceError):
            save_manual_day(self.emp, tomorrow, 'present', check_in=datetime.time(9, 0))

    def test_untouched_gps_row_is_not_rewritten(self):
        check_in = timezone.make_aware(datetime.datetime(self.YEAR, self.MONTH, 3, 9, 0, 41))
        Attendance.objects.create(employee=self.emp, date=self.day(3), check_in=check_in, status='present')
        k = self.emp.id
        self.client.post(reverse('hr:attendance_manage'), {
            'date': self.day(3).isoformat(), f'status_{k}': 'present', f'in_{k}': '09:00', f'out_{k}': '',
            f'notes_{k}': '', f'orig_{k}': 'present|09:00||',
        })
        att = Attendance.objects.get(employee=self.emp, date=self.day(3))
        self.assertEqual(att.source, Attendance.SOURCE_GPS)
        self.assertEqual(att.check_in, check_in)      # seconds kept: row was not saved again

    def test_fill_period_then_payroll_has_no_absence(self):
        r = self.client.post(reverse('hr:attendance_manage'), {
            'action': 'fill_period', 'date_from': self.day(1).isoformat(), 'date_to': self.day(28).isoformat(),
        })
        self.assertEqual(r.status_code, 302)
        rows = Attendance.objects.filter(employee=self.emp, date__year=self.YEAR, date__month=self.MONTH)
        self.assertEqual(rows.count(), 24)                                  # Fridays skipped
        self.assertFalse(rows.filter(date=self.day(7)).exists())
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        pay = Payroll.objects.get(employee=self.emp, month=self.MONTH, year=self.YEAR)
        self.assertEqual(pay.deductions, D('0.00'))
        self.assertEqual(pay.net_salary, D('3000.00'))

    def test_manual_absence_and_delay_reach_payroll(self):
        self.client.post(reverse('hr:attendance_manage'), {
            'action': 'fill_period', 'date_from': self.day(1).isoformat(), 'date_to': self.day(28).isoformat(),
        })
        self.post_day(3, 'absent')
        self.post_day(4, 'present', '10:00', '17:00')                          # 1 h late
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        pay = Payroll.objects.get(employee=self.emp, month=self.MONTH, year=self.YEAR)
        self.assertEqual(pay.deductions, D('100.00') + D('100.00'))          # 1 day (3000/30) + 1 h

    def test_paid_month_is_locked(self):
        Payroll.objects.create(employee=self.emp, month=self.MONTH, year=self.YEAR, is_paid=True)
        self.post_day(3, 'present', '09:00', '17:00')
        self.assertFalse(Attendance.objects.filter(employee=self.emp, date=self.day(3)).exists())

    def test_screen_lists_employee_and_needs_permission(self):
        r = self.client.get(reverse('hr:attendance_manage') + f'?date={self.day(3).isoformat()}')
        self.assertContains(r, 'إدخال الحضور اليدوي')
        self.assertContains(r, f'status_{self.emp.id}')
        login(self.client, self.worker)
        r = self.client.get(reverse('hr:attendance_manage'))
        self.assertEqual(r.status_code, 302)


class PayrollAndAttendanceReportTests(TestCase):
    YEAR, MONTH = 2025, 2      # 24 working days (Fridays off)

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('report_worker', role='CASHIER', branch=cls.w.branch)
        cls.worker.date_joined = timezone.make_aware(datetime.datetime(2020, 1, 1))
        cls.worker.save(update_fields=['date_joined'])
        emp = cls.worker.employee_profile
        emp.base_salary = D('3000')
        emp.deduction_per_hour = D('100')
        emp.shift_start_time, emp.shift_end_time = datetime.time(9, 0), datetime.time(17, 0)
        emp.save()
        cls.emp = emp

    def setUp(self):
        login(self.client, self.w.admin)
        self.client.post(reverse('hr:attendance_manage'), {
            'action': 'fill_period', 'date_from': '2025-02-01', 'date_to': '2025-02-20'})   # 18 working days
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        self.period = {'month': self.MONTH, 'year': self.YEAR}

    def test_payroll_stores_attendance_breakdown(self):
        p = Payroll.objects.get(employee=self.emp, month=self.MONTH, year=self.YEAR)
        self.assertEqual((p.working_days, p.present_days, p.absent_days), (24, 18, 6))
        self.assertEqual(p.absence_deduction, D('600.00'))           # 6 x 3000/30
        self.assertEqual(p.delay_deduction, D('0.00'))
        self.assertEqual(p.deductions, p.absence_deduction + p.delay_deduction)

    def test_payroll_screen_is_one_month_with_12_months(self):
        r = self.client.get(reverse('hr:payroll_list'), self.period)
        self.assertEqual(len(r.context['month_choices']), 12)
        self.assertEqual(r.context['totals']['n'], 1)
        self.assertContains(r, 'مسير رواتب فبراير 2025')
        self.assertNotContains(r, '2,025')
        r = self.client.get(reverse('hr:payroll_list'), {'month': 3, 'year': 2025})
        self.assertEqual(r.context['payrolls'], [])

    def test_attendance_report_rows_totals_and_excel(self):
        r = self.client.get(reverse('hr:attendance_report'), self.period)
        row = next(x for x in r.context['rows'] if x['employee'] == self.emp)
        self.assertEqual((row['present_days'], row['absent_days'], row['attendance_rate']), (18, 6, 75))
        self.assertEqual(row['payroll'].net_salary, D('2400.00'))
        r = self.client.get(reverse('hr:attendance_report'), {**self.period, 'export': 'excel'})
        self.assertEqual(r['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def test_employee_month_day_by_day(self):
        r = self.client.get(reverse('hr:attendance_report'), {**self.period, 'employee': self.emp.id})
        states = {d['date'].day: d['state'] for d in r.context['days']}
        self.assertEqual(states[3], 'present')
        self.assertEqual(states[7], 'off')        # Friday
        self.assertEqual(states[24], 'absent')

    def test_salaries_hidden_without_payroll_permission(self):
        clerk = make_user('att_clerk', role='CASHIER', branch=self.w.branch, perms=('hr.view_attendance',))
        login(self.client, clerk)
        r = self.client.get(reverse('hr:attendance_report'), self.period)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context['show_pay'])
        self.assertNotContains(r, 'صافي الراتب')
        login(self.client, make_user('no_hr', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('hr:attendance_report')).status_code, 302)


class EmployeeLoanTests(TestCase):
    """سلف الموظفين: الصرف من الخزينة، الخصم من المسير، وعجز الكاشير كسلفة."""
    YEAR, MONTH = 2025, 2

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('loan_worker', role='CASHIER', branch=cls.w.branch)
        cls.worker.date_joined = timezone.make_aware(datetime.datetime(2020, 1, 1))
        cls.worker.save(update_fields=['date_joined'])
        emp = cls.worker.employee_profile
        emp.base_salary = D('3000')
        emp.shift_start_time, emp.shift_end_time = datetime.time(9, 0), datetime.time(17, 0)
        emp.save()
        cls.emp = emp

    def setUp(self):
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        from apps.tenant.core.tests.factories import account
        login(self.client, self.w.admin)
        # money in the treasury to pay loans from
        e = JournalEntry.objects.create(date=datetime.date(2025, 1, 1), reference='CAP-LOAN', description='رأس مال')
        JournalItem.objects.create(entry=e, account=self.w.treasury.ensure_account(), debit=D('10000'), credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=D('10000'))
        e.post()
        self.client.post(reverse('hr:attendance_manage'), {
            'action': 'fill_period', 'date_from': '2025-02-01', 'date_to': '2025-02-28'})

    def give_loan(self, amount='900', n='3', start='2025-2'):
        return self.client.post(reverse('hr:loan_list'), {
            'employee': self.emp.id, 'amount': amount, 'installments': n, 'start': start,
            'treasury': self.w.treasury.id, 'reason': 'ظرف'})

    def payroll(self):
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        return Payroll.objects.get(employee=self.emp, month=self.MONTH, year=self.YEAR)

    def test_cash_loan_books_entry_and_payroll_deducts_installment(self):
        from apps.tenant.hr.models import EmployeeLoan
        from apps.tenant.core.tests.factories import account
        self.give_loan()
        loan = EmployeeLoan.objects.get(employee=self.emp)
        self.assertEqual(loan.monthly_installment, D('300.00'))
        self.assertTrue(loan.journal_entry.items.filter(account=account('1135'), debit=D('900')).exists())
        p = self.payroll()
        self.assertEqual(p.loan_deduction, D('300.00'))
        self.assertEqual(p.net_salary, D('2700.00'))
        p2 = self.payroll()                       # recalculation does not deduct twice
        self.assertEqual(p2.loan_deduction, D('300.00'))
        self.assertEqual(loan.remaining, D('600.00'))

    def test_confirm_posts_loan_credit_and_balances(self):
        from apps.tenant.core.tests.factories import account
        self.give_loan(amount='300', n='1')
        p = self.payroll()
        self.client.post(reverse('hr:payroll_confirm', args=[p.id]))
        p.refresh_from_db()
        items = p.journal_entry.items
        self.assertTrue(items.filter(account=account('5210'), debit=D('3000')).exists())
        self.assertTrue(items.filter(account=account('1135'), credit=D('300')).exists())
        self.assertTrue(items.filter(account=account('2160'), credit=D('2700')).exists())
        from apps.tenant.hr.models import EmployeeLoan
        self.assertEqual(EmployeeLoan.objects.get(employee=self.emp).status, EmployeeLoan.SETTLED)

    def test_installment_never_makes_net_negative(self):
        self.give_loan(amount='5000', n='1')
        p = self.payroll()
        self.assertEqual(p.net_salary, D('0.00'))
        self.assertEqual(p.loan_deduction, D('3000.00'))

    def test_future_start_month_not_deducted_and_cancel(self):
        from apps.tenant.hr.models import EmployeeLoan
        self.give_loan(start='2025-3')
        self.assertEqual(self.payroll().loan_deduction, D('0.00'))
        loan = EmployeeLoan.objects.get(employee=self.emp)
        self.client.post(reverse('hr:loan_cancel', args=[loan.id]))
        loan.refresh_from_db()
        self.assertEqual(loan.status, EmployeeLoan.CANCELLED)
        self.assertTrue(loan.journal_entry.is_reversed)

    def test_open_shift_treasury_refused(self):
        from apps.tenant.hr.models import EmployeeLoan
        from apps.tenant.invoicing.models import POSSession
        POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        self.give_loan()
        self.assertFalse(EmployeeLoan.objects.exists())

    def test_cashier_shortage_becomes_loan_deducted_from_salary(self):
        from apps.tenant.hr.models import EmployeeLoan
        from apps.tenant.invoicing.models import POSSession
        from apps.tenant.invoicing.shift_services import CHARGE_CASHIER, post_shift_variance
        s = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.worker,
                                      status=POSSession.CLOSED, closing_balance_expected=D('500'),
                                      closing_balance_actual=D('450'), difference=D('-50'),
                                      end_time=timezone.make_aware(datetime.datetime(2025, 2, 10, 22)))
        post_shift_variance(s, CHARGE_CASHIER, self.w.admin)
        loan = EmployeeLoan.objects.get(pos_session=s)
        self.assertEqual((loan.source, loan.amount, loan.start_month), (EmployeeLoan.SHORTAGE, D('50.00'), 2))
        self.assertEqual(self.payroll().loan_deduction, D('50.00'))

    def test_loans_screen_permission(self):
        r = self.client.get(reverse('hr:loan_list'))
        self.assertContains(r, 'صرف سلفة جديدة')
        login(self.client, make_user('no_loans', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('hr:loan_list')).status_code, 302)


class LeaveTests(TestCase):
    """الإجازات: الرصيد، الاعتماد بيكتب الحضور، والمسير."""
    YEAR, MONTH = 2025, 2   # Fridays 7,14,21,28 off

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('leave_worker', role='CASHIER', branch=cls.w.branch)
        cls.worker.date_joined = timezone.make_aware(datetime.datetime(2020, 1, 1))
        cls.worker.save(update_fields=['date_joined'])
        emp = cls.worker.employee_profile
        emp.base_salary = D('3000')
        emp.shift_start_time, emp.shift_end_time = datetime.time(9, 0), datetime.time(17, 0)
        emp.save()
        cls.emp = emp

    def setUp(self):
        from apps.tenant.hr.models import LeaveType
        login(self.client, self.w.admin)
        self.annual = LeaveType.objects.get(name='اعتيادية')
        self.unpaid = LeaveType.objects.get(name='بدون أجر')

    def create(self, ltype, start, end, approve=True):
        return self.client.post(reverse('hr:leave_list'), {
            'action': 'create', 'employee': self.emp.id, 'leave_type': ltype.id,
            'start_date': start, 'end_date': end, **({'approve_now': '1'} if approve else {})})

    def test_paid_leave_writes_attendance_and_no_absence_deduction(self):
        self.client.post(reverse('hr:attendance_manage'), {
            'action': 'fill_period', 'date_from': '2025-02-01', 'date_to': '2025-02-28'})
        Attendance.objects.filter(employee=self.emp, date__range=('2025-02-03', '2025-02-09')).delete()
        self.create(self.annual, '2025-02-03', '2025-02-09')          # 6 working days (Friday 7 off)
        from apps.tenant.hr.models import LeaveRequest
        req = LeaveRequest.objects.get(employee=self.emp)
        self.assertEqual((req.status, req.days), (LeaveRequest.APPROVED, D('6')))
        self.assertEqual(Attendance.objects.filter(employee=self.emp, leave_request=req, status='leave').count(), 6)
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        p = Payroll.objects.get(employee=self.emp, month=self.MONTH, year=self.YEAR)
        self.assertEqual((p.absent_days, p.leave_days, p.absence_deduction), (0, 6, D('0.00')))

    def test_unpaid_leave_is_deducted(self):
        self.client.post(reverse('hr:attendance_manage'), {
            'action': 'fill_period', 'date_from': '2025-02-01', 'date_to': '2025-02-28'})
        Attendance.objects.filter(employee=self.emp, date__in=['2025-02-10', '2025-02-11']).delete()
        self.create(self.unpaid, '2025-02-10', '2025-02-11')
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        p = Payroll.objects.get(employee=self.emp, month=self.MONTH, year=self.YEAR)
        self.assertEqual((p.absent_days, p.absence_deduction), (2, D('200.00')))

    def test_balance_limit_and_cancel_returns_days(self):
        from apps.tenant.hr.leave_services import balances
        from apps.tenant.hr.models import LeaveRequest
        self.annual.days_per_year = D('3')
        self.annual.save()
        self.create(self.annual, '2025-02-03', '2025-02-09')          # 6 > 3 refused
        self.assertFalse(LeaveRequest.objects.exists())
        self.create(self.annual, '2025-02-03', '2025-02-04')
        req = LeaveRequest.objects.get()
        self.assertEqual(balances([self.emp], 2025)[(self.emp.id, self.annual.id)]['remaining'], D('1'))
        self.client.post(reverse('hr:leave_list'), {'action': 'cancel', 'request': req.id})
        req.refresh_from_db()
        self.assertEqual(req.status, LeaveRequest.CANCELLED)
        self.assertFalse(Attendance.objects.filter(employee=self.emp, leave_request=req).exists())
        self.assertEqual(balances([self.emp], 2025)[(self.emp.id, self.annual.id)]['remaining'], D('3'))

    def test_worked_day_blocks_approval_and_pending_flow(self):
        from apps.tenant.hr.attendance_services import save_manual_day
        from apps.tenant.hr.models import LeaveRequest
        save_manual_day(self.emp, datetime.date(2025, 2, 3), 'present', check_in=datetime.time(9, 0))
        self.create(self.annual, '2025-02-03', '2025-02-04', approve=False)
        req = LeaveRequest.objects.get()
        self.assertEqual(req.status, LeaveRequest.PENDING)
        self.client.post(reverse('hr:leave_list'), {'action': 'approve', 'request': req.id})
        req.refresh_from_db()
        self.assertEqual(req.status, LeaveRequest.PENDING)            # refused: he worked on the 3rd

    def test_screen_permission(self):
        self.assertContains(self.client.get(reverse('hr:leave_list')), 'أرصدة الإجازات')
        login(self.client, make_user('no_leaves', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('hr:leave_list')).status_code, 302)


class PayrollTaxTests(TestCase):
    """ضريبة كسب العمل والتأمينات في المسير."""
    YEAR, MONTH = 2025, 2

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('tax_worker', role='CASHIER', branch=cls.w.branch)
        cls.worker.date_joined = timezone.make_aware(datetime.datetime(2020, 1, 1))
        cls.worker.save(update_fields=['date_joined'])
        emp = cls.worker.employee_profile
        emp.base_salary = D('10000')
        emp.shift_start_time, emp.shift_end_time = datetime.time(9, 0), datetime.time(17, 0)
        emp.save()
        cls.emp = emp

    def setUp(self):
        login(self.client, self.w.admin)
        self.client.post(reverse('hr:attendance_manage'), {
            'action': 'fill_period', 'date_from': '2025-02-01', 'date_to': '2025-02-28'})

    def test_bracket_math(self):
        from apps.tenant.hr.models import _default_brackets
        from apps.tenant.hr.payroll_tax import annual_tax
        b = _default_brackets()
        self.assertEqual(annual_tax(D('40000'), b), D('0'))
        # 15000 x 10% + 15000 x 15% + 10000 x 20% = 1500 + 2250 + 2000
        self.assertEqual(annual_tax(D('80000'), b), D('5750'))

    def test_disabled_by_default_then_applied_and_posted(self):
        from apps.tenant.core.tests.factories import account
        from apps.tenant.hr.models import PayrollTaxSettings
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        p = Payroll.objects.get(employee=self.emp, month=self.MONTH, year=self.YEAR)
        self.assertEqual((p.social_insurance, p.income_tax, p.net_salary), (D('0.00'), D('0.00'), D('10000.00')))
        s = PayrollTaxSettings.get()
        s.tax_enabled = s.insurance_enabled = True
        s.save()
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        p.refresh_from_db()
        self.assertEqual(p.social_insurance, D('1100.00'))                  # 10000 x 11%
        self.assertEqual(p.employer_insurance, D('1875.00'))                # 10000 x 18.75%
        # yearly taxable = (10000 - 1100) x 12 - 20000 = 86800
        # 15000x10% + 15000x15% + 16800x20% = 1500 + 2250 + 3360 = 7110 -> 592.50 a month
        self.assertEqual(p.income_tax, D('592.50'))
        self.assertEqual(p.net_salary, D('10000') - D('1100') - D('592.50'))
        self.client.post(reverse('hr:payroll_confirm', args=[p.id]))
        p.refresh_from_db()
        items = p.journal_entry.items
        self.assertTrue(items.filter(account=account('5210'), debit=D('10000')).exists())
        self.assertTrue(items.filter(account=account('5212'), debit=D('1875')).exists())
        self.assertTrue(items.filter(account=account('2165'), credit=D('2975')).exists())
        self.assertTrue(items.filter(account=account('2166'), credit=D('592.50')).exists())
        self.assertTrue(p.journal_entry.is_balanced())

    def test_insurance_wage_limits_and_exempt(self):
        from apps.tenant.hr.models import PayrollTaxSettings
        from apps.tenant.hr.payroll_tax import compute
        s = PayrollTaxSettings.get()
        s.tax_enabled = s.insurance_enabled = True
        self.emp.base_salary = D('30000')
        self.emp.tax_exempt = True
        r = compute(self.emp, D('30000'), s)
        self.assertEqual(r['social_insurance'], D('1595.00'))               # capped at 14500 x 11%
        self.assertEqual(r['income_tax'], D('0'))

    def test_settings_screen(self):
        r = self.client.get(reverse('hr:payroll_tax_settings'), {'sample': '10000'})
        self.assertEqual(r.context['sample']['income_tax'], D('592.50'))
        # decimal rates must render with a dot, or the number input shows empty and saving drops them
        self.assertContains(r, 'value="22.5"')
        self.assertContains(r, 'value="27.5"')
        self.assertContains(r, 'value="18.75"')
        self.client.post(reverse('hr:payroll_tax_settings'), {
            'insurance_enabled': 'on', 'employee_insurance_rate': '11', 'employer_insurance_rate': '18.75',
            'insurance_min_wage': '2300', 'insurance_max_wage': '14500', 'personal_exemption': '20000',
            'br_upper[]': ['40000', ''], 'br_rate[]': ['0', '10']})
        from apps.tenant.hr.models import PayrollTaxSettings
        s = PayrollTaxSettings.get()
        self.assertTrue(s.insurance_enabled and not s.tax_enabled)
        self.assertEqual(s.brackets, [[40000.0, 0.0], [None, 10.0]])


class PayrollPaymentTests(TestCase):
    """صرف المرتبات وسداد التأمينات والضرايب."""
    YEAR, MONTH = 2025, 2

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('paid_worker', role='CASHIER', branch=cls.w.branch)
        cls.worker.date_joined = timezone.make_aware(datetime.datetime(2020, 1, 1))
        cls.worker.save(update_fields=['date_joined'])
        emp = cls.worker.employee_profile
        emp.base_salary = D('10000')
        emp.shift_start_time, emp.shift_end_time = datetime.time(9, 0), datetime.time(17, 0)
        emp.save()
        cls.emp = emp

    def setUp(self):
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        from apps.tenant.core.tests.factories import account
        from apps.tenant.hr.models import PayrollTaxSettings
        login(self.client, self.w.admin)
        e = JournalEntry.objects.create(date=datetime.date(2025, 1, 1), reference='CAP-PAY', description='رأس مال')
        JournalItem.objects.create(entry=e, account=self.w.treasury.ensure_account(), debit=D('50000'), credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=D('50000'))
        e.post()
        s = PayrollTaxSettings.get()
        s.tax_enabled = s.insurance_enabled = True
        s.save()
        self.client.post(reverse('hr:attendance_manage'), {
            'action': 'fill_period', 'date_from': '2025-02-01', 'date_to': '2025-02-28'})
        self.client.post(reverse('hr:payroll_process'), {'month': self.MONTH, 'year': self.YEAR})
        self.p = Payroll.objects.get(employee=self.emp, month=self.MONTH, year=self.YEAR)

    def pay(self, kind):
        return self.client.post(reverse('hr:payroll_pay'), {'kind': kind, 'month': self.MONTH, 'year': self.YEAR,
                                                            'source': f't:{self.w.treasury.id}'})

    def test_needs_approval_then_pays_once(self):
        from apps.tenant.core.tests.factories import account
        from apps.tenant.hr.models import PayrollPayment
        self.pay('salaries')
        self.assertFalse(PayrollPayment.objects.exists())                 # not approved yet
        self.client.post(reverse('hr:payroll_confirm', args=[self.p.id]))
        self.pay('salaries')
        pm = PayrollPayment.objects.get(kind=PayrollPayment.SALARIES)
        self.assertEqual(pm.amount, D('8307.50'))
        self.assertTrue(pm.journal_entry.items.filter(account=account('2160'), debit=D('8307.50')).exists())
        self.pay('salaries')
        self.assertEqual(PayrollPayment.objects.filter(kind=PayrollPayment.SALARIES).count(), 1)
        r = self.client.get(reverse('hr:payroll_list'), {'month': self.MONTH, 'year': self.YEAR})
        self.assertContains(r, 'اتصرف')

    def test_government_dues(self):
        from apps.tenant.core.tests.factories import account
        from apps.tenant.hr.models import PayrollPayment
        self.client.post(reverse('hr:payroll_confirm', args=[self.p.id]))
        self.pay('government')
        pm = PayrollPayment.objects.get(kind=PayrollPayment.GOVERNMENT)
        self.assertEqual((pm.insurance_amount, pm.tax_amount), (D('2975.00'), D('592.50')))
        items = pm.journal_entry.items
        self.assertTrue(items.filter(account=account('2165'), debit=D('2975')).exists())
        self.assertTrue(items.filter(account=account('2166'), debit=D('592.50')).exists())
        self.pay('government')
        self.assertEqual(PayrollPayment.objects.filter(kind=PayrollPayment.GOVERNMENT).count(), 1)


class EmployeeFileTests(TestCase):
    """ملف الموظف، المستندات، العهد العينية، نهاية الخدمة، صفحتي، وطلبات السلف."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.worker = make_user('file_worker', role='CASHIER', branch=cls.w.branch)
        cls.worker.first_name = 'سعيد'
        cls.worker.save()
        emp = cls.worker.employee_profile
        emp.base_salary = D('6000')
        emp.save()
        cls.emp = emp

    def setUp(self):
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        from apps.tenant.core.tests.factories import account
        login(self.client, self.w.admin)
        e = JournalEntry.objects.create(date=datetime.date(2025, 1, 1), reference='CAP-EF', description='رأس مال')
        JournalItem.objects.create(entry=e, account=self.w.treasury.ensure_account(), debit=D('50000'), credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=D('50000'))
        e.post()

    def test_profile_documents_assets_and_expiry_alert(self):
        from apps.tenant.hr.models import Department, EmployeeAsset, EmployeeDocument
        dept = Department.objects.create(name='المبيعات')
        url = reverse('hr:employee_file', args=[self.emp.id])
        self.client.post(url, {'action': 'profile', 'department': dept.id, 'hire_date': '2022-01-01', 'phone': '010'})
        self.emp.refresh_from_db()
        self.assertEqual(self.emp.hr_profile.department, dept)
        soon = (timezone.localdate() + datetime.timedelta(days=10)).isoformat()
        self.client.post(url, {'action': 'document', 'doc_type': 'HEALTH', 'expiry_date': soon})
        self.client.post(url, {'action': 'asset', 'item': 'لابتوب', 'value': '15000'})
        self.assertEqual(EmployeeDocument.objects.filter(employee=self.emp).count(), 1)
        r = self.client.get(reverse('hr:employee_list'))
        self.assertEqual(len(r.context['expiring']), 1)
        r = self.client.get(reverse('core:main_screen'))
        self.assertTrue(any('مستند موظف' in a['text'] for a in r.context['alerts']))
        asset = EmployeeAsset.objects.get()
        self.client.post(url, {'action': 'return_asset', 'asset': asset.id})
        asset.refresh_from_db()
        self.assertIsNotNone(asset.returned_on)

    def test_end_of_service_with_loan_and_open_asset(self):
        from apps.tenant.core.tests.factories import account
        from apps.tenant.hr.models import EmployeeAsset, EmployeeLoan, EmployeeProfile, EndOfService
        from apps.tenant.hr.loan_services import create_cash_loan
        EmployeeProfile.objects.create(employee=self.emp, hire_date=datetime.date(2020, 1, 1))
        create_cash_loan(self.emp, D('1000'), 2, 2025, 1, self.w.treasury, user=self.w.admin)
        asset = EmployeeAsset.objects.create(employee=self.emp, item='يونيفورم')
        url = reverse('hr:end_of_service', args=[self.emp.id])
        data = {'termination_date': '2025-06-30', 'reason': 'استقالة', 'severance': '3000', 'leave_days': '10',
                'source': f't:{self.w.treasury.id}'}
        self.client.post(url, data)
        self.assertFalse(EndOfService.objects.exists())                  # asset not returned yet
        asset.returned_on = datetime.date(2025, 6, 30)
        asset.save()
        self.client.post(url, data)
        eos = EndOfService.objects.get()
        self.assertEqual((eos.leave_encashment, eos.loans_deducted), (D('2000.00'), D('1000.00')))   # 10 x 200
        self.assertEqual(eos.net_amount, D('4000.00'))
        items = eos.journal_entry.items
        self.assertTrue(items.filter(account=account('5210'), debit=D('5000')).exists())
        self.assertTrue(items.filter(account=account('1135'), credit=D('1000')).exists())
        self.worker.refresh_from_db()
        self.assertFalse(self.worker.is_active)
        self.assertEqual(EmployeeLoan.objects.get().status, EmployeeLoan.SETTLED)

    def test_my_portal_requests_and_manager_approval(self):
        from apps.tenant.hr.models import EmployeeLoan, LeaveRequest, LeaveType, LoanRequest
        login(self.client, self.worker)
        r = self.client.get(reverse('hr:my_portal'))
        self.assertContains(r, 'صفحتي')
        annual = LeaveType.objects.get(name='اعتيادية')
        self.client.post(reverse('hr:my_portal'), {'action': 'leave', 'leave_type': annual.id,
                                                   'start_date': '2026-12-01', 'end_date': '2026-12-02'})
        self.client.post(reverse('hr:my_portal'), {'action': 'loan', 'amount': '600', 'installments': '3'})
        self.assertEqual(LeaveRequest.objects.get().status, LeaveRequest.PENDING)
        req = LoanRequest.objects.get()
        login(self.client, self.w.admin)
        self.client.post(reverse('hr:loan_request_decide', args=[req.id]), {
            'action': 'approve', 'amount': '600', 'installments': '3', 'start': '2026-12', 'treasury': self.w.treasury.id})
        req.refresh_from_db()
        self.assertEqual(req.status, LoanRequest.APPROVED)
        self.assertEqual(EmployeeLoan.objects.get().monthly_installment, D('200.00'))

    def test_permissions(self):
        login(self.client, self.worker)
        self.assertEqual(self.client.get(reverse('hr:employee_list')).status_code, 302)
        self.assertEqual(self.client.get(reverse('hr:employee_file', args=[self.emp.id])).status_code, 302)
