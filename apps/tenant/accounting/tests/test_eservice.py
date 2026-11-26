"""
E-payment services (Fawry / Aman / bank machines, e-wallets):
cash-in / cash-out for customers and machine recharge from an agent.
"""
import datetime
from decimal import Decimal as D
from io import StringIO

from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from apps.tenant.accounting.models import (
    EServiceCommissionRule, EServiceTransaction, JournalEntry, JournalItem, POSMachine,
)
from apps.tenant.core.tests.factories import (
    account, add_perms, build_world, ledger_totals, login, make_branch, make_partner, make_treasury, make_user,
)

ESVC_PERMS = ('accounting.view_eservicetransaction', 'accounting.add_eservicetransaction')


def seed(acc, amount, ref):
    """Puts ``amount`` on asset account ``acc`` (against capital 3100)."""
    e = JournalEntry.objects.create(date=datetime.date.today(), reference=ref, description='seed',
                                    status=JournalEntry.POSTED)
    JournalItem.objects.create(entry=e, account=acc, debit=amount, credit=0)
    JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=amount)
    e.post()


def bal(acc):
    acc.refresh_from_db()
    return acc.current_balance


class EServiceBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.w.treasury.ensure_account()
        cls.w.treasury.refresh_from_db()
        cls.machine = POSMachine.objects.create(name='فوري 1', machine_type=POSMachine.FAWRY, branch=cls.w.branch)
        cls.machine.refresh_from_db()
        cls.cashier = make_user('esvc_cashier', role='CASHIER', branch=cls.w.branch, perms=ESVC_PERMS)
        cls.agent = make_partner('مندوب فوري', 'SUPPLIER')

    def setUp(self):
        seed(self.w.treasury.account, D('1000'), 'SEED-T')
        seed(self.machine.account, D('500'), 'SEED-M')

    def make_txn(self, ttype, principal, commission=D('0'), fee=D('0'), **kw):
        return EServiceTransaction.objects.create(
            transaction_type=ttype, source_type='MACHINE', pos_machine=kw.pop('machine', self.machine),
            treasury=kw.pop('treasury', self.w.treasury), principal_amount=D(str(principal)),
            commission_revenue=D(str(commission)), commission_expense=D(str(fee)),
            created_by=kw.pop('created_by', self.cashier), **kw)

    def post_center(self, user=None, **data):
        payload = {'transaction_type': 'CASH_OUT', 'source_type': 'MACHINE', 'pos_machine_id': self.machine.pk,
                   'treasury_id': self.w.treasury.pk, 'principal_amount': '100', 'description': 'test'}
        payload.update({k: str(v) for k, v in data.items()})
        c = login(self.client, user or self.cashier)
        return c.post('/accounting/eservice/', payload, follow=True)


class PostingTests(EServiceBase):
    def test_cash_out_books_commission_on_4240_without_invoice(self):
        from apps.tenant.invoicing.models import Invoice
        invoices_before = Invoice.objects.count()
        self.post_center(principal_amount='100', commission_revenue='5')
        txn = EServiceTransaction.objects.get()
        self.assertEqual(txn.status, EServiceTransaction.POSTED)
        self.assertIsNone(txn.invoice)
        self.assertEqual(Invoice.objects.count(), invoices_before)
        self.assertEqual(account('4240').current_balance, D('5'))
        self.assertEqual(account('4240').account_type, 'REVENUE')
        self.assertEqual(bal(self.w.treasury.account), D('905'))
        self.assertEqual(bal(self.machine.account), D('600'))
        self.assertEqual(txn.cash_effect, D('-95'))
        self.assertEqual(txn.journal_entry.content_type.model, 'eservicetransaction')
        d, c = ledger_totals()
        self.assertEqual(d, c)

    def test_cash_in_and_revenue_account_created_lazily(self):
        from apps.tenant.accounting.models import Account
        Account.objects.filter(code='4240').delete()
        txn = self.make_txn('CASH_IN', 200, commission=3)
        txn.post_transaction()
        self.assertEqual(account('4240').current_balance, D('3'))
        self.assertEqual(account('4240').parent.code, '4000')
        self.assertEqual(bal(self.w.treasury.account), D('1203'))
        self.assertEqual(bal(self.machine.account), D('300'))

    def test_recharge_fee_on_5305(self):
        txn = self.make_txn('MERCHANT_RECHARGE', 300, fee=2, agent=self.agent)
        txn.post_transaction()
        self.assertEqual(bal(self.machine.account), D('800'))
        self.assertEqual(bal(self.w.treasury.account), D('698'))
        self.assertEqual(account('5305').current_balance, D('2'))

    def test_cancelled_cannot_be_reposted(self):
        txn = self.make_txn('CASH_IN', 50)
        txn.post_transaction()
        txn.cancel_transaction(user=self.w.admin, reason='خطأ')
        with self.assertRaises(ValidationError):
            txn.post_transaction()


