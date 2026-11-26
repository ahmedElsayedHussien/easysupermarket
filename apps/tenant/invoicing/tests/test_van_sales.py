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
        from apps.tenant.core.tests.factories import make_branch
        branch2 = make_branch('فرع طنطا')
        main2 = make_warehouse(branch2, 'مخزن طنطا', code='WH-TNT')
        url = reverse('invoicing:rep_list')
        self.assertEqual(set(self.client.get(url).context['source_warehouses']) >= {self.w.warehouse, main2}, True)
        self.assertNotIn(self.rep.van_warehouse, self.client.get(url).context['source_warehouses'])   # no vans
        self.client.post(url, {'employee': self.w.cashier.employee_profile.id, 'branch': branch2.id,
                               'source_warehouse': self.w.warehouse.id, 'commission_rate': '1'})   # other branch
        self.assertFalse(SalesRep.objects.filter(employee=self.w.cashier.employee_profile).exists())
        self.client.post(url, {'employee': self.w.cashier.employee_profile.id, 'branch': branch2.id,
                               'source_warehouse': main2.id, 'commission_rate': '1', 'monthly_target': '0'})
        rep = SalesRep.objects.select_related('employee', 'van_warehouse', 'treasury').get(
            employee=self.w.cashier.employee_profile)
        self.assertTrue(rep.treasury.account_id)
        self.assertEqual((rep.employee.branch, rep.van_warehouse.branch, rep.treasury.branch, rep.source_warehouse),
                         (branch2, branch2, branch2, main2))

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

    def test_rep_role_alone_lands_on_his_screen(self):
        from django.urls import reverse
        user = make_user('role_only_rep', role='SALES_REP', branch=self.w.branch)      # no permissions ticked
        SalesRep.objects.create(employee=user.employee_profile)
        r = self.client.post(reverse('login'), {'username': 'role_only_rep', 'password': 'pass-123'}, follow=True)
        self.assertEqual(r.redirect_chain[-1][0], reverse('invoicing:rep_app_home'))
        self.assertEqual(r.status_code, 200)

    def test_rep_is_switched_to_his_van_branch(self):
        from django.urls import reverse
        from apps.tenant.core.tests.factories import make_branch
        branch2 = make_branch('فرع الإسكندرية')
        user = make_user('branchless_rep', role='SALES_REP', branch=None)
        rep = SalesRep.objects.create(employee=user.employee_profile,
                                      van_warehouse=make_warehouse(branch2, 'عربية إسكندرية', code='VAN-ALX'))
        login(self.client, user)
        r = self.client.get(reverse('invoicing:rep_app_home'))
        self.assertContains(r, 'فرع الإسكندرية')
        rep.employee.refresh_from_db()
        self.assertEqual(rep.employee.branch, branch2)

    def test_storekeeper_reviews_prints_and_issues(self):
        from django.urls import reverse
        from apps.tenant.core.permission_catalog import ROLE_DEFAULTS
        keeper = make_user('keeper1', role='INVENTORY', branch=self.w.branch, perms=ROLE_DEFAULTS['INVENTORY'])
        order = van.create_loading_request(self.rep, [{'product_id': self.w.product.id, 'qty': 150}], self.rep_user)
        line = order.lines.get()
        login(self.client, keeper)
        r = self.client.get(reverse('invoicing:loading_detail', args=[order.id]))
        self.assertContains(r, 'مش كفاية')                                          # 150 asked, 100 in stock
        self.assertRedirects(self.client.get(reverse('invoicing:loading_print', args=[order.id])),
                             reverse('invoicing:loading_detail', args=[order.id]))   # not approved yet
        url = reverse('invoicing:loading_detail', args=[order.id])
        self.client.post(url, {'action': 'approve', f'qty_{line.id}': '150'})       # refused: not available
        order.refresh_from_db()
        self.assertEqual(order.status, LoadingOrder.PENDING)
        self.client.post(url, {'action': 'approve', f'qty_{line.id}': '60'})
        r = self.client.get(reverse('invoicing:loading_print', args=[order.id]))
        self.assertContains(r, 'إذن صرف')
        self.assertContains(r, 'المندوب (استلمت البضاعة كاملة)')
        self.client.post(url, {'action': 'issue'})
        order.refresh_from_db()
        self.assertEqual(order.status, LoadingOrder.ISSUED)
        inv = van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 60}])
        self.assertEqual(inv.warehouse_id, self.rep.van_warehouse_id)

    def test_rep_cash_box_hidden_from_pos_and_purchases(self):
        from django.urls import reverse
        from apps.tenant.accounting.models import Treasury
        from apps.tenant.core.tests.factories import make_invoice
        box = self.rep.treasury
        self.assertNotIn(box, Treasury.objects.business())
        self.assertIn(self.w.treasury, Treasury.objects.business())
        login(self.client, self.w.admin)
        r = self.client.get(reverse('invoicing:purchase_invoice'))
        self.assertNotIn(box, list(r.context['treasuries']))
        with self.assertRaisesMessage(ValueError, 'خزنة مندوب'):                       # purchase paid from it
            make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.w.product, 1, D('5'))], treasury=box)
        with self.assertRaisesMessage(ValueError, 'خزنة مندوب'):                       # someone else's sale
            make_invoice(self.w, lines=[(self.w.product, 1, D('10'))], treasury=box)
        stock_in(self.w.product, self.rep.van_warehouse, 5, D('5'))
        inv = van.rep_sale(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': 1}])   # his own: ok
        self.assertEqual(inv.treasury_id, box.id)

    def test_loading_screen_lists_all_stock_and_sends_many_items(self):
        from django.urls import reverse
        from apps.tenant.inventory.models import ProductUoM, UnitOfMeasure
        carton = ProductUoM.objects.create(product=self.w.product2, uom=UnitOfMeasure.objects.create(name='كرتونة'),
                                           conversion_factor=D('12'))
        stock_in(self.w.product2, self.w.warehouse, 48, D('40'))
        stock_in(self.w.product, self.rep.van_warehouse, 7, D('5'))
        login(self.client, self.rep_user)
        r = self.client.get(reverse('invoicing:rep_app_loading'))
        self.assertContains(r, self.w.warehouse.name)                       # sender
        self.assertContains(r, self.rep.van_warehouse.name)                 # receiver
        rows = {p['id']: p for p in r.context['catalog_json']}
        self.assertEqual((rows[self.w.product.id]['available'], rows[self.w.product.id]['in_van']), ('100', '7'))
        self.assertEqual(rows[self.w.product2.id]['uoms'][1]['factor'], '12')
        self.client.post(reverse('invoicing:rep_app_loading'), {
            'product_id': [self.w.product.id, self.w.product2.id],
            'uom_id': ['', carton.id], 'qty': ['10', '2'], 'notes': 'كذا صنف'})
        order = LoadingOrder.objects.get(rep=self.rep)
        lines = {l.product_id: l for l in order.lines.all()}
        self.assertEqual(lines[self.w.product.id].requested_qty, D('10'))
        self.assertEqual((lines[self.w.product2.id].requested_qty, lines[self.w.product2.id].uom_id), (D('2'), carton.id))
        van.approve_loading(order, {}, self.w.admin)
        van.issue_loading(order, self.w.admin)
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product2.id], D('24'))  # 2 cartons × 12

    def test_back_to_dashboard_button(self):
        from django.urls import reverse
        login(self.client, self.rep_user)
        home = reverse('invoicing:rep_app_home')
        self.assertContains(self.client.get(reverse('invoicing:rep_app_loading')), f'href="{home}" title="رجوع للوحتي"')
        self.assertNotContains(self.client.get(home), 'رجوع للوحتي')

    def test_phone_friendly_markup(self):
        from django.urls import reverse
        login(self.client, self.rep_user)
        for name in ('rep_app_sell', 'rep_app_loading', 'rep_app_stock'):
            r = self.client.get(reverse(f'invoicing:{name}'))
            self.assertContains(r, 'data-scan=', msg_prefix=name)            # camera barcode button
            self.assertContains(r, 'font-size: 16px !important')             # no iPhone zoom on inputs
            self.assertContains(r, 'id="offline"')                           # no-connection banner
            self.assertContains(r, 'width=device-width')

    def test_round_quantities_show_as_plain_numbers(self):
        from django.urls import reverse
        stock_in(self.w.product, self.rep.van_warehouse, 10, D('5'))
        stock_in(self.w.product2, self.rep.van_warehouse, D('2.5'), D('5'))
        login(self.client, self.rep_user)
        r = self.client.get(reverse('invoicing:rep_app_stock'))
        qty = {p['id']: p['qty'] for p in r.context['products']}
        self.assertEqual((qty[self.w.product.id], qty[self.w.product2.id]), ('10', '2.5'))   # not "1E+1"
        self.assertNotContains(r, 'E+')
        self.assertNotContains(self.client.get(reverse('invoicing:rep_app_sell')), 'E+')


