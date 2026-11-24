"""POS-related SystemSetting / Branch settings wired into the POS and purchase screens."""
import datetime
import json
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import (
    account_net, build_world, entry_totals, login, make_bank_account, make_partner, make_product,
    make_treasury, stock_in, system_settings,
)
from apps.tenant.invoicing.models import Invoice, POSSession
from apps.tenant.invoicing.views import _pos_default_ids


class _POSBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 50, '6')
        for user in (self.w.cashier, self.w.admin):
            POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=user)
        login(self.client, self.w.cashier)

    def _sale(self, cart, **extra):
        payload = {'cart': cart, 'payment_type': 'CASH', 'warehouse_id': self.w.warehouse.id,
                   'branch_id': self.w.branch.id}
        payload.update(extra)
        return self.client.post(reverse('invoicing:complete_sale'), data=json.dumps(payload),
                                content_type='application/json')

    def _item(self, product=None, **kw):
        item = {'product_id': (product or self.w.product).id, 'quantity': 2, 'unit_price': 10,
                'discount_percent': 0, 'tax_rate': 0}
        item.update(kw)
        return item


class BranchPaymentDefaultsTests(_POSBase):
    def test_pos_preselects_branch_default_accounts(self):
        global_bank = make_bank_account('بنك عام')
        branch_bank = make_bank_account('بنك الفرع')
        branch_treasury = make_treasury(self.w.branch, 'خزينة الفرع الافتراضية')
        system_settings(default_pos_bank_account=global_bank)
        self.w.branch.default_pos_treasury = branch_treasury
        self.w.branch.default_pos_bank_account = branch_bank
        self.w.branch.save()

        resp = self.client.get(reverse('invoicing:pos'))
        self.assertEqual(resp.status_code, 200)
        # The open shift's drawer wins over the branch default: its cash is what gets counted.
        self.assertEqual(resp.context['pos_default_treasury_id'], self.w.treasury.id)
        POSSession.objects.all().delete()
        self.assertEqual(
            _pos_default_ids(self.w.branch)['pos_default_treasury_id'],
            branch_treasury.id)
        self.assertEqual(resp.context['pos_default_bank_account_id'], branch_bank.id)

    def test_card_sale_uses_branch_default_bank(self):
        global_bank = make_bank_account('بنك عام')
        branch_bank = make_bank_account('بنك الفرع')
        system_settings(default_pos_bank_account=global_bank)
        self.w.branch.default_pos_bank_account = branch_bank
        self.w.branch.save()

        data = self._sale([self._item()], payment_type='CARD').json()
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(pk=data['invoice_id'])
        self.assertEqual(inv.bank_account_id, branch_bank.id)

    def test_card_sale_falls_back_to_global_default(self):
        global_bank = make_bank_account('بنك عام')
        system_settings(default_pos_bank_account=global_bank)
        data = self._sale([self._item()], payment_type='CARD').json()
        self.assertTrue(data['success'], data)
        self.assertEqual(Invoice.objects.get(pk=data['invoice_id']).bank_account_id, global_bank.id)


class PriceMarginTests(_POSBase):
    def setUp(self):
        super().setUp()
        system_settings(allow_pos_price_modification=True, pos_price_margin_percent=D('10'))

    def test_cashier_price_outside_margin_rejected(self):
        resp = self._sale([self._item(unit_price='8')])
        self.assertEqual(resp.status_code, 403)
        self.assertIn('هامش', resp.json()['error'])
        self.assertFalse(Invoice.objects.exists())
        resp = self._sale([self._item(unit_price='11.5')])
        self.assertEqual(resp.status_code, 403)

    def test_cashier_price_within_margin_accepted(self):
        data = self._sale([self._item(unit_price='9.5')]).json()
        self.assertTrue(data['success'], data)
        self.assertEqual(Invoice.objects.get(pk=data['invoice_id']).lines.get().unit_price, D('9.5'))

    def test_admin_exempt_from_margin(self):
        self.client.logout()
        login(self.client, self.w.admin)
        data = self._sale([self._item(unit_price='5')]).json()
        self.assertTrue(data['success'], data)

    def test_open_price_product_exempt(self):
        p = make_product(self.w.category, 'صنف بسعر مفتوح', sale_price=D('10'), tax_rate=D('0'), is_open_price=True)
        stock_in(p, self.w.warehouse, 10, '3')
        data = self._sale([self._item(product=p, unit_price='4')]).json()
        self.assertTrue(data['success'], data)


class LargeAmountCustomerTests(_POSBase):
    def setUp(self):
        super().setUp()
        system_settings(large_amount_requires_customer=D('15'))

    def test_large_sale_without_customer_rejected(self):
        resp = self._sale([self._item()])  # total 20
        self.assertFalse(resp.json()['success'])
        self.assertIn('عميل', resp.json()['error'])
        self.assertFalse(Invoice.objects.exists())

    def test_large_sale_to_generic_cash_customer_rejected(self):
        cash_customer = make_partner('عميل نقدي', 'CUSTOMER')
        resp = self._sale([self._item()], partner_id=cash_customer.id)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Invoice.objects.exists())

    def test_large_sale_with_customer_allowed(self):
        data = self._sale([self._item()], partner_id=self.w.customer.id).json()
        self.assertTrue(data['success'], data)

    def test_small_sale_without_customer_allowed(self):
        data = self._sale([self._item(quantity=1)]).json()  # total 10
        self.assertTrue(data['success'], data)