class BalanceCheckTests(EServiceBase):
    def test_cash_in_requires_machine_balance(self):
        txn = self.make_txn('CASH_IN', 501)
        with self.assertRaisesMessage(ValidationError, '500.00'):
            txn.post_transaction()
        txn.refresh_from_db()
        self.assertEqual(txn.status, EServiceTransaction.DRAFT)
        self.assertFalse(JournalEntry.objects.filter(reference__startswith=txn.transaction_number).exists())

    def test_cash_out_requires_treasury_balance_net_of_commission(self):
        self.make_txn('CASH_OUT', 1010, commission=10).post_transaction()   # needs exactly 1000
        self.assertEqual(bal(self.w.treasury.account), D('0'))
        with self.assertRaises(ValidationError):
            self.make_txn('CASH_OUT', 10, commission=1).post_transaction()

    def test_recharge_cash_requires_principal_plus_fee(self):
        with self.assertRaises(ValidationError):
            self.make_txn('MERCHANT_RECHARGE', 999, fee=2).post_transaction()
        self.make_txn('MERCHANT_RECHARGE', 998, fee=2).post_transaction()
        self.assertEqual(bal(self.w.treasury.account), D('0'))

    def test_view_shows_error_and_creates_nothing_posted(self):
        r = self.post_center(transaction_type='CASH_IN', principal_amount='9999')
        self.assertContains(r, 'غير كافٍ')
        self.assertFalse(EServiceTransaction.objects.filter(status='POSTED').exists())


class CreditRechargeTests(EServiceBase):
    def test_credit_recharge_hits_agent_payable(self):
        txn = self.make_txn('MERCHANT_RECHARGE', 5000, fee=10, agent=self.agent, paid_on_credit=True)
        txn.post_transaction()   # no treasury balance needed
        self.agent.refresh_from_db()
        payable = self.agent.payable_account
        self.assertIsNotNone(payable)
        self.assertEqual(payable.current_balance, D('5010'))
        self.assertEqual(bal(self.w.treasury.account), D('1000'))
        self.assertEqual(bal(self.machine.account), D('5500'))
        self.assertEqual(txn.cash_effect, D('0'))

    def test_credit_recharge_on_agent_statement_and_balance(self):
        from apps.tenant.partners.models import Partner
        from apps.tenant.partners.services import batch_get_partners_balances, get_partner_full_statement
        txn = self.make_txn('MERCHANT_RECHARGE', 5000, fee=10, agent=self.agent, paid_on_credit=True)
        txn.post_transaction()
        agent = Partner.objects.get(pk=self.agent.pk)
        self.assertEqual(agent.supplier_balance, D('5010'))                       # we owe the agent
        self.assertEqual(batch_get_partners_balances(partner_ids=[agent.pk])[agent.pk]['final_balance'], D('-5010'))
        statement = get_partner_full_statement(agent)
        lines = statement['transactions'] if isinstance(statement, dict) and 'transactions' in statement else statement
        refs = str(lines)
        self.assertIn(txn.transaction_number, refs)
        # cancelled -> disappears from the balance
        txn.cancel_transaction(self.w.admin, 'خطأ')
        agent = Partner.objects.get(pk=self.agent.pk)
        self.assertEqual(agent.supplier_balance, D('0'))

    def test_credit_requires_agent(self):
        with self.assertRaises(ValidationError):
            self.make_txn('MERCHANT_RECHARGE', 100, paid_on_credit=True).post_transaction()

    def test_agent_must_be_supplier(self):
        customer = make_partner('عميل', 'CUSTOMER')
        with self.assertRaises(ValidationError):
            self.make_txn('MERCHANT_RECHARGE', 100, agent=customer).post_transaction()

    def test_credit_only_for_recharge(self):
        with self.assertRaises(ValidationError):
            self.make_txn('CASH_OUT', 100, paid_on_credit=True, agent=self.agent).post_transaction()

    def test_view_credit_recharge(self):
        self.post_center(transaction_type='MERCHANT_RECHARGE', principal_amount='2000',
                         commission_expense='0', agent_id=self.agent.pk, paid_on_credit='1')
        txn = EServiceTransaction.objects.get()
        self.assertEqual(txn.status, 'POSTED')
        self.assertTrue(txn.paid_on_credit)
        self.assertEqual(txn.agent, self.agent)


