"""
Tests for P1-6: Removal of hidden side-effects, swallowed errors, GET writes, and unified opening balance behavior.
1. Opening balance entries for Treasury / BankAccount / EWallet credit account 3999 and honor custom opening_balance_date.
2. Treasury has explicit treasury_type ('CASH' / 'BANK') mapping directly to parent 1110 / 1120 regardless of name.
3. Failed opening balance booking propagates and rolls back Treasury creation atomically.
4. _get_system_account raises ValueError when an account is missing without auto-seeding the chart of accounts.
5. Read-only GET views (liquidity dashboard, treasury statement, liquidity position) do not invoke ensure_account().
"""
import datetime
from decimal import Decimal as D
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import Account, BankAccount, JournalEntry, Treasury
from apps.tenant.core.tests.factories import build_world, login, make_treasury
from apps.tenant.services.journal_service import _get_system_account


class SideEffectsSafeguardsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_treasury_opening_balance_posts_to_3999_and_honors_custom_date(self):
        """Treasury opening balance credits account 3999 (opening balances) and uses opening_balance_date."""
        custom_date = datetime.date(2025, 1, 1)
        t = make_treasury(
            self.w.branch,
            'خزينة فرعية تجريبية',
            opening_balance=D('1000'),
            opening_balance_date=custom_date,
        )
        entry = JournalEntry.objects.filter(reference=f'OPENING-TR-{t.id}').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, JournalEntry.POSTED)
        self.assertEqual(entry.date, custom_date)

        debit_item = entry.items.filter(debit__gt=0).first()
        credit_item = entry.items.filter(credit__gt=0).first()
        self.assertEqual(debit_item.account, t.account)
        self.assertEqual(credit_item.account.code, '3999')

    def test_treasury_has_explicit_treasury_type(self):
        """Treasury maps to 1110 or 1120 via explicit treasury_type even when name contains 'بنك'."""
        t_cash = Treasury.objects.create(
            name='خزينة بنكنوت نقدية', branch=self.w.branch, treasury_type=Treasury.CASH
        )
        self.assertEqual(t_cash.account.parent.code, '1110')

        t_bank = Treasury.objects.create(
            name='حساب جاري فرعي', branch=self.w.branch, treasury_type=Treasury.BANK
        )
        self.assertEqual(t_bank.account.parent.code, '1120')

    def test_book_opening_balance_error_propagates_and_rolls_back(self):
        """When booking opening balance fails during Treasury creation, exception propagates and Treasury rolls back."""
        with patch('apps.tenant.accounting.models.book_opening_balance', side_effect=RuntimeError("Posting failed")):
            with self.assertRaises(RuntimeError):
                Treasury.objects.create(
                    name='خزينة ستفشل',
                    branch=self.w.branch,
                    opening_balance=D('500'),
                )
        self.assertFalse(Treasury.objects.filter(name='خزينة ستفشل').exists())

    def test_get_system_account_fails_cleanly_without_autoseeding(self):
        """When an account is missing, _get_system_account raises ValueError without calling setup_chart_of_accounts_data."""
        with self.assertRaises(ValueError) as ctx:
            _get_system_account('9999999')
        self.assertIn('9999999', str(ctx.exception))

        # Standard system account deleted -> raises ValueError and does NOT auto-seed it back
        Account.objects.filter(code='4100').delete()
        with self.assertRaises(ValueError) as ctx2:
            _get_system_account('4100')
        self.assertIn('4100', str(ctx2.exception))
        self.assertFalse(Account.objects.filter(code='4100').exists())

    def test_get_report_views_do_not_call_ensure_account(self):
        """Read-only GET endpoints never trigger ensure_account() database writes."""
        login(self.client, self.w.admin)
        with patch.object(Treasury, 'ensure_account', side_effect=AssertionError("DB write on GET!")) as mock_tr, \
             patch.object(BankAccount, 'ensure_account', side_effect=AssertionError("DB write on GET!")) as mock_bk:
            for url_name in (
                'accounting:liquidity_dashboard',
                'core:report_treasury',
                'core:report_liquidity_position',
            ):
                resp = self.client.get(reverse(url_name))
                self.assertEqual(resp.status_code, 200, url_name)
        mock_tr.assert_not_called()
        mock_bk.assert_not_called()
