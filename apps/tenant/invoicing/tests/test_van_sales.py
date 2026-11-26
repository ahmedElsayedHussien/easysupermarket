"""بيع المناديب: طلب تحميل ← اعتماد ← صرف، البيع من العربية، التحصيل، التفريغ، وتوريد النقدية."""
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.tenant.core.tests.factories import (build_world, ledger_totals, login, make_treasury, make_user,
                                             make_warehouse, stock_in)
from apps.tenant.invoicing import van_services as van
from apps.tenant.invoicing.models import Invoice, LoadingOrder, SalesRep, UnloadingOrder


class VanSalesServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.rep_user = make_user('van_rep', role='SALES_REP', branch=cls.w.branch, perms=('invoicing.use_rep_app',))
        cls.rep = SalesRep.objects.create(employee=cls.rep_user.employee_profile, source_warehouse=cls.w.warehouse)
        van.ensure_rep_setup(cls.rep, create_van=True, create_treasury=True)
        cls.rep.refresh_from_db()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 100, D('5'))

    def load(self, qty=30, approve=None):
        order = van.create_loading_request(self.rep, [{'product_id': self.w.product.id, 'qty': qty}], self.rep_user)
        line = order.lines.get()
        van.approve_loading(order, {line.id: approve if approve is not None else qty}, self.w.admin)
        return van.issue_loading(order, self.w.admin)

    def test_setup_creates_van_and_cash_box(self):
        self.assertTrue(self.rep.van_warehouse_id and self.rep.treasury_id)
        self.assertIsNotNone(self.rep.treasury.account_id)
        self.assertEqual(van.default_source_warehouse(self.rep), self.w.warehouse)

    def test_loading_cycle_moves_approved_quantity(self):
        order = self.load(30, approve=20)
        self.assertEqual(order.status, LoadingOrder.ISSUED)
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product.id], D('20'))
        self.assertEqual(van.van_stock(self.w.warehouse)[self.w.product.id], D('80'))
        self.assertEqual(order.transfer.total_cost, D('100'))

    def test_loading_rules(self):
        order = van.create_loading_request(self.rep, [{'product_id': self.w.product.id, 'qty': 500}], self.rep_user)
        with self.assertRaises(ValidationError):                  # not approved yet
            van.issue_loading(order, self.w.admin)
        with self.assertRaises(ValidationError):                  # more than the warehouse has
            van.approve_loading(order, {}, self.w.admin)
        van.cancel_loading(order)
        with self.assertRaises(ValidationError):
            van.approve_loading(order, {}, self.w.admin)

    def test_cash_sale_from_van_lands_in_rep_cash_box(self):
        self.load(20)
        inv = van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 5}])
        self.assertEqual((inv.status, inv.warehouse_id, inv.sales_rep_id), (Invoice.POSTED, self.rep.van_warehouse_id,
                                                                            self.rep.id))
        self.assertEqual(inv.total_amount, D('57.00'))                        # 5 × 10 + 14% VAT
        self.assertEqual(van.cash_balance(self.rep), D('57.00'))
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product.id], D('15'))
        with self.assertRaises(ValidationError):                            # more than the van has
            van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 16}])
        debit, credit = ledger_totals()
        self.assertEqual(debit, credit)

    def test_credit_sale_collection_and_credit_limit(self):
        self.load(20)
        with self.assertRaises(ValidationError):                            # credit needs a real customer
            van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 1}], payment='CREDIT')
        inv = van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 10}],
                           partner=self.w.customer, payment='PARTIAL', paid_amount='40')
        self.assertEqual((inv.paid_cash, inv.paid_credit), (D('40.00'), D('74.00')))
        self.w.customer.refresh_from_db()
        self.assertEqual(self.w.customer.customer_balance, D('74.00'))
        van.rep_collect(self.rep, self.rep_user, self.w.customer, '50')
        self.assertEqual(van.cash_balance(self.rep), D('90.00'))
        self.w.customer.credit_limit = D('30')
        self.w.customer.save()
        with self.assertRaises(ValidationError):
            van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 2}],
                         partner=self.w.customer, payment='CREDIT')

    def test_unloading_sound_and_damaged(self):
        self.load(20)
        damaged_wh = make_warehouse(self.w.branch, 'توالف', code='DMG')
        order = van.create_unloading_request(self.rep, [{'product_id': self.w.product.id, 'sound': 6, 'damaged': 2}],
                                             self.rep_user)
        line = order.lines.get()
        order = van.receive_unloading(order, {line.id: ('5', '2')}, self.w.admin, damaged_warehouse=damaged_wh)
        self.assertEqual(order.status, UnloadingOrder.RECEIVED)
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product.id], D('13'))
        self.assertEqual(van.van_stock(self.w.warehouse)[self.w.product.id], D('85'))
        self.assertEqual(van.van_stock(damaged_wh)[self.w.product.id], D('2'))
        with self.assertRaises(ValidationError):                            # more than the van holds
            van.create_unloading_request(self.rep, [{'product_id': self.w.product.id, 'sound': 50}], self.rep_user)

    def test_handover_with_shortage_charged_to_rep(self):
        from apps.tenant.hr.models import EmployeeLoan
        self.load(20)
        van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 10}])   # 114 in his box
        main = make_treasury(self.w.branch, 'الخزنة الرئيسية')
        with self.assertRaises(ValidationError):
            van.post_handover(self.rep, '200', self.w.admin, treasury=main)
        h = van.post_handover(self.rep, '100', self.w.admin, treasury=main, charge_shortage=True)
        self.assertEqual((h.expected, h.amount, h.shortage_charged), (D('114.00'), D('100.00'), D('14.00')))
        self.assertEqual(van.cash_balance(self.rep), D('0'))
        self.assertEqual(main.ensure_account().current_balance, D('100.00'))
        loan = EmployeeLoan.objects.get(employee=self.rep.employee)
        self.assertEqual((loan.source, loan.amount), (EmployeeLoan.SHORTAGE, D('14.00')))


