"""P3-4 (6/6): manage.py check_money_precision — posted invoices equal their entries to the piaster, no
sub-piaster partner balance, then inventory_ledger_check."""
import io
from decimal import Decimal as D

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from apps.tenant.core.tests.factories import build_world, make_bank_account, make_invoice, stock_in


class CheckMoneyPrecisionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 100, D('3.3333'))
        bank = make_bank_account('ماكينة فيزا')
        self.cash = make_invoice(self.w, lines=[(self.w.product, 3, D('33.3333'), 14)])
        make_invoice(self.w, lines=[(self.w.product, 2, D('19.99'), 14)], payment_type='CARD', bank_account=bank)
        make_invoice(self.w, lines=[(self.w.product, 1, D('10.33'))], payment_type='SPLIT', treasury=self.w.treasury,
                     bank_account=bank, paid_cash=D('5.33'), paid_card=D('5'))
        make_invoice(self.w, lines=[(self.w.product, 7, D('0.3333'), 14)], payment_type='CREDIT')
        make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.w.product, 5, D('3.3333'), 14)],
                     payment_type='CREDIT')

    def run_check(self):
        out = io.StringIO()
        call_command('check_money_precision', stdout=out)
        return out.getvalue()

    def test_clean_books_pass(self):
        out = self.run_check()
        self.assertIn('كل الفواتير مطابقة لقيودها بالقرش', out)
        self.assertIn('الأستاذ 1140', out)                                  # inventory_ledger_check ran after it

    def test_an_invoice_that_does_not_match_its_entry_is_reported(self):
        with connection.cursor() as c:                                       # bypass the posted-invoice guard
            c.execute('UPDATE invoicing_invoice SET total_amount = %s WHERE id = %s',
                      [str(self.cash.total_amount + D('0.01')), self.cash.id])
        with self.assertRaisesMessage(CommandError, 'مشكلة'):
            self.run_check()
        from apps.tenant.core.management.commands.check_money_precision import invoice_problems
        problems = invoice_problems()
        self.assertEqual([number for number, _ in problems], [self.cash.invoice_number])
        self.assertIn('طرف الدفع', problems[0][1])

    def test_sub_piaster_partner_balance_is_reported(self):
        from apps.tenant.accounting.posting import post_entry
        from apps.tenant.core.management.commands.check_money_precision import partner_problems
        from apps.tenant.core.tests.factories import account
        self.assertEqual(partner_problems(), [])
        acc = self.w.customer.receivable_account
        post_entry(reference='SUB-PIASTER', description='اختبار', lines=[(acc, D('0.0040'), D('0')),
                                                                          (account('3100'), D('0'), D('0.0040'))])
        self.assertEqual([name for name, _ in partner_problems()], [self.w.customer.name])
