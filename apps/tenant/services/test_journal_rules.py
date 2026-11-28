"""journal_service: حسابات النظام بتتعمل لما تنقص، مفيش ترحيل صامت على حساب رئيسي، ترقيم المرتجع من سلسلته،
مرتجع المشتريات بيخرج المخزون بتكلفته والفرق على 5110، والقيد باسم الكاشير."""
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.tenant.accounting.models import Account
from apps.tenant.core.tests.factories import account, build_world, make_invoice, stock_in
from apps.tenant.services.journal_service import _get_system_account, _money_account


class JournalServiceRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_lazy_system_account_created_when_missing(self):
        Account.objects.filter(code='1155').delete()
        acc = _get_system_account('1155')
        self.assertEqual((acc.code, acc.parent.code), ('1155', '1100'))

    def test_no_silent_posting_on_a_group_account(self):
        self.w.treasury.ensure_account()                       # 1110 now has a sub-account
        with self.assertRaisesMessage(ValidationError, 'الخزينة'):
            _money_account([None], '1110', 'فاتورة تجربة')

    def test_return_series_and_cashier_recorded(self):
        stock_in(self.w.product, self.w.warehouse, 20, D('5'))
        sale = make_invoice(self.w, lines=[(self.w.product, 3, 10)])
        ret = make_invoice(self.w, 'RETURN_SALE', lines=[(self.w.product, 1, 10)], parent_invoice=sale)
        self.assertTrue(sale.journal_entry.reference.startswith('JE-SALE-'))
        self.assertTrue(ret.journal_entry.reference.startswith('JE-RSAL-'))
        self.assertEqual((sale.journal_entry.created_by, sale.journal_entry.posted_by), (self.w.admin, self.w.admin))

    def test_purchase_return_leaves_stock_at_cost_gap_to_5110(self):
        from django.db.models import F, Sum
        from apps.tenant.inventory.models import InventoryBatch
        make_invoice(self.w, 'PURCHASE', lines=[(self.w.product, 10, 5)])                    # cost 5
        ret = make_invoice(self.w, 'RETURN_PURCHASE', lines=[(self.w.product, 2, 7)])      # returned at 7
        items = ret.journal_entry.items
        self.assertTrue(items.filter(account=account('1140'), credit=D('10')).exists())    # 2 x 5 (its cost)
        self.assertTrue(items.filter(account=account('5110'), credit=D('4')).exists())     # 2 x (7 - 5)
        self.assertTrue(ret.journal_entry.is_balanced())
        stock_value = InventoryBatch.objects.aggregate(v=Sum(F('quantity_remaining') * F('unit_cost')))['v']
        inv = account('1140')
        ledger = sum((i.debit - i.credit for i in inv.journal_items.filter(entry__status='POSTED')), D('0'))
        self.assertEqual(ledger, stock_value)                                               # 8 x 5 = 40

    def test_payment_posted_once(self):
        import datetime
        from apps.tenant.partners.models import Payment
        from apps.tenant.services.journal_service import post_payment
        p = Payment.objects.create(payment_type=Payment.RECEIPT, partner=self.w.customer, branch=self.w.branch,
                                   amount=D('50'), date=datetime.date.today(), method=Payment.CASH)
        post_payment(p)
        with self.assertRaisesMessage(ValueError, 'مرحّلة بالفعل'):
            post_payment(p)
