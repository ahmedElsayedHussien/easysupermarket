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