class CancelTests(EServiceBase):
    def setUp(self):
        super().setUp()
        self.txn = self.make_txn('CASH_OUT', 100, commission=5)
        self.txn.post_transaction()
        self.canceller = make_user('canceller', role='CASHIER', branch=self.w.branch,
                                   perms=ESVC_PERMS + ('accounting.cancel_eservicetransaction',))

    def test_cancel_reverses_entry(self):
        c = login(self.client, self.canceller)
        c.post(f'/accounting/eservice/{self.txn.pk}/cancel/', {'reason': 'عملية خاطئة'})
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, EServiceTransaction.CANCELLED)
        self.assertEqual(self.txn.cancelled_by, self.canceller)
        self.assertIsNotNone(self.txn.cancelled_at)
        self.assertEqual(self.txn.cancel_reason, 'عملية خاطئة')
        rev = self.txn.reversal_entry
        self.assertIsNotNone(rev)
        self.assertEqual(rev.date, datetime.date.today())
        self.assertEqual(bal(self.w.treasury.account), D('1000'))
        self.assertEqual(bal(self.machine.account), D('500'))
        self.assertEqual(account('4240').current_balance, D('0'))
        self.assertEqual(self.txn.cash_effect, D('0'))

    def test_cancel_requires_reason(self):
        c = login(self.client, self.canceller)
        c.post(f'/accounting/eservice/{self.txn.pk}/cancel/', {'reason': '  '})
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'POSTED')

    def test_cancel_requires_permission(self):
        c = login(self.client, self.cashier)
        c.post(f'/accounting/eservice/{self.txn.pk}/cancel/', {'reason': 'x'})
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'POSTED')

    def test_admin_can_cancel_and_get_not_allowed(self):
        c = login(self.client, self.w.admin)
        self.assertEqual(c.get(f'/accounting/eservice/{self.txn.pk}/cancel/').status_code, 405)
        c.post(f'/accounting/eservice/{self.txn.pk}/cancel/', {'reason': 'x'})
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'CANCELLED')

    def test_cancel_twice_refused(self):
        self.txn.cancel_transaction(user=self.w.admin, reason='a')
        with self.assertRaises(ValidationError):
            self.txn.cancel_transaction(user=self.w.admin, reason='b')
        self.assertEqual(JournalEntry.objects.filter(reversal_of=self.txn.journal_entry).count(), 1)

    def test_draft_cancel_without_entry(self):
        draft = self.make_txn('CASH_IN', 10)
        n = JournalEntry.objects.count()
        draft.cancel_transaction(user=self.w.admin, reason='مسودة')
        self.assertEqual(draft.status, 'CANCELLED')
        self.assertEqual(JournalEntry.objects.count(), n)

    def test_detail_shows_cancel_button(self):
        c = login(self.client, self.canceller)
        r = c.get(f'/accounting/eservice/{self.txn.pk}/')
        self.assertContains(r, f'/accounting/eservice/{self.txn.pk}/cancel/')


