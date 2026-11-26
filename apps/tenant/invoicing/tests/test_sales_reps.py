"""مناديب المبيعات: الفاتورة بتتعلّم باسمه، الأداء، العمولة في المسير."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.tenant.core.tests.factories import build_world, login, make_invoice, make_user, make_warehouse, stock_in
from apps.tenant.invoicing.models import RepCommission, SalesRep
from apps.tenant.invoicing.rep_services import month_performance


class SalesRepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.user = make_user('rep_user', role='CASHIER', branch=cls.w.branch)
        cls.user.date_joined = timezone.make_aware(datetime.datetime(2020, 1, 1))
        cls.user.save(update_fields=['date_joined'])
        emp = cls.user.employee_profile
        emp.base_salary = D('4000')
        emp.save()
        cls.van = make_warehouse(cls.w.branch, 'عربية 1', code='VAN1')
        cls.rep = SalesRep.objects.create(employee=emp, van_warehouse=cls.van, commission_rate=D('2'),
                                          monthly_target=D('1000'))

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 100, D('5'))

    def test_performance_net_of_returns_and_commission_in_payroll(self):
        today = timezone.localdate()
        make_invoice(self.w, lines=[(self.w.product, 50, D('10'))], sales_rep=self.rep)            # 500
        make_invoice(self.w, invoice_type='RETURN_SALE', lines=[(self.w.product, 5, D('10'))], sales_rep=self.rep)
        p = month_performance([self.rep], today.year, today.month)[self.rep.id]
        self.assertEqual((p['net'], p['achievement'], p['commission']), (D('450'), 45, D('9.00')))
        login(self.client, self.w.admin)
        self.client.post(reverse('invoicing:rep_detail', args=[self.rep.id]),
                         {'action': 'approve', 'year': today.year, 'month': today.month})
        self.assertEqual(RepCommission.objects.get().amount, D('9.00'))
        self.client.post(reverse('hr:payroll_process'), {'month': today.month, 'year': today.year})
        from apps.tenant.hr.models import Payroll
        pay = Payroll.objects.get(employee=self.rep.employee, month=today.month, year=today.year)
        self.assertEqual(pay.commission_pay, D('9.00'))
        self.client.post(reverse('hr:payroll_confirm', args=[pay.id]))
        pay.refresh_from_db()
        from apps.tenant.core.tests.factories import account
        self.assertTrue(pay.journal_entry.items.filter(account=account('5215'), debit=D('9')).exists())

    def test_pos_sale_by_rep_is_tagged(self):
        from apps.tenant.invoicing.rep_services import rep_for_user
        self.assertEqual(rep_for_user(self.user), self.rep)
        self.assertIsNone(rep_for_user(self.w.admin))

    def test_screens_and_van_transfer_preset(self):
        login(self.client, self.w.admin)
        r = self.client.get(reverse('invoicing:rep_list'))
        self.assertContains(r, 'مناديب المبيعات')
        r = self.client.get(reverse('invoicing:rep_detail', args=[self.rep.id]))
        self.assertContains(r, 'تحميل العربية')
        r = self.client.get(reverse('inventory:transfer_stock') + f'?from={self.w.warehouse.id}&to={self.van.id}')
        self.assertEqual(r.context['posted']['to_warehouse'], str(self.van.id))
        login(self.client, make_user('no_reps', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('invoicing:rep_list')).status_code, 302)
