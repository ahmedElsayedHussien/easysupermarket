"""
One branch rule for header + POS + documents (core.access.get_working_branch):

- Admin: the header branch (session) scopes data and is the working branch;
  "all branches" falls back to the admin's own branch for documents.
- User: always their own branch; ?branch= is ignored.
- POS: sells from the open shift's branch; the admin header follows it.
"""
from decimal import Decimal as D

from django.test import TestCase, RequestFactory

from apps.tenant.core.access import get_working_branch, set_header_branch
from apps.tenant.core.tests.factories import (
    build_world, login, make_branch, make_treasury, make_user, make_warehouse,
)


class WorkingBranchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.wh2 = make_warehouse(cls.branch2, 'مخزن 2', code='WH-2')
        cls.treasury2 = make_treasury(cls.branch2, 'خزينة 2')

    def _req(self, user, header_branch=None):
        rq = RequestFactory().get('/')
        rq.user = user
        rq.session = {}
        rq.branch = header_branch
        return rq

    def test_admin_uses_header_branch(self):
        self.assertEqual(get_working_branch(self._req(self.w.admin, self.branch2)), self.branch2)

    def test_admin_on_all_branches_falls_back_to_own_branch(self):
        self.assertEqual(get_working_branch(self._req(self.w.admin, None)), self.w.branch)

    def test_user_always_own_branch(self):
        self.assertEqual(get_working_branch(self._req(self.w.cashier, self.branch2)), self.w.branch)

    def test_user_without_branch_has_none(self):
        u = make_user(role='CASHIER', branch=None)
        self.assertIsNone(get_working_branch(self._req(u, None)))

    def test_set_header_branch_only_for_admin(self):
        rq = self._req(self.w.cashier, self.w.branch)
        set_header_branch(rq, self.branch2)
        self.assertNotIn('current_branch_id', rq.session)
        rq = self._req(self.w.admin, self.w.branch)
        set_header_branch(rq, self.branch2)
        self.assertEqual(rq.session['current_branch_id'], self.branch2.id)

    # --- header (middleware) ------------------------------------------------

    def test_user_cannot_switch_header_branch(self):
        c = login(self.client, self.w.cashier)
        r = c.get(f'/?branch={self.branch2.id}')
        self.assertEqual(r.wsgi_request.branch, self.w.branch)

    def test_admin_switches_header_branch(self):
        c = login(self.client, self.w.admin)
        r = c.get(f'/?branch={self.branch2.id}')
        self.assertEqual(r.wsgi_request.branch, self.branch2)
        r = c.get('/?branch=all')
        self.assertIsNone(r.wsgi_request.branch)

    # --- shift + POS --------------------------------------------------------

    def _open_shift(self, client, treasury):
        return client.post('/invoicing/pos/open-shift/', {'opening_balance': '0', 'treasury_id': treasury.id})

    def test_admin_shift_opens_on_header_branch(self):
        from apps.tenant.invoicing.models import POSSession
        c = login(self.client, self.w.admin)
        c.get(f'/?branch={self.branch2.id}')
        self._open_shift(c, self.treasury2)
        s = POSSession.objects.get(user=self.w.admin, status=POSSession.OPEN)
        self.assertEqual(s.branch, self.branch2)

    def test_shift_rejects_treasury_of_other_branch(self):
        from apps.tenant.invoicing.models import POSSession
        c = login(self.client, self.w.cashier)
        self._open_shift(c, self.treasury2)  # cashier is on the main branch
        self.assertFalse(POSSession.objects.filter(user=self.w.cashier).exists())

    def test_pos_follows_open_shift_and_syncs_admin_header(self):
        c = login(self.client, self.w.admin)
        c.get(f'/?branch={self.branch2.id}')
        self._open_shift(c, self.treasury2)
        c.get(f'/?branch={self.w.branch.id}')          # admin moves the header away
        r = c.get('/invoicing/pos/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['current_branch'], self.branch2)
        self.assertEqual(c.session['current_branch_id'], self.branch2.id)  # header followed the shift
        r = c.get('/')
        self.assertEqual(r.wsgi_request.branch, self.branch2)

    def test_complete_sale_rejects_branch_other_than_shift(self):
        import json
        c = login(self.client, self.w.admin)
        c.get(f'/?branch={self.branch2.id}')
        self._open_shift(c, self.treasury2)
        payload = {
            'cart': [{'product_id': self.w.product.id, 'quantity': 1, 'unit_price': 10,
                      'discount_percent': 0, 'tax_rate': 0, 'uom_id': 'base'}],
            'payment_type': 'CASH', 'cash_received': 100,
            'branch_id': self.w.branch.id, 'warehouse_id': self.w.warehouse.id,
        }
        r = c.post('/invoicing/api/sale/complete/', json.dumps(payload), content_type='application/json')
        self.assertFalse(r.json().get('success'))


class ShiftBranchLockTests(TestCase):
    """An open POS shift locks the branch everywhere until it is closed."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.treasury2 = make_treasury(cls.branch2, 'خزينة 2')

    def setUp(self):
        from apps.tenant.invoicing.models import POSSession
        self.c = login(self.client, self.w.admin)
        self.c.get(f'/?branch={self.branch2.id}')
        self.c.post('/invoicing/pos/open-shift/', {'opening_balance': '0', 'treasury_id': self.treasury2.id})
        self.shift = POSSession.objects.get(user=self.w.admin, status=POSSession.OPEN)
        self.assertEqual(self.shift.branch, self.branch2)

    def test_header_switch_blocked_while_shift_open(self):
        r = self.c.get(f'/?branch={self.w.branch.id}')
        self.assertEqual(r.wsgi_request.branch, self.branch2)
        self.assertTrue(r.wsgi_request.branch_locked_by_shift)
        r = self.c.get('/?branch=all')
        self.assertEqual(r.wsgi_request.branch, self.branch2)

    def test_header_shows_lock_instead_of_switcher(self):
        r = self.c.get('/')
        html = r.content.decode()
        self.assertNotIn('id="branchSelector"', html)
        self.assertIn('🔒', html)

    def test_switch_allowed_after_shift_closed(self):
        from apps.tenant.invoicing.models import POSSession
        POSSession.objects.filter(pk=self.shift.pk).update(status=POSSession.CLOSED)
        r = self.c.get(f'/?branch={self.w.branch.id}')
        self.assertEqual(r.wsgi_request.branch, self.w.branch)
        self.assertFalse(r.wsgi_request.branch_locked_by_shift)

    def test_lock_helper_rejects_other_branch(self):
        from apps.tenant.core.access import shift_branch_lock_error
        r = self.c.get('/')
        self.assertIsNotNone(shift_branch_lock_error(r.wsgi_request, self.w.branch))
        self.assertIsNone(shift_branch_lock_error(r.wsgi_request, self.branch2))


class TreasuryFollowsCurrentBranchTests(TestCase):
    """Vouchers/expenses/transfers may only use treasuries of the current branch."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch2 = make_branch('الفرع 2', code='B2')
        cls.treasury2 = make_treasury(cls.branch2, 'خزينة 2')

    def _form_request(self, user, header_branch_id):
        c = login(self.client, user)
        return c.get(f'/?branch={header_branch_id}').wsgi_request

    def test_voucher_form_lists_only_current_branch_treasuries(self):
        from apps.tenant.accounting.forms import VoucherForm
        rq = self._form_request(self.w.admin, self.branch2.id)
        ids = set(VoucherForm(user=rq.user).fields['treasury'].queryset.values_list('id', flat=True))
        self.assertEqual(ids, {self.treasury2.id})
        rq = self._form_request(self.w.admin, self.w.branch.id)
        ids = set(VoucherForm(user=rq.user).fields['treasury'].queryset.values_list('id', flat=True))
        self.assertEqual(ids, {self.w.treasury.id})

    def test_cashier_never_sees_other_branch_treasury(self):
        from apps.tenant.accounting.forms import scoped_treasuries
        rq = self._form_request(self.w.cashier, self.branch2.id)  # ?branch ignored for users
        self.assertEqual(list(scoped_treasuries(rq.user)), [self.w.treasury])

    def test_confirm_guard_rejects_other_branch_treasury(self):
        from django.core.exceptions import ValidationError
        from apps.tenant.accounting.views import _check_treasury_branch
        rq = self._form_request(self.w.admin, self.w.branch.id)
        with self.assertRaises(ValidationError):
            _check_treasury_branch(rq.user, treasuries=[self.treasury2])
        _check_treasury_branch(rq.user, treasuries=[self.w.treasury])  # same branch: ok

    def test_transfer_source_current_branch_destination_any_for_admin(self):
        from apps.tenant.accounting.forms import InternalTransferForm
        rq = self._form_request(self.w.admin, self.w.branch.id)
        f = InternalTransferForm(user=rq.user)
        self.assertEqual(set(f.fields['from_treasury'].queryset.values_list('id', flat=True)), {self.w.treasury.id})
        self.assertEqual(set(f.fields['to_treasury'].queryset.values_list('id', flat=True)),
                         {self.w.treasury.id, self.treasury2.id})

    def test_open_shift_branch_drives_treasuries(self):
        from apps.tenant.accounting.forms import scoped_treasuries
        c = login(self.client, self.w.admin)
        c.get(f'/?branch={self.branch2.id}')
        c.post('/invoicing/pos/open-shift/', {'opening_balance': '0', 'treasury_id': self.treasury2.id})
        rq = c.get(f'/?branch={self.w.branch.id}').wsgi_request   # attempt blocked by the shift lock
        self.assertEqual(list(scoped_treasuries(rq.user)), [self.treasury2])


class OpenShiftDefaultTreasuryTests(TestCase):
    """Opening a shift uses the branch's POS default treasury — no choice needed."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.extra = make_treasury(cls.w.branch, 'خزينة 2')    # 2 treasuries in the branch -> needs a default

    def _open(self, data=None):
        from apps.tenant.invoicing.models import POSSession
        c = login(self.client, self.w.cashier)
        c.post('/invoicing/pos/open-shift/', data or {'opening_balance': '100'})
        return POSSession.objects.filter(user=self.w.cashier, status=POSSession.OPEN).first()

    def test_branch_default_used_when_no_treasury_sent(self):
        self.w.branch.default_pos_treasury = self.extra
        self.w.branch.save(update_fields=['default_pos_treasury'])
        page = login(self.client, self.w.cashier).get('/invoicing/pos/open-shift/')
        self.assertEqual(page.context['default_treasury'], self.extra)
        self.assertEqual(self._open().treasury, self.extra)

    def test_without_default_a_choice_is_required(self):
        self.assertIsNone(self._open())

    def test_locked_default_cannot_be_changed(self):
        from apps.tenant.core.tests.factories import system_settings
        system_settings(lock_pos_payment_accounts=True)
        self.w.branch.default_pos_treasury = self.extra
        self.w.branch.save(update_fields=['default_pos_treasury'])
        page = login(self.client, self.w.cashier).get('/invoicing/pos/open-shift/')
        self.assertTrue(page.context['treasury_locked'])
        self.assertContains(page, '🔒')
        shift = self._open({'opening_balance': '0', 'treasury_id': self.w.treasury.id})
        self.assertEqual(shift.treasury, self.extra)


class MaintenanceTicketPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_ticket_create_and_detail_render(self):
        from apps.tenant.maintenance.models import MaintenanceTicket
        ticket = MaintenanceTicket.objects.create(
            customer=self.w.customer, branch=self.w.branch, device_model='لابتوب',
            issue_description='لا يعمل',
        )
        c = login(self.client, self.w.admin)
        r = c.get(f'/maintenance/{ticket.pk}/')
        self.assertEqual(r.status_code, 200)  # was a NameError (undefined `branch`)