class VanSalesScreenTests(TestCase):
    """واجهة المندوب على الموبايل + شاشات الإدارة (اعتماد وصرف واستلام وتوريد)."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.rep_user = make_user('van_rep2', role='SALES_REP', branch=cls.w.branch,
                                 perms=('invoicing.use_rep_app', 'partners.add_partner'))
        cls.rep = SalesRep.objects.create(employee=cls.rep_user.employee_profile, source_warehouse=cls.w.warehouse)
        van.ensure_rep_setup(cls.rep, create_van=True, create_treasury=True)
        cls.rep.refresh_from_db()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 100, D('5'))

    def post_json(self, url, data):
        import json
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def test_rep_lands_on_phone_screen_and_full_cycle(self):
        from django.urls import reverse
        login(self.client, self.rep_user)
        r = self.client.get(reverse('core:main_screen'))
        self.assertRedirects(r, reverse('invoicing:rep_app_home'))
        self.assertContains(self.client.get(reverse('invoicing:rep_app_home')), 'الفلوس اللي معاك')

        # 1) rep requests loading from his phone
        r = self.client.post(reverse('invoicing:rep_app_loading'), {
            'product_id': [self.w.product.id], 'uom_id': [''], 'qty': ['30'], 'notes': 'الصبح'})
        order = LoadingOrder.objects.get(rep=self.rep)
        self.assertEqual(order.status, LoadingOrder.PENDING)
        self.assertEqual(self.client.get(reverse('invoicing:loading_list')).status_code, 302)   # not for the rep

        # 2) back office approves 25 and issues
        login(self.client, self.w.admin)
        self.assertContains(self.client.get(reverse('core:main_screen')), 'طلب تحميل مندوب مستني الاعتماد')
        line = order.lines.get()
        self.client.post(reverse('invoicing:loading_detail', args=[order.id]), {'action': 'approve', f'qty_{line.id}': '25'})
        self.client.post(reverse('invoicing:loading_detail', args=[order.id]), {'action': 'issue'})
        order.refresh_from_db()
        self.assertEqual(order.status, LoadingOrder.ISSUED)
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product.id], D('25'))

        # 3) rep sells: quote, then save
        login(self.client, self.rep_user)
        sell = reverse('invoicing:rep_app_sell')
        self.assertEqual(self.client.get(sell).context['products_json'][0]['name'], self.w.product.name)
        cart = [{'product_id': self.w.product.id, 'uom_id': '', 'qty': '4'}]
        q = self.post_json(sell, {'cart': cart, 'quote': True}).json()
        self.assertEqual(q['total'], '45.60')
        r = self.post_json(sell, {'cart': cart, 'payment': 'CASH'}).json()
        self.assertTrue(r['success'], r)
        self.assertContains(self.client.get(r['url']), 'wa.me')
        r = self.post_json(sell, {'cart': [{'product_id': self.w.product.id, 'qty': '999'}]})
        self.assertEqual(r.status_code, 400)

        # 4) customer search + collection
        found = self.client.get(reverse('invoicing:rep_app_customers'), {'json': 1, 'q': 'السلام'}).json()['items']
        self.assertEqual(found[0]['id'], self.w.customer.id)
        self.post_json(sell, {'cart': cart, 'payment': 'CREDIT', 'partner_id': self.w.customer.id})
        self.client.post(reverse('invoicing:rep_app_customer', args=[self.w.customer.id]), {'amount': '20'})
        self.assertEqual(van.cash_balance(self.rep), D('65.60'))

        # 5) rep returns 5 units; storekeeper receives
        self.client.post(reverse('invoicing:rep_app_unloading'), {'product_id': [self.w.product.id], 'sound': ['5'],
                                                                   'damaged': ['']})
        un = UnloadingOrder.objects.get(rep=self.rep)
        login(self.client, self.w.admin)
        self.client.post(reverse('invoicing:unloading_detail', args=[un.id]), {'action': 'receive'})
        un.refresh_from_db()
        self.assertEqual(un.status, UnloadingOrder.RECEIVED)
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product.id], D('12'))

        # 6) cash hand-over to the branch treasury
        r = self.client.get(reverse('invoicing:handover_list'))
        self.assertEqual(r.context['balances'][0]['balance'], D('65.60'))
        self.client.post(reverse('invoicing:handover_list'), {'rep': self.rep.id, 'amount': '65.60',
                                                              'destination': f't:{self.w.treasury.id}'})
        self.assertEqual(van.cash_balance(self.rep), D('0'))
        self.assertContains(self.client.get(reverse('invoicing:rep_detail', args=[self.rep.id])), 'RH-')

    def test_user_without_rep_and_rep_creation_from_screen(self):
        from django.urls import reverse
        from apps.tenant.core.tests.factories import add_perms
        login(self.client, self.w.cashier)
        self.assertEqual(self.client.get(reverse('invoicing:rep_app_home')).status_code, 302)       # no permission
        add_perms(self.w.cashier, 'invoicing.use_rep_app')
        self.assertContains(self.client.get(reverse('invoicing:rep_app_home')), 'حسابك مش مربوط بمندوب')
        login(self.client, self.w.admin)
        self.client.post(reverse('invoicing:rep_list'), {'employee': self.w.cashier.employee_profile.id,
                                                         'van_warehouse': 'new', 'treasury': 'new',
                                                         'commission_rate': '1', 'monthly_target': '0'})
        rep = SalesRep.objects.get(employee=self.w.cashier.employee_profile)
        self.assertTrue(rep.van_warehouse_id and rep.treasury_id and rep.treasury.account_id)

    def test_every_screen_opens(self):
        from django.urls import reverse
        lo = van.create_loading_request(self.rep, [{'product_id': self.w.product.id, 'qty': 3}], self.rep_user)
        login(self.client, self.rep_user)
        for name in ('rep_app_home', 'rep_app_sell', 'rep_app_customers', 'rep_app_stock', 'rep_app_loading',
                     'rep_app_unloading'):
            self.assertEqual(self.client.get(reverse(f'invoicing:{name}')).status_code, 200, name)
        self.assertEqual(self.client.get(reverse('invoicing:rep_app_loading_order', args=[lo.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse('invoicing:rep_app_customer', args=[self.w.customer.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse('invoicing:rep_app_loading'))
                         .context['catalog_json'][0]['id'], self.w.product.id)
        van.approve_loading(lo, {}, self.w.admin)
        van.issue_loading(lo, self.w.admin)
        un = van.create_unloading_request(self.rep, [{'product_id': self.w.product.id, 'sound': 1}], self.rep_user)
        login(self.client, self.w.admin)
        for url in (reverse('invoicing:loading_list'), reverse('invoicing:loading_list') + '?status=ALL',
                    reverse('invoicing:loading_detail', args=[lo.id]), reverse('invoicing:unloading_list'),
                    reverse('invoicing:unloading_detail', args=[un.id]), reverse('invoicing:handover_list'),
                    reverse('invoicing:rep_list'), reverse('invoicing:rep_detail', args=[self.rep.id])):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(reverse('core:main_screen')), 'طلب تفريغ مندوب مستني الاستلام')
