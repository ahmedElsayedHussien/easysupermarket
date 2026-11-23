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
