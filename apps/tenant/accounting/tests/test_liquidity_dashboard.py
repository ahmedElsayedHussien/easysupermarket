import datetime
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.accounting.models import InternalTransfer, JournalEntry, JournalItem
from apps.tenant.core.tests.factories import (
    account, build_world, login, make_bank_account, make_branch, make_treasury,
)


def post(account_debit, account_credit, amount, ref):
    e = JournalEntry.objects.create(date=datetime.date.today(), reference=ref, description=ref,
                                    status=JournalEntry.POSTED)
    JournalItem.objects.create(entry=e, account=account_debit, debit=amount, credit=0)
    JournalItem.objects.create(entry=e, account=account_credit, debit=0, credit=amount)
    return e


class LiquidityDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.treasury2 = make_treasury(cls.branch2, 'خزينة 2')
        cls.bank = make_bank_account('بنك مصر')

    def setUp(self):
        for obj in (self.w.treasury, self.treasury2, self.bank):
            obj.ensure_account()
            obj.refresh_from_db()
        post(self.w.treasury.account, account('4100'), D('500'), 'SALE-1')   # cash in
        post(self.bank.account, account('3100'), D('2000'), 'CAP-1')        # bank in
        post(self.treasury2.account, account('3100'), D('300'), 'CAP-2')    # other branch

    def test_admin_all_branches_sees_everything(self):
        c = login(self.client, self.w.admin)
        c.get('/?branch=all')
        r = c.get('/accounting/liquidity/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['totals']['TREASURY'], D('800'))
        self.assertEqual(r.context['totals']['BANK'], D('2000'))
        self.assertEqual(r.context['grand_total'], D('2800'))

    def test_admin_header_branch_scopes_treasuries(self):
        c = login(self.client, self.w.admin)
        c.get(f'/?branch={self.w.branch.id}')
        r = c.get('/accounting/liquidity/')
        self.assertEqual([row['obj'] for row in r.context['treasury_rows']], [self.w.treasury])
        self.assertEqual(r.context['totals']['BANK'], D('2000'))  # banks are company-wide

    def test_cashier_without_permission_redirected(self):
        r = login(self.client, self.w.cashier).get('/accounting/liquidity/')
        self.assertEqual(r.status_code, 302)

    def test_flows_exclude_internal_transfers(self):
        t = InternalTransfer.objects.create(
            amount=D('100'), from_account_type='TREASURY', from_treasury=self.w.treasury,
            to_account_type='BANK', to_bank_account=self.bank, created_by=self.w.admin)
        t.confirm_transfer(user=self.w.admin)
        c = login(self.client, self.w.admin)
        c.get('/?branch=all')
        r = c.get('/accounting/liquidity/')
        # 500 sale + 2000 + 300 capital in; the 100 transfer is neither in nor out
        self.assertEqual(r.context['total_in'], 2800.0)
        self.assertEqual(r.context['total_out'], 0.0)
        self.assertEqual(r.context['grand_total'], D('2800'))

    def test_composition_percentages(self):
        c = login(self.client, self.w.admin)
        c.get('/?branch=all')
        comp = {x['key']: x for x in c.get('/accounting/liquidity/').context['composition']}
        self.assertAlmostEqual(comp['TREASURY']['pct'] + comp['BANK']['pct'] + comp['EWALLET']['pct'], 100.0, places=3)

    def test_transfer_form_prefilled_from_dashboard(self):
        c = login(self.client, self.w.admin)
        r = c.get(f'/accounting/transfers/create/?from_type=TREASURY&from_id={self.w.treasury.id}'
                  f'&to_type=BANK&to_id={self.bank.id}')
        form = r.context['form']
        self.assertEqual(form.initial['from_account_type'], 'TREASURY')
        self.assertEqual(form.initial['from_treasury'], self.w.treasury.id)
        self.assertEqual(form.initial['to_bank_account'], self.bank.id)

    def test_sidebar_has_single_liquidity_link(self):
        c = login(self.client, self.w.admin)
        html = c.get('/accounting/liquidity/').content.decode()
        self.assertIn('إدارة السيولة', html)
        for old_link in ('/accounting/treasuries/', '/accounting/bank-accounts/', '/accounting/ewallets/'):
            self.assertNotIn(f'href="{old_link}" class="nav-link', html)
