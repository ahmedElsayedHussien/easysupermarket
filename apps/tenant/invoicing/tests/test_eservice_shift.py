"""E-payment services (Fawry/Aman/...) in shift reconciliation, liquidity and the e-service dashboard."""
import datetime
from decimal import Decimal as D

from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.tenant.accounting.eservice_dashboard_views import (
    eservice_cash_effect_expression, eservice_cash_for_session, eservice_dashboard,
)
from apps.tenant.accounting.models import EServiceTransaction, POSMachine
from apps.tenant.core.tests.factories import build_world, login, make_branch, make_partner, make_treasury
from apps.tenant.invoicing.models import POSSession

T = EServiceTransaction


def make_machine(branch, name='فوري 1', **kw):
    m = POSMachine.objects.create(name=name, machine_type=POSMachine.FAWRY, branch=branch, **kw)
    m.refresh_from_db()
    return m


def make_op(w, machine, ttype, principal, rev=D('0'), exp=D('0'), status=T.POSTED, session=None, **kw):
    """Plain row (no journal entry): enough for the drawer / dashboard arithmetic."""
    return T.objects.create(
        transaction_type=ttype, pos_machine=machine, treasury=kw.pop('treasury', w.treasury),
        principal_amount=principal, commission_revenue=rev, commission_expense=exp,
        status=status, pos_session=session, **kw)


class EServiceShiftCashTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.machine = make_machine(cls.w.branch)
        cls.agent = make_partner('مندوب فوري', 'SUPPLIER')

    def setUp(self):
        self.session = POSSession.objects.create(
            branch=self.w.branch, treasury=self.w.treasury, user=self.w.cashier, opening_balance=D('1000'))

    def _ops(self):
        s = self.session
        return [
            make_op(self.w, self.machine, T.CASH_OUT, D('500'), rev=D('5'), session=s),      # -495
            make_op(self.w, self.machine, T.CASH_IN, D('200'), rev=D('3'), session=s),       # +203
            make_op(self.w, self.machine, T.MERCHANT_RECHARGE, D('1000'), exp=D('10'),
                    agent=self.agent, paid_on_credit=True, session=s),                        # 0 (credit)
            make_op(self.w, self.machine, T.MERCHANT_RECHARGE, D('100'), exp=D('2'), session=s),  # -102
            make_op(self.w, self.machine, T.CASH_IN, D('999'), rev=D('9'), status=T.CANCELLED, session=s),  # 0
            make_op(self.w, self.machine, T.CASH_IN, D('50'), rev=D('1'), status=T.DRAFT, session=s),       # 0
            make_op(self.w, self.machine, T.CASH_IN, D('70'), rev=D('1')),                    # other shift: ignored
        ]

    def test_sql_expression_matches_cash_effect_property(self):
        ops = self._ops()
        sql = dict(T.objects.annotate(fx=eservice_cash_effect_expression()).values_list('id', 'fx'))
        for op in ops:
            self.assertEqual(D(sql[op.id]).quantize(D('0.01')), op.cash_effect.quantize(D('0.01')),
                             f'{op.transaction_type} {op.status} credit={op.paid_on_credit}')

    def test_session_total(self):
        self._ops()
        self.assertEqual(eservice_cash_for_session(self.session), D('-394'))  # -495 + 203 + 0 - 102

    def test_close_shift_expected_includes_eservice(self):
        make_op(self.w, self.machine, T.CASH_OUT, D('300'), rev=D('3'), session=self.session)   # -297
        make_op(self.w, self.machine, T.CASH_IN, D('100'), rev=D('2'), session=self.session)    # +102
        make_op(self.w, self.machine, T.MERCHANT_RECHARGE, D('400'), agent=self.agent,
                paid_on_credit=True, session=self.session)                                     # 0
        c = login(self.client, self.w.cashier)
        r = c.get(f"{reverse('invoicing:pos_close_shift')}?session_id={self.session.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_eservice_cash'], D('-195'))
        self.assertEqual(r.context['expected_balance'], D('805'))
        self.assertContains(r, 'خدمات الدفع الإلكتروني (صافي نقدي)')

        r = c.post(reverse('invoicing:pos_close_shift'), {'session_id': self.session.id, 'actual_balance': '805'})
        self.assertEqual(r.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.closing_balance_expected, D('805'))
        self.assertEqual(self.session.difference, D('0'))

    def test_cash_out_reduces_and_cash_in_increases(self):
        c = login(self.client, self.w.cashier)
        base = c.get(reverse('invoicing:pos_shift_stats_api')).json()['cash_in_drawer']
        op = make_op(self.w, self.machine, T.CASH_OUT, D('100'), rev=D('2'), session=self.session)
        self.assertAlmostEqual(c.get(reverse('invoicing:pos_shift_stats_api')).json()['cash_in_drawer'], base - 98)
        op.delete()
        make_op(self.w, self.machine, T.CASH_IN, D('100'), rev=D('2'), session=self.session)
        data = c.get(reverse('invoicing:pos_shift_stats_api')).json()
        self.assertAlmostEqual(data['cash_in_drawer'], base + 102)
        self.assertAlmostEqual(data['eservice_cash'], 102)

    def test_shift_report_detail_includes_eservice(self):
        from apps.tenant.core.reports_views import get_pos_session_details
        make_op(self.w, self.machine, T.CASH_OUT, D('200'), rev=D('4'), session=self.session)  # -196
        dt = get_pos_session_details(self.session)
        self.assertEqual(dt['total_eservice_cash'], D('-196'))
        self.assertEqual(dt['expected_balance'], D('804'))
        self.assertEqual(dt['eservices_count'], 1)
        r = login(self.client, self.w.admin).get(f'/reports/sales/shifts/{self.session.id}/')
        self.assertContains(r, 'خدمات الدفع الإلكتروني (صافي نقدي)')


class EServiceLiquidityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.treasury2 = make_treasury(cls.branch2, 'خزينة 2')
        cls.machine = make_machine(cls.w.branch, 'فوري الرئيسي', min_balance_alert=D('500'))
        cls.machine2 = make_machine(cls.branch2, 'أمان فرع 2')

    def setUp(self):
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        from apps.tenant.core.tests.factories import account
        self.w.treasury.ensure_account()
        self.w.treasury.refresh_from_db()
        # Fund the drawer outside the 30-day flow window (posting checks the balance)
        e = JournalEntry.objects.create(date=datetime.date.today() - datetime.timedelta(days=90),
                                        reference='FUND-1', description='capital', status=JournalEntry.POSTED)
        JournalItem.objects.create(entry=e, account=self.w.treasury.account, debit=D('5000'), credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=D('5000'))

    def _post(self, ttype, principal, rev=D('0'), exp=D('0'), machine=None, treasury=None):
        op = T.objects.create(transaction_type=ttype, pos_machine=machine or self.machine,
                              treasury=treasury or self.w.treasury, principal_amount=principal,
                              commission_revenue=rev, commission_expense=exp)
        op.post_transaction()
        op.refresh_from_db()
        return op

    def test_machines_are_a_liquidity_type(self):
        self._post(T.CASH_OUT, D('300'), rev=D('5'))  # machine +300, treasury -295, revenue +5
        c = login(self.client, self.w.admin)
        c.get('/?branch=all')
        r = c.get('/accounting/liquidity/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['totals']['POS'], D('300'))
        self.assertEqual({row['obj'] for row in r.context['machine_rows']}, {self.machine, self.machine2})
        comp = {x['key']: x for x in r.context['composition']}
        self.assertEqual(comp['POS']['series'], 4)
        self.assertEqual(r.context['grand_total'], D('5005'))  # 5000 funding + 300 machine - 295 drawer
        # flow: the principal is internal; only the net commission (5) counts as inflow
        self.assertEqual(r.context['total_in'], 5.0)
        self.assertEqual(r.context['total_out'], 0.0)
        self.assertEqual(r.context['low_machines'], 1)  # 300 < 500 alert
        self.assertContains(r, 'ماكينات الدفع')
        self.assertContains(r, f'?type=MERCHANT_RECHARGE&machine={self.machine.pk}')

    def test_machines_follow_branch_scope(self):
        c = login(self.client, self.w.admin)
        c.get(f'/?branch={self.w.branch.id}')
        r = c.get('/accounting/liquidity/')
        self.assertEqual([row['obj'] for row in r.context['machine_rows']], [self.machine])

    def test_recharge_fee_is_outflow(self):
        self._post(T.MERCHANT_RECHARGE, D('1000'), exp=D('10'))  # machine +1000, treasury -1010
        c = login(self.client, self.w.admin)
        c.get('/?branch=all')
        r = c.get('/accounting/liquidity/')
        self.assertEqual(r.context['total_in'], 0.0)
        self.assertEqual(r.context['total_out'], 10.0)


class EServiceDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.treasury2 = make_treasury(cls.branch2, 'خزينة 2')
        cls.machine = make_machine(cls.w.branch, 'فوري', min_balance_alert=D('1000'))
        cls.machine_ok = make_machine(cls.w.branch, 'أمان')                       # no alert (0)
        cls.machine2 = make_machine(cls.branch2, 'بنك فرع 2', min_balance_alert=D('1000'))

    def _get(self, user, branch=None):
        req = RequestFactory().get('/accounting/eservice/dashboard/')
        req.user = user
        req.session = {}
        req.branch = branch
        req._messages = None
        return req

    def _render(self, user, branch=None):
        req = self._get(user, branch)
        captured = {}
        from unittest import mock
        with mock.patch('apps.tenant.accounting.eservice_dashboard_views.render',
                        side_effect=lambda request, tpl, ctx: captured.update(ctx) or tpl):
            eservice_dashboard(req)
        return captured

    def test_month_commissions_and_low_balance(self):
        today = datetime.date.today()
        last_month = today.replace(day=1) - datetime.timedelta(days=1)
        make_op(self.w, self.machine, T.CASH_OUT, D('500'), rev=D('5'))
        make_op(self.w, self.machine, T.CASH_IN, D('300'), rev=D('3'))
        make_op(self.w, self.machine, T.MERCHANT_RECHARGE, D('400'), exp=D('4'))
        make_op(self.w, self.machine, T.CASH_IN, D('100'), rev=D('7'), status=T.CANCELLED)  # ignored
        make_op(self.w, self.machine, T.CASH_IN, D('100'), rev=D('11'), date=last_month)     # other month
        make_op(self.w, self.machine2, T.CASH_IN, D('100'), rev=D('13'), treasury=self.treasury2)  # other branch

        ctx = self._render(self.w.admin, branch=self.w.branch)
        self.assertEqual(ctx['month_earned'], D('8'))
        self.assertEqual(ctx['month_fees'], D('4'))
        self.assertEqual(ctx['month_net'], D('4'))
        self.assertEqual(ctx['today_count'], 3)
        self.assertEqual(ctx['today_volume'], D('1200'))
        self.assertEqual([r['obj'] for r in ctx['low_balance']], [self.machine])  # 0 < 1000; machine_ok has no alert
        rows = {r['obj']: r for r in ctx['machine_rows']}
        self.assertNotIn(self.machine2, rows)
        self.assertEqual(rows[self.machine]['month_commission'], D('8'))
        self.assertEqual(rows[self.machine]['today_volume'], D('1200'))
        self.assertEqual(ctx['chart_data']['net_commission'][-1], 4.0)
        self.assertEqual(len(ctx['chart_data']['types']), 3)

        ctx_all = self._render(self.w.admin, branch=None)
        self.assertEqual(ctx_all['month_earned'], D('21'))
        self.assertEqual({r['obj'] for r in ctx_all['low_balance']}, {self.machine, self.machine2})

    def test_template_renders(self):
        from django.template.loader import render_to_string
        make_op(self.w, self.machine, T.CASH_OUT, D('500'), rev=D('5'))
        ctx = self._render(self.w.admin, branch=self.w.branch)
        html = render_to_string('accounting/eservice_dashboard.html', ctx, request=self._get(self.w.admin))
        self.assertIn('لوحة خدمات الدفع الإلكتروني', html)
        self.assertIn('رصيد منخفض', html)
        if ctx['commission_rules_url']:
            self.assertIn(ctx['commission_rules_url'], html)
        else:
            self.assertNotIn('🧮 قواعد العمولات', html)

    def test_permission_required(self):
        req = self._get(self.w.cashier)
        from django.contrib.messages.storage.fallback import FallbackStorage
        req._messages = FallbackStorage(req)
        r = eservice_dashboard(req)
        self.assertEqual(r.status_code, 302)