class CommissionRuleTests(EServiceBase):
    def test_matching_precedence(self):
        generic = EServiceCommissionRule.objects.create(name='عامة', transaction_type='CASH_OUT', fixed_amount=D('1'))
        self.assertEqual(EServiceCommissionRule.match('CASH_OUT', self.machine), generic)
        self.assertEqual(EServiceCommissionRule.match('CASH_OUT', None), generic)
        by_type = EServiceCommissionRule.objects.create(name='فوري', transaction_type='CASH_OUT',
                                                        machine_type='FAWRY', fixed_amount=D('2'))
        self.assertEqual(EServiceCommissionRule.match('CASH_OUT', self.machine), by_type)
        self.assertEqual(EServiceCommissionRule.match('CASH_OUT', None), generic)
        by_machine = EServiceCommissionRule.objects.create(name='ماكينة', transaction_type='CASH_OUT',
                                                           pos_machine=self.machine, fixed_amount=D('3'))
        self.assertEqual(EServiceCommissionRule.match('CASH_OUT', self.machine), by_machine)
        self.assertIsNone(EServiceCommissionRule.match('CASH_IN', self.machine))
        by_machine.is_active = False
        by_machine.save()
        self.assertEqual(EServiceCommissionRule.match('CASH_OUT', self.machine), by_type)

    def test_compute_clamps(self):
        r = EServiceCommissionRule(fixed_amount=D('1'), percent=D('1'), min_amount=D('3'), max_amount=D('20'))
        self.assertEqual(r.compute(100), D('3.00'))
        self.assertEqual(r.compute(1000), D('11.00'))
        self.assertEqual(r.compute(5000), D('20.00'))

    def test_blank_commission_uses_rule(self):
        EServiceCommissionRule.objects.create(name='1%', transaction_type='CASH_OUT', percent=D('1'))
        self.post_center(principal_amount='300', commission_revenue='')
        txn = EServiceTransaction.objects.get()
        self.assertEqual(txn.commission_revenue, D('3.00'))
        self.assertFalse(txn.commission_overridden)

    def test_different_commission_rejected_without_permission(self):
        EServiceCommissionRule.objects.create(name='1%', transaction_type='CASH_OUT', percent=D('1'))
        r = self.post_center(principal_amount='300', commission_revenue='10')
        self.assertContains(r, 'تختلف عن العمولة المحسوبة')
        self.assertFalse(EServiceTransaction.objects.exists())

    def test_override_permission_allows_and_flags(self):
        EServiceCommissionRule.objects.create(name='1%', transaction_type='CASH_OUT', percent=D('1'))
        supervisor = make_user('esvc_sup', role='CASHIER', branch=self.w.branch,
                               perms=ESVC_PERMS + ('accounting.override_eservice_commission',))
        self.post_center(user=supervisor, principal_amount='300', commission_revenue='10')
        txn = EServiceTransaction.objects.get()
        self.assertEqual(txn.commission_revenue, D('10.00'))
        self.assertTrue(txn.commission_overridden)

    def test_no_rule_manual_value_accepted(self):
        self.post_center(principal_amount='300', commission_revenue='7')
        txn = EServiceTransaction.objects.get()
        self.assertEqual(txn.commission_revenue, D('7.00'))
        self.assertFalse(txn.commission_overridden)

    def test_recharge_rule_sets_fee(self):
        EServiceCommissionRule.objects.create(name='رسوم', transaction_type='MERCHANT_RECHARGE', fixed_amount=D('4'))
        self.post_center(transaction_type='MERCHANT_RECHARGE', principal_amount='100', commission_expense='')
        txn = EServiceTransaction.objects.get()
        self.assertEqual(txn.commission_expense, D('4.00'))
        self.assertEqual(txn.commission_revenue, D('0'))

    def test_json_endpoint(self):
        EServiceCommissionRule.objects.create(name='فوري', transaction_type='CASH_IN', machine_type='FAWRY',
                                              fixed_amount=D('1'), percent=D('0.5'))
        c = login(self.client, self.cashier)
        r = c.get('/accounting/eservice/commission/', {'type': 'CASH_IN', 'machine': self.machine.pk, 'amount': '200'})
        data = r.json()
        self.assertTrue(data['has_rule'])
        self.assertEqual(data['commission'], '2.00')
        self.assertEqual(data['field'], 'commission_revenue')
        r = c.get('/accounting/eservice/commission/', {'type': 'CASH_OUT', 'machine': '', 'amount': '200'})
        self.assertFalse(r.json()['has_rule'])
        self.assertEqual(c.get('/accounting/eservice/commission/', {'type': 'X'}).status_code, 400)

    def test_rule_crud_pages(self):
        c = login(self.client, self.w.admin)
        self.assertEqual(c.get('/accounting/eservice/rules/').status_code, 200)
        c.post('/accounting/eservice/rules/create/', {
            'name': 'قاعدة', 'transaction_type': 'CASH_OUT', 'pos_machine': '', 'machine_type': '',
            'fixed_amount': '1', 'percent': '0', 'min_amount': '0', 'max_amount': '0', 'is_active': 'on'})
        rule = EServiceCommissionRule.objects.get()
        self.assertEqual(c.get(f'/accounting/eservice/rules/{rule.pk}/edit/').status_code, 200)
        self.assertEqual(c.get(f'/accounting/eservice/rules/{rule.pk}/delete/').status_code, 405)
        c.post(f'/accounting/eservice/rules/{rule.pk}/delete/')
        self.assertFalse(EServiceCommissionRule.objects.exists())

    def test_rule_pages_need_permission(self):
        c = login(self.client, self.cashier)
        c.post('/accounting/eservice/rules/create/', {
            'name': 'قاعدة', 'transaction_type': 'CASH_OUT', 'fixed_amount': '1', 'percent': '0',
            'min_amount': '0', 'max_amount': '0'})
        self.assertFalse(EServiceCommissionRule.objects.exists())


