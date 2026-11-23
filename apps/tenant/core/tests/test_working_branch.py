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
