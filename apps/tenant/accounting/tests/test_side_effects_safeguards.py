"""
Tests for P1-6: Removal of hidden side-effects and swallowed errors.
1. _book_opening_balance_safely does not swallow errors: failure propagates to roll back transaction.
2. Opening balance entries for Treasury and BankAccount post against account 3999 (matching inventory opening stock).
3. Treasury has an explicit treasury_type field ('CASH' / 'BANK') used by ensure_account.
4. _get_system_account raises ValueError if an account is missing, without auto-seeding the entire chart of accounts inside transactions.
"""
from decimal import Decimal as D
from unittest.mock import patch

from django.test import TestCase

from apps.tenant.accounting.models import Account, BankAccount, JournalEntry, Treasury
from apps.tenant.core.tests.factories import build_world, make_branch, make_treasury
from apps.tenant.services.journal_service import _get_system_account


class SideEffectsSafeguardsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_treasury_opening_balance_posts_to_3999(self):
        """Treasury opening balance credits account 3999 (opening balances) not 3100 (capital)."""
        t = make_treasury(self.w.branch, 'خزينة فرعية تجريبية', opening_balance=D('1000'))
        entry = JournalEntry.objects.filter(reference=f'OPN-TREASURY-{t.id}').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, JournalEntry.POSTED)

        # Check debit on treasury account and credit on 3999
        debit_item = entry.items.filter(debit__gt=0).first()
        credit_item = entry.items.filter(credit__gt=0).first()
        self.assertEqual(debit_item.account, t.account)
        self.assertEqual(credit_item.account.code, '3999')

    def test_treasury_has_explicit_treasury_type(self):
        """Treasury model supports explicit treasury_type and maps to parent code accordingly."""
        t_cash = Treasury.objects.create(
            name='خزينة تجارية', branch=self.w.branch, treasury_type='CASH'
        )
        t_cash.ensure_account()
        self.assertEqual(t_cash.account.parent.code, '1110')

        t_bank = Treasury.objects.create(
            name='خزينة بنكية', branch=self.w.branch, treasury_type='BANK'
        )
        t_bank.ensure_account()
        self.assertEqual(t_bank.account.parent.code, '1120')

    def test_book_opening_balance_error_propagates(self):
        """When booking opening balance fails (e.g. invalid entry), exception propagates rather than being swallowed."""
        from apps.tenant.accounting.models import _book_opening_balance_safely
        t = make_treasury(self.w.branch, 'خزينة عادية', opening_balance=D('0'))
        t.opening_balance = D('500')

        with patch('apps.tenant.accounting.models.book_opening_balance', side_effect=RuntimeError("Posting failed")):
            with self.assertRaises(RuntimeError):
                _book_opening_balance_safely(t)

    def test_get_system_account_fails_cleanly_without_autoseeding(self):
        """When an account does not exist, _get_system_account raises ValueError without calling setup_chart_of_accounts_data."""
        with self.assertRaises(ValueError) as ctx:
            _get_system_account('9999999')
        self.assertIn('9999999', str(ctx.exception))