class BranchScopingTests(EServiceBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.treasury2 = make_treasury(cls.branch2, 'خزينة 2')
        cls.treasury2.ensure_account()
        cls.treasury2.refresh_from_db()
        cls.machine2 = POSMachine.objects.create(name='أمان 2', machine_type='AMAN', branch=cls.branch2)
        cls.machine2.refresh_from_db()

    def setUp(self):
        super().setUp()
        self.own = self.make_txn('CASH_IN', 10)
        self.own.post_transaction()
        self.other = self.make_txn('MERCHANT_RECHARGE', 10, machine=self.machine2, treasury=self.treasury2,
                                   agent=self.agent, paid_on_credit=True)
        self.other.post_transaction()

    def test_history_only_own_branch(self):
        c = login(self.client, self.cashier)
        r = c.get('/accounting/eservice/history/')
        self.assertContains(r, self.own.transaction_number)
        self.assertNotContains(r, self.other.transaction_number)
        self.assertEqual(c.get(f'/accounting/eservice/{self.other.pk}/').status_code, 404)
        add_perms(self.cashier, 'accounting.view_posmachine')
        c = login(self.client, User.objects.get(pk=self.cashier.pk))
        self.assertEqual(c.get(f'/accounting/pos-machines/{self.machine2.pk}/').status_code, 404)
        self.assertEqual(c.get(f'/accounting/pos-machines/{self.machine.pk}/').status_code, 200)
        r = c.get('/accounting/pos-machines/')
        self.assertEqual([m.pk for m in r.context['machines']], [self.machine.pk])

    def test_admin_follows_header_branch(self):
        c = login(self.client, self.w.admin)
        r = c.get('/accounting/eservice/history/', {'branch': self.branch2.pk})
        self.assertNotContains(r, self.own.transaction_number)
        self.assertContains(r, self.other.transaction_number)

    def test_admin_all_branches_sees_everything(self):
        c = login(self.client, self.w.admin)
        r = c.get('/accounting/eservice/history/', {'branch': 'all'})
        self.assertContains(r, self.own.transaction_number)
        self.assertContains(r, self.other.transaction_number)

    def test_history_filters_and_totals(self):
        c = login(self.client, self.w.admin)
        c.get('/accounting/eservice/history/', {'branch': 'all'})
        r = c.get('/accounting/eservice/history/', {'type': 'CASH_IN'})
        self.assertContains(r, self.own.transaction_number)
        self.assertNotContains(r, self.other.transaction_number)
        self.assertEqual(r.context['totals']['net_cash'], D('10'))
        r = c.get('/accounting/eservice/history/', {'q': self.other.transaction_number})
        self.assertEqual(r.context['total_count'], 1)

    def test_center_machines_only_working_branch(self):
        c = login(self.client, self.cashier)
        r = c.get('/accounting/eservice/')
        self.assertEqual(list(r.context['machines']), [self.machine])

    def test_view_permission_required(self):
        nobody = make_user('esvc_nobody', role='CASHIER', branch=self.w.branch)
        c = login(self.client, nobody)
        r = c.get('/accounting/eservice/history/')
        self.assertEqual(r.status_code, 302)


class ShiftLinkTests(EServiceBase):
    def setUp(self):
        super().setUp()
        from apps.tenant.invoicing.models import POSSession
        self.session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury,
                                                 user=self.cashier, opening_balance=D('0'))
        self.other_treasury = make_treasury(self.w.branch, 'خزينة أخرى')
        self.other_treasury.ensure_account()

    def test_pos_session_set(self):
        self.post_center(principal_amount='50')
        txn = EServiceTransaction.objects.get()
        self.assertEqual(txn.pos_session, self.session)

    def test_other_treasury_refused_while_shift_open(self):
        r = self.post_center(principal_amount='50', treasury_id=self.other_treasury.pk)
        self.assertContains(r, 'وردية مفتوحة')
        self.assertFalse(EServiceTransaction.objects.exists())

    def test_center_defaults_to_shift_treasury(self):
        c = login(self.client, self.cashier)
        r = c.get('/accounting/eservice/')
        self.assertEqual(r.context['default_treasury_id'], self.w.treasury.pk)
        self.assertEqual(list(r.context['treasuries']), [self.w.treasury])

    def test_cancel_refused_after_shift_closed(self):
        self.post_center(principal_amount='50')
        txn = EServiceTransaction.objects.get()
        self.session.status = 'CLOSED'
        self.session.save()
        with self.assertRaises(ValidationError):
            txn.cancel_transaction(user=self.w.admin, reason='x')