class CashRoundingTests(_POSBase):
    def setUp(self):
        super().setUp()
        system_settings(pos_cash_rounding=D('0.50'))

    def _product(self, price):
        p = make_product(self.w.category, f'صنف {price}', sale_price=D(price), tax_rate=D('0'))
        stock_in(p, self.w.warehouse, 10, '1')
        return p

    def test_cash_rounded_up_posts_gain_to_5115(self):
        p = self._product('10.30')
        data = self._sale([self._item(product=p, quantity=1)], cash_received='20').json()
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(pk=data['invoice_id'])
        self.assertEqual(inv.total_amount, D('10.30'))
        self.assertEqual(inv.paid_cash, D('10.50'))
        self.assertEqual(inv.rounding_amount, D('0.20'))
        entry = inv.journal_entry
        debit, credit = entry_totals(entry)
        self.assertEqual(debit, credit)
        self.assertEqual(account_net(entry, '5115'), D('-0.20'))  # credit = gain
        cash_debit = entry.items.filter(account=self.w.treasury.account).first()
        self.assertEqual(cash_debit.debit, D('10.50'))

    def test_cash_rounded_down_posts_loss_to_5115(self):
        p = self._product('10.20')
        data = self._sale([self._item(product=p, quantity=1)], cash_received='10').json()
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(pk=data['invoice_id'])
        self.assertEqual(inv.total_amount, D('10.20'))
        self.assertEqual(inv.paid_cash, D('10.00'))
        self.assertEqual(inv.rounding_amount, D('-0.20'))
        debit, credit = entry_totals(inv.journal_entry)
        self.assertEqual(debit, credit)
        self.assertEqual(account_net(inv.journal_entry, '5115'), D('0.20'))  # debit = loss

    def test_split_rounds_cash_part_only(self):
        bank = make_bank_account('بنك الفيزا')
        p = self._product('10.30')
        data = self._sale([self._item(product=p, quantity=1)], payment_type='SPLIT', bank_account_id=bank.id,
                          payments={'cash': '5.50', 'card': '5', 'visa_bank_id': bank.id}).json()
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(pk=data['invoice_id'])
        self.assertEqual(inv.paid_card, D('5'))
        self.assertEqual(inv.paid_cash, D('5.50'))  # exact due 5.30 -> 5.50
        self.assertEqual(inv.rounding_amount, D('0.20'))
        debit, credit = entry_totals(inv.journal_entry)
        self.assertEqual(debit, credit)
        self.assertEqual(account_net(inv.journal_entry, '5115'), D('-0.20'))

    def test_card_sale_not_rounded(self):
        bank = make_bank_account('بنك الفيزا')
        p = self._product('10.30')
        data = self._sale([self._item(product=p, quantity=1)], payment_type='CARD', bank_account_id=bank.id).json()
        self.assertTrue(data['success'], data)
        inv = Invoice.objects.get(pk=data['invoice_id'])
        self.assertEqual(inv.rounding_amount, D('0'))
        self.assertEqual(account_net(inv.journal_entry, '5115'), D('0'))

    def test_cash_received_below_rounded_due_rejected(self):
        p = self._product('10.30')
        data = self._sale([self._item(product=p, quantity=1)], cash_received='10.30').json()
        self.assertFalse(data['success'])

    def test_receipt_shows_rounding(self):
        p = self._product('10.30')
        data = self._sale([self._item(product=p, quantity=1)]).json()
        resp = self.client.get(reverse('invoicing:receipt', args=[data['invoice_id']]))
        self.assertContains(resp, 'المطلوب نقداً بعد التقريب')
        self.assertEqual(resp.context['cash_after_rounding'], D('10.50'))
        self.assertEqual(resp.context['rounding_amount'], D('0.20'))


class ReceiptSettingsTests(_POSBase):
    def test_paper_width_and_branch_info(self):
        self.w.branch.phone = '01000000000'
        self.w.branch.save()
        system_settings(receipt_paper_width='58', show_branch_info_on_receipt=True)
        data = self._sale([self._item()]).json()
        resp = self.client.get(reverse('invoicing:receipt', args=[data['invoice_id']]))
        self.assertEqual(resp.context['paper_size'], '58mm')
        self.assertContains(resp, '01000000000')

        system_settings(show_branch_info_on_receipt=False)
        resp = self.client.get(reverse('invoicing:receipt', args=[data['invoice_id']]))
        self.assertNotContains(resp, '01000000000')


class PurchaseExpiryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def _post(self, expiry):
        login(self.client, self.w.admin)
        return self.client.post(reverse('invoicing:purchase_invoice'), {
            'invoice_type': 'PURCHASE', 'supplier_id': self.w.supplier.id, 'warehouse_id': self.w.warehouse.id,
            'branch_id': self.w.branch.id, 'payment_type': 'CASH', 'treasury_id': self.w.treasury.id,
            'product_id[]': [self.w.product.id], 'quantity[]': ['5'], 'price[]': ['6'], 'uom_id[]': [''],
            'expiry_date[]': [expiry],
        })

    def _batch(self):
        from apps.tenant.inventory.models import InventoryBatch
        inv = Invoice.objects.get(invoice_type=Invoice.PURCHASE)
        return InventoryBatch.objects.get(source_invoice_line=inv.lines.get())

    def test_expiry_saved_on_batch_when_tracking_enabled(self):
        system_settings(enable_expiry_tracking=True)
        resp = self._post('2027-03-31')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._batch().expiry_date, datetime.date(2027, 3, 31))

    def test_expiry_ignored_when_tracking_disabled(self):
        system_settings(enable_expiry_tracking=False)
        resp = self._post('2027-03-31')
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self._batch().expiry_date)