class RepSellScreenTests(TestCase):
    """شاشة البيع والمرتجع للمندوب: الإجمالي على الشاشة = المتسجل، الخصومات، المرتجع وحدوده."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.rep_user = make_user('sell_rep', role='SALES_REP', branch=cls.w.branch)
        cls.rep = SalesRep.objects.create(employee=cls.rep_user.employee_profile, source_warehouse=cls.w.warehouse)
        van.ensure_rep_setup(cls.rep, create_van=True, create_treasury=True)
        cls.rep.refresh_from_db()

    def setUp(self):
        stock_in(self.w.product, self.rep.van_warehouse, 50, D('5'))
        login(self.client, self.rep_user)
        self.url = __import__('django.urls', fromlist=['reverse']).reverse('invoicing:rep_app_sell')

    def post(self, **data):
        import json
        return self.client.post(self.url, json.dumps(data), content_type='application/json')

    def test_quote_equals_saved_invoice_with_line_and_invoice_discounts(self):
        cart = [{'product_id': self.w.product.id, 'uom_id': '', 'qty': '10', 'disc': '10'}]
        q = self.post(cart=cart, discount_pct='5', quote=True).json()
        self.assertEqual(q['lines'][0]['disc_pct'], '14.5')                     # 1-(0.9×0.95)
        self.assertEqual((q['gross'], q['discount'], q['total']), ('100.00', '14.50', '97.47'))
        r = self.post(cart=cart, discount_pct='5', payment='CASH').json()
        inv = Invoice.objects.get(pk=r['invoice_id'])
        self.assertEqual(inv.total_amount.quantize(D('0.01')), D('97.47'))    # what he saw is what was saved
        self.assertEqual(inv.discount_amount.quantize(D('0.01')), D('14.50'))
        self.assertEqual(van.cash_balance(self.rep), D('97.47'))

    def test_discount_cap_applies_to_the_combined_discount(self):
        self.w.settings.max_pos_discount_percentage = D('10')
        self.w.settings.save()
        cart = [{'product_id': self.w.product.id, 'qty': '1', 'disc': '6'}]
        r = self.post(cart=cart, discount_pct='6', quote=True)                  # 11.64% combined > 10%
        self.assertEqual(r.status_code, 400)

    def test_return_limits_refund_and_stock(self):
        cust = self.w.customer
        self.post(cart=[{'product_id': self.w.product.id, 'qty': '8'}], partner_id=cust.id, payment='CREDIT')
        items = self.client.get(self.url, {'returnable': cust.id}).json()['items']
        self.assertEqual((items[0]['id'], items[0]['qty']), (self.w.product.id, '8'))
        r = self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '9'}], partner_id=cust.id)
        self.assertIn('أكبر من اللي العميل اشتراه', r.json()['error'])             # bought 8 only
        r = self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '3'}], partner_id=cust.id,
                      refund='CASH')
        self.assertIn('مش كفاية', r.json()['error'])                              # empty cash box
        r = self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '3'}], partner_id=cust.id,
                      refund='CREDIT').json()
        ret = Invoice.objects.get(pk=r['invoice_id'])
        self.assertEqual((ret.invoice_type, ret.warehouse_id, ret.sales_rep_id),
                         (Invoice.RETURN_SALE, self.rep.van_warehouse_id, self.rep.id))
        cust.refresh_from_db()
        self.assertEqual(cust.customer_balance, D('57.00'))                      # 91.20 − 34.20
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product.id], D('45'))   # 50 − 8 + 3
        self.assertEqual(van.returnable_for(cust)[self.w.product.id], D('5'))
        self.assertContains(self.client.get(r['url']), 'اتخصم من حساب العميل')
        r = self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '1'}])
        self.assertIn('عميل مسجّل', r.json()['error'])

    def test_cash_refund_from_rep_box_and_customer_card(self):
        cust = self.w.customer
        self.post(cart=[{'product_id': self.w.product.id, 'qty': '5'}], partner_id=cust.id, payment='CASH')   # +57
        r = self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '2'}], partner_id=cust.id,
                      refund='CASH').json()
        self.assertTrue(r['success'], r)
        self.assertEqual(van.cash_balance(self.rep), D('34.20'))                 # 57 − 22.80
        info = self.client.get(self.url, {'customer_info': cust.id}).json()['customer']
        self.assertEqual(info['name'], cust.name)
        page = self.client.get(self.url, {'customer': cust.id, 'mode': 'return'})
        self.assertEqual(page.context['start_mode'], 'return')
        self.assertContains(page, 'مرتجع من عميل')

    def test_fixed_amount_discounts_and_taxes(self):
        cart = [{'product_id': self.w.product.id, 'qty': '10', 'disc': '7', 'disc_type': 'amt'},      # 100 − 7
                {'product_id': self.w.product2.id, 'qty': '1', 'disc': '', 'disc_type': 'pct'}]      # 50
        stock_in(self.w.product2, self.rep.van_warehouse, 5, D('30'))
        q = self.post(cart=cart, discount_pct='50', discount_type='amt', quote=True).json()
        self.assertEqual((q['gross'], q['discount']), ('150.00', '57.00'))                 # 7 + 50 exactly
        self.assertEqual(q['taxes'], [{'rate': '14', 'amount': q['tax']}])
        r = self.post(cart=cart, discount_pct='50', discount_type='amt', payment='CASH').json()
        inv = Invoice.objects.get(pk=r['invoice_id'])
        self.assertEqual(inv.discount_amount.quantize(D('0.01')), D('57.00'))
        self.assertEqual(str(inv.total_amount.quantize(D('0.01'))), q['total'])
        page = self.client.get(r['url'])
        self.assertContains(page, '?size=a4')                                               # tax invoice print
        with self.assertRaises(ValidationError):                                            # more than the line
            van.price_cart(self.rep, self.rep_user, [{'product_id': self.w.product.id, 'qty': '1', 'disc': '20',
                                                      'disc_type': 'amt'}])

    def test_withholding_for_subject_customer_and_credit_note_reference(self):
        cust = self.w.customer
        cust.subject_to_withholding_tax, cust.tax_id = True, '123456789'
        cust.save()
        self.w.product.withholding_tax_rate = D('1')
        self.w.product.save()
        q = self.post(cart=[{'product_id': self.w.product.id, 'qty': '10'}], partner_id=cust.id, quote=True).json()
        self.assertEqual((q['tax'], q['wht'], q['total']), ('14.00', '1.00', '113.00'))
        sale = self.post(cart=[{'product_id': self.w.product.id, 'qty': '10'}], partner_id=cust.id,
                         payment='CREDIT').json()
        info = self.client.get(self.url, {'customer_info': cust.id}).json()['customer']
        self.assertEqual((info['tax_id'], info['wht']), ('123456789', True))
        ret = self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '2'}], partner_id=cust.id).json()
        self.assertEqual(Invoice.objects.get(pk=ret['invoice_id']).parent_invoice_id, sale['invoice_id'])

    def test_taxes_follow_the_customer_card_only(self):
        from apps.tenant.core.tests.factories import make_partner
        exempt = make_partner('جمعية معفاة', 'CUSTOMER')
        exempt.subject_to_vat = False
        exempt.save()
        b2b = make_partner('شركة خاضعة', 'CUSTOMER')
        b2b.vat_rate, b2b.subject_to_withholding_tax, b2b.wht_rate = D('10'), True, D('3')
        b2b.save()
        cart = [{'product_id': self.w.product.id, 'qty': '10', 'tax_rate': '0', 'wht_rate': '0'}]   # phone tries to drop tax
        q = self.post(cart=cart, partner_id=exempt.id, quote=True).json()
        self.assertEqual((q['tax'], q['total']), ('0.00', '100.00'))
        q = self.post(cart=cart, partner_id=b2b.id, quote=True).json()
        self.assertEqual((q['tax'], q['wht'], q['total']), ('10.00', '3.00', '107.00'))
        q = self.post(cart=cart, quote=True).json()                          # walk-in: default 14%
        self.assertEqual(q['tax'], '14.00')
        self.w.product.tax_rate = D('0')                                       # an exempt product stays exempt
        self.w.product.save()
        self.assertEqual(self.post(cart=cart, partner_id=b2b.id, quote=True).json()['tax'], '0.00')

    def test_customer_form_saves_taxes(self):
        from django.urls import reverse
        from apps.tenant.partners.models import Partner
        login(self.client, self.w.admin)
        self.client.post(reverse('partners:partner_edit', args=[self.w.customer.id]), {
            'name': self.w.customer.name, 'partner_type': 'CUSTOMER', 'opening_balance': '0', 'phone': '', 'mobile': '',
            'email': '', 'national_id': '', 'tax_id': '',
            'opening_balance_type': 'DEBIT', 'vat_rate': '5', 'subject_to_withholding_tax': 'on', 'wht_rate': '1.5'})
        c = Partner.objects.get(pk=self.w.customer.id)
        self.assertEqual((c.subject_to_vat, c.vat_rate, c.subject_to_withholding_tax, c.wht_rate),
                         (False, D('5'), True, D('1.5')))

    def test_rep_price_edit_setting(self):
        cart = [{'product_id': self.w.product.id, 'qty': '1', 'price': '8'}]
        q = self.post(cart=cart, quote=True).json()
        self.assertEqual(q['lines'][0]['price'], '10.00')                     # setting off: the phone's price is ignored
        self.assertFalse(self.client.get(self.url).context['can_edit_price'])
        self.w.settings.rep_allow_price_edit = True
        self.w.settings.rep_price_margin_percent = D('25')
        self.w.settings.save()
        q = self.post(cart=cart, quote=True).json()
        self.assertEqual((q['lines'][0]['price'], q['lines'][0]['list_price']), ('8.00', '10.00'))
        r = self.post(cart=[{'product_id': self.w.product.id, 'qty': '1', 'price': '5'}], quote=True)
        self.assertIn('خارج الهامش', r.json()['error'])                        # 50% off > 25%
        r = self.post(cart=cart, payment='CASH').json()
        self.assertEqual(Invoice.objects.get(pk=r['invoice_id']).lines.get().unit_price, D('8'))
        self.w.product.min_sale_price = D('9')
        self.w.product.save()
        self.assertIn('أدنى سعر', self.post(cart=cart, quote=True).json()['error'])
        self.assertTrue(self.client.get(self.url).context['can_edit_price'])

    def test_return_price_can_only_go_down(self):
        self.w.settings.rep_allow_price_edit = True
        self.w.settings.save()
        cust = self.w.customer
        self.post(cart=[{'product_id': self.w.product.id, 'qty': '5'}], partner_id=cust.id, payment='CREDIT')
        r = self.post(mode='return', partner_id=cust.id, cart=[{'product_id': self.w.product.id, 'qty': '1', 'price': '12'}],
                      quote=True)
        self.assertIn('ميزيدش', r.json()['error'])
        r = self.post(mode='return', partner_id=cust.id, cart=[{'product_id': self.w.product.id, 'qty': '1', 'price': '9'}]).json()
        self.assertEqual(Invoice.objects.get(pk=r['invoice_id']).lines.get().unit_price, D('9'))

    def test_all_customers_listed_mine_first(self):
        from django.urls import reverse
        from apps.tenant.core.tests.factories import make_partner
        for n in range(3):
            make_partner(f'عميل تجريبي {n}', 'CUSTOMER')
        url = reverse('invoicing:rep_app_customers')
        d = self.client.get(url, {'json': 1}).json()                       # nothing typed, never sold yet
        names = [c['name'] for c in d['items']]
        self.assertIn('عميل تجريبي 0', names)
        self.assertIn(self.w.customer.name, names)
        self.assertNotIn('عميل نقدي', names)
        self.post(cart=[{'product_id': self.w.product.id, 'qty': '1'}], partner_id=self.w.customer.id, payment='CREDIT')
        d = self.client.get(url, {'json': 1}).json()
        self.assertEqual((d['items'][0]['id'], d['items'][0]['mine']), (self.w.customer.id, True))
        self.assertEqual(len(self.client.get(url).context['rows']), d['total'])
        self.assertEqual(len(self.client.get(url, {'json': 1, 'q': 'ت'}).json()['items']), 3)   # 1 letter is enough

    def test_day_screen_figures_and_badges(self):
        from django.urls import reverse
        cust = self.w.customer
        self.post(cart=[{'product_id': self.w.product.id, 'qty': '10'}], payment='CASH')                          # +114
        self.post(cart=[{'product_id': self.w.product.id, 'qty': '5'}], partner_id=cust.id, payment='CREDIT')      # 57 credit
        self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '1'}], partner_id=cust.id,
                  refund='CASH')                                                                                 # −11.40
        van.rep_collect(self.rep, self.rep_user, cust, '20')                                                     # +20
        main = make_treasury(self.w.branch, 'خزنة التوريد')
        van.post_handover(self.rep, '100', self.w.admin, treasury=main)                                          # −100
        r = self.client.get(reverse('invoicing:rep_app_day'))
        a = r.context['a']
        self.assertEqual((a['sales'], a['sales_cash'], a['sales_credit']), (D('171.00'), D('114.00'), D('57.00')))
        self.assertEqual((a['ret'], a['ret_cash'], a['net']), (D('11.40'), D('11.40'), D('159.60')))
        self.assertEqual((r.context['collected'], r.context['handed']), (D('20.00'), D('100.00')))
        self.assertEqual((r.context['opening'], r.context['closing']), (D('0'), D('22.60')))   # 114+20−11.40−100
        self.assertEqual(r.context['closing'], r.context['cash_now'])
        pays = {(i.kind, i.pay) for i in r.context['invoices']}
        self.assertEqual(pays, {('بيع', 'نقدي'), ('بيع', 'آجل'), ('مرتجع', 'رد نقدي')})
        sold = {it['product__name']: (it['sold'], it['back']) for it in r.context['items']}
        self.assertEqual(sold[self.w.product.name], ('15', '1'))
        yesterday = self.client.get(reverse('invoicing:rep_app_day'), {'date': '2020-01-01'})
        self.assertEqual((yesterday.context['a']['sales'], yesterday.context['invoices']), (D('0'), []))
        home = self.client.get(reverse('invoicing:rep_app_home'))
        self.assertContains(home, 'رد نقدي')
        self.assertContains(home, reverse('invoicing:rep_app_day'))

    def test_rep_returns_only_from_his_own_invoices(self):
        from apps.tenant.core.tests.factories import make_invoice
        cust = self.w.customer
        stock_in(self.w.product, self.w.warehouse, 20, D('5'))
        make_invoice(self.w, lines=[(self.w.product, 10, D('10'))], partner=cust)              # a POS sale
        self.assertEqual(self.client.get(self.url, {'returnable': cust.id}).json()['items'], [])
        r = self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '1'}], partner_id=cust.id)
        self.assertIn('اشتراه منك', r.json()['error'])
        self.post(cart=[{'product_id': self.w.product.id, 'qty': '3'}], partner_id=cust.id, payment='CREDIT')
        items = self.client.get(self.url, {'returnable': cust.id}).json()['items']
        self.assertEqual(items[0]['qty'], '3')                                                  # his 3, not POS's 10

    def test_return_without_invoice_option(self):
        cust = self.w.customer
        cat = self.client.get(self.url, {'catalog': 1}).json()['items']
        self.assertIn(self.w.product.id, [p['id'] for p in cat])
        cart = [{'product_id': self.w.product.id, 'qty': '2'}]
        r = self.post(mode='return', no_invoice=True, cart=cart, partner_id=cust.id).json()    # never sold to him
        ret = Invoice.objects.get(pk=r['invoice_id'])
        self.assertEqual((ret.invoice_type, ret.parent_invoice_id), (Invoice.RETURN_SALE, None))
        self.assertIn('بدون فاتورة', ret.notes)
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product.id], D('52'))
        r = self.post(mode='return', no_invoice=True, cart=cart)                                 # walk-in: no
        self.assertIn('عميل مسجّل', r.json()['error'])
        self.w.settings.rep_return_without_invoice_max = D('10')
        self.w.settings.save()
        r = self.post(mode='return', no_invoice=True, cart=cart, partner_id=cust.id)              # 22.80 > 10
        self.assertIn('حده الأقصى', r.json()['error'])
        self.w.settings.rep_allow_return_without_invoice = False
        self.w.settings.save()
        r = self.post(mode='return', no_invoice=True, cart=cart, partner_id=cust.id)
        self.assertIn('مقفول', r.json()['error'])
        self.assertEqual(self.client.get(self.url, {'catalog': 1}).json()['items'], [])
        self.assertFalse(self.client.get(self.url).context['no_invoice_ok'])

    def test_unloading_detail_shows_asked_vs_received(self):
        from django.urls import reverse
        order = van.create_unloading_request(self.rep, [{'product_id': self.w.product.id, 'sound': 6, 'damaged': 2}],
                                             self.rep_user)
        url = reverse('invoicing:rep_app_unloading_order', args=[order.id])
        r = self.client.get(url)
        self.assertContains(r, 'سلّم البضاعة لأمين المخزن')
        self.assertContains(self.client.get(reverse('invoicing:rep_app_unloading')), url)       # list links to it
        line = order.lines.get()
        van.receive_unloading(order, {line.id: ('5', '2')}, self.w.admin)
        line.refresh_from_db()
        self.assertEqual((line.requested_sound, line.sound_qty), (D('6'), D('5')))
        r = self.client.get(url)
        self.assertContains(r, 'طلبت 6')
        self.assertContains(r, 'أمين المخزن عدّل الكمية')
        other = make_user('other_rep', role='SALES_REP', branch=self.w.branch)
        SalesRep.objects.create(employee=other.employee_profile)
        login(self.client, other)
        self.assertEqual(self.client.get(url).status_code, 404)                                 # not his order

    # ---- returns get their own batch; cancellation goes back to the original batches ----
    def test_return_batch_and_cancel_restores_original_batch(self):
        from apps.tenant.inventory.models import InventoryBatch, StockMovement
        from apps.tenant.services.invoice_service import cancel_invoice
        cust = self.w.customer
        batch = InventoryBatch.objects.get(warehouse=self.rep.van_warehouse, product=self.w.product)
        sale = Invoice.objects.get(pk=self.post(cart=[{'product_id': self.w.product.id, 'qty': '4'}], partner_id=cust.id,
                                                payment='CREDIT').json()['invoice_id'])
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D('46'))
        ret = Invoice.objects.get(pk=self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '1'}],
                                               partner_id=cust.id).json()['invoice_id'])
        rb = InventoryBatch.objects.get(source_invoice_line__invoice=ret)
        self.assertEqual(rb.batch_number, f'مرتجع {ret.invoice_number}')                 # its own return batch
        cancel_invoice(ret.id)                                                           # cancel the return
        rb.refresh_from_db()
        self.assertEqual(rb.quantity_remaining, D('0'))                                  # left its own batch
        self.assertTrue(StockMovement.objects.filter(batch=rb, movement_type='CANCEL_OUT').exists())
        n_batches = InventoryBatch.objects.filter(warehouse=self.rep.van_warehouse).count()
        cancel_invoice(sale.id)                                                          # cancel the sale
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D('50'))                              # same batch is whole again
        self.assertEqual(InventoryBatch.objects.filter(warehouse=self.rep.van_warehouse).count(), n_batches)
        self.assertTrue(StockMovement.objects.filter(batch=batch, movement_type='CANCEL_IN').exists())

    def test_rep_cancel_rules_and_effects(self):
        from django.urls import reverse
        cust = self.w.customer
        r = self.post(cart=[{'product_id': self.w.product.id, 'qty': '5'}], partner_id=cust.id, payment='CASH').json()
        inv = Invoice.objects.get(pk=r['invoice_id'])
        url = reverse('invoicing:rep_app_invoice', args=[inv.id])
        self.assertNotContains(self.client.get(url), 'إلغاء الفاتورة بالكامل')            # setting off: no button
        self.client.post(url, {'action': 'cancel', 'reason': 'غلط'})
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invoice.POSTED)
        self.w.settings.rep_allow_cancel_invoice = True
        self.w.settings.save()
        self.client.post(url, {'action': 'cancel', 'reason': ''})                       # a reason is required
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invoice.POSTED)
        main = make_treasury(self.w.branch, 'خزنة رئيسية')
        van.post_handover(self.rep, '57', self.w.admin, treasury=main)                 # money already handed over
        self.assertContains(self.client.post(url, {'action': 'cancel', 'reason': 'العميل رجع في كلامه'}, follow=True),
                            'ورّدت الفلوس')
        main2 = make_treasury(self.w.branch, 'خزنة 2')
        sale2 = Invoice.objects.get(pk=self.post(cart=[{'product_id': self.w.product.id, 'qty': '2'}], partner_id=cust.id,
                                                 payment='CREDIT').json()['invoice_id'])
        from apps.tenant.partners.models import Partner
        balance_before = Partner.objects.get(pk=cust.pk).customer_balance            # fresh (the balance is cached)
        r = self.client.post(reverse('invoicing:rep_app_invoice', args=[sale2.id]),
                             {'action': 'cancel', 'reason': 'العميل رفض الاستلام'}, follow=True)
        sale2.refresh_from_db()
        self.assertEqual(sale2.status, Invoice.CANCELLED)
        self.assertIn('العميل رفض الاستلام', sale2.notes)
        self.assertContains(r, 'الفاتورة دي اتلغت')
        self.assertEqual(Partner.objects.get(pk=cust.pk).customer_balance, balance_before - D('22.80'))
        self.assertEqual(van.van_stock(self.rep.van_warehouse)[self.w.product.id], D('45'))   # 50 − 5 (+2 −2)
        inv.invoice_date_backup = inv.date
        Invoice.objects.filter(pk=inv.pk).update(date=inv.date.replace(year=inv.date.year - 1))
        inv.refresh_from_db()
        self.assertIn('النهارده بس', van.cancel_block_reason(self.rep, inv))
        day = self.client.get(reverse('invoicing:rep_app_day'))
        self.assertIn(('فاتورة ملغاة', 'اتلغت'), {(i.kind, i.pay) for i in day.context['invoices']})
        self.assertEqual(day.context['a']['sales'], D('0'))          # the cancelled one isn't counted (the other moved to last year)
        del main2

    def test_cancel_blocked_when_return_exists(self):
        self.w.settings.rep_allow_cancel_invoice = True
        self.w.settings.save()
        cust = self.w.customer
        sale = Invoice.objects.get(pk=self.post(cart=[{'product_id': self.w.product.id, 'qty': '3'}], partner_id=cust.id,
                                                payment='CREDIT').json()['invoice_id'])
        self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '1'}], partner_id=cust.id)
        self.assertIn('عليها مرتجع', van.cancel_block_reason(self.rep, sale))

    # ---- expiry gaps: a return keeps the expiry; a van is never loaded with expired goods ----
    def test_return_keeps_the_expiry_of_the_goods_sold(self):
        import datetime
        from apps.tenant.inventory.models import InventoryBatch
        soon = datetime.date.today() + datetime.timedelta(days=5)
        InventoryBatch.objects.filter(warehouse=self.rep.van_warehouse, product=self.w.product).update(expiry_date=soon)
        cust = self.w.customer
        self.post(cart=[{'product_id': self.w.product.id, 'qty': '3'}], partner_id=cust.id, payment='CREDIT')
        ret = self.post(mode='return', cart=[{'product_id': self.w.product.id, 'qty': '1'}], partner_id=cust.id).json()
        rb = InventoryBatch.objects.get(source_invoice_line__invoice_id=ret['invoice_id'])
        self.assertEqual(rb.expiry_date, soon)                      # not a "fresh" batch without a date

    def test_loading_leaves_expired_goods_in_the_warehouse(self):
        import datetime
        from apps.tenant.inventory.models import InventoryBatch
        src = self.rep.source_warehouse
        stock_in(self.w.product2, src, 10, D('20'))
        InventoryBatch.objects.filter(warehouse=src, product=self.w.product2).update(
            expiry_date=datetime.date.today() - datetime.timedelta(days=1))           # all expired
        stock_in(self.w.product2, src, 4, D('20'))                                     # 4 good
        order = van.create_loading_request(self.rep, [{'product_id': self.w.product2.id, 'qty': 6}], self.rep_user)
        with self.assertRaisesMessage(ValidationError, 'الصالح'):
            van.approve_loading(order, {}, self.w.admin)                               # only 4 sellable
        line = order.lines.get()
        van.approve_loading(order, {line.id: '4'}, self.w.admin)
        van.issue_loading(order, self.w.admin)
        moved = InventoryBatch.objects.filter(warehouse=self.rep.van_warehouse, product=self.w.product2)
        self.assertEqual(sum(b.quantity_remaining for b in moved), D('4'))
        self.assertFalse(any(b.expiry_date for b in moved))                            # no expired batch moved