class CleanupCommandTests(EServiceBase):
    def setUp(self):
        super().setUp()
        from apps.tenant.invoicing.models import Invoice
        self.txn = self.make_txn('CASH_OUT', 100, commission=5)
        self.txn.post_transaction()
        self.inv = Invoice.objects.create(
            invoice_number='SVC-2026-00001', invoice_type=Invoice.SALE, partner=self.w.customer,
            branch=self.w.branch, warehouse=self.w.warehouse, payment_type=Invoice.CASH,
            treasury=self.w.treasury, subtotal=D('5'), total_amount=D('5'), status=Invoice.POSTED,
            date=datetime.date.today())
        EServiceTransaction.objects.filter(pk=self.txn.pk).update(invoice=self.inv)
        from apps.tenant.einvoicing.models import EInvoiceLog
        EInvoiceLog.objects.get_or_create(invoice=self.inv)

    def run_cmd(self, *args):
        out = StringIO()
        call_command('cleanup_eservice_invoices', *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        from apps.tenant.invoicing.models import Invoice
        out = self.run_cmd()
        self.assertIn('SVC-2026-00001', out)
        self.assertTrue(Invoice.objects.filter(pk=self.inv.pk).exists())
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.invoice_id, self.inv.pk)

    def test_apply_deletes_invoice_and_log(self):
        from apps.tenant.einvoicing.models import EInvoiceLog
        from apps.tenant.invoicing.models import Invoice
        self.run_cmd('--apply')
        self.assertFalse(Invoice.objects.filter(pk=self.inv.pk).exists())
        self.assertFalse(EInvoiceLog.objects.filter(invoice_id=self.inv.pk).exists())
        self.txn.refresh_from_db()
        self.assertIsNone(self.txn.invoice_id)
        self.assertEqual(self.txn.status, 'POSTED')
        self.assertEqual(account('4240').current_balance, D('5'))

    def test_apply_skips_invoice_with_journal_entry(self):
        from apps.tenant.invoicing.models import Invoice
        Invoice.objects.filter(pk=self.inv.pk).update(journal_entry=self.txn.journal_entry)
        out = self.run_cmd('--apply')
        self.assertIn('skip', out)
        self.assertTrue(Invoice.objects.filter(pk=self.inv.pk).exists())


class PagesSmokeTests(EServiceBase):
    def test_pages_render(self):
        txn = self.make_txn('CASH_IN', 10, commission=1)
        txn.post_transaction()
        c = login(self.client, self.w.admin)
        for url in ('/accounting/eservice/', '/accounting/eservice/?type=MERCHANT_RECHARGE&machine=%d' % self.machine.pk,
                    '/accounting/eservice/history/', f'/accounting/eservice/{txn.pk}/',
                    '/accounting/pos-machines/', '/accounting/pos-machines/create/',
                    f'/accounting/pos-machines/{self.machine.pk}/', f'/accounting/pos-machines/{self.machine.pk}/edit/',
                    '/accounting/eservice/rules/', '/accounting/eservice/rules/create/'):
            self.assertEqual(c.get(url).status_code, 200, url)

    def test_machine_edit_sets_alert(self):
        c = login(self.client, self.w.admin)
        c.post(f'/accounting/pos-machines/{self.machine.pk}/edit/', {
            'name': 'فوري معدلة', 'machine_type': 'FAWRY', 'branch_id': self.w.branch.pk,
            'min_balance_alert': '600', 'is_active': 'on', 'notes': ''})
        self.machine.refresh_from_db()
        self.assertEqual(self.machine.name, 'فوري معدلة')
        self.assertEqual(self.machine.min_balance_alert, D('600'))
        self.assertTrue(self.machine.is_low_balance)   # balance 500 < 600
