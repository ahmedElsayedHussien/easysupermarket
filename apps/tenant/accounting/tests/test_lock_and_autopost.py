"""
Accounting settings:
  - SystemSetting.accounting_lock_date: closed-period journal entries are frozen
    (model layer), documents of a closed period can still be cancelled by reversal.
  - SystemSetting.auto_post_journals: vouchers / expenses posted on save or left DRAFT.
  - Per-branch POS defaults (Branch.default_pos_*) used for vouchers.
"""
import datetime
from decimal import Decimal as D

from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import Expense, JournalEntry, JournalItem, Voucher
from apps.tenant.core.tests.factories import (
    account, build_world, login, make_treasury, system_settings,
)

TODAY = datetime.date.today()
LOCK = TODAY - datetime.timedelta(days=10)
LOCKED_DAY = TODAY - datetime.timedelta(days=20)
OPEN_DAY = TODAY - datetime.timedelta(days=5)


def _entry(date, ref, amount=D('100')):
    e = JournalEntry.objects.create(date=date, reference=ref, description='test', status=JournalEntry.DRAFT)
    JournalItem.objects.create(entry=e, account=account('1110'), debit=amount, credit=0)
    JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=amount)
    e.post()
    return e


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class PeriodLockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def lock(self):
        system_settings(accounting_lock_date=LOCK)

    def test_entry_before_lock_date_rejected(self):
        self.lock()
        with self.assertRaises(ValidationError) as cm:
            JournalEntry.objects.create(date=LOCKED_DAY, reference='T-LOCKED', description='x')
        self.assertIn('مقفلة', ' '.join(cm.exception.messages))
        # on the lock date itself is also closed
        with self.assertRaises(ValidationError):
            JournalEntry.objects.create(date=LOCK, reference='T-LOCK-DAY', description='x')
        self.assertFalse(JournalEntry.objects.filter(reference__in=['T-LOCKED', 'T-LOCK-DAY']).exists())

    def test_entry_after_lock_date_allowed(self):
        self.lock()
        e = _entry(OPEN_DAY, 'T-OPEN')
        self.assertEqual(e.status, JournalEntry.POSTED)
        self.assertTrue(e.is_balanced())

    def test_existing_locked_entry_is_frozen(self):
        e = _entry(LOCKED_DAY, 'T-OLD')       # posted before the period was closed
        self.lock()
        item = e.items.first()

        e.date = OPEN_DAY                      # moving it out of the closed period
        with self.assertRaises(ValidationError):
            e.save()
        e.refresh_from_db()
        e.status = JournalEntry.DRAFT          # un-posting it
        with self.assertRaises(ValidationError):
            e.save(update_fields=['status'])
        e.refresh_from_db()
        with self.assertRaises(ValidationError):
            e.delete()
        with self.assertRaises(ValidationError):
            JournalItem.objects.create(entry=e, account=account('1110'), debit=D('1'), credit=0)
        item.debit = D('5')
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(ValidationError):
            item.delete()
        # bulk paths
        with self.assertRaises(ValidationError):
            JournalEntry.objects.filter(pk=e.pk).update(status=JournalEntry.DRAFT)
        with self.assertRaises(ValidationError):
            JournalEntry.objects.filter(pk=e.pk).delete()
        with self.assertRaises(ValidationError):
            JournalItem.objects.filter(entry=e).update(debit=D('1'))
        with self.assertRaises(ValidationError):
            e.items.all().delete()
        with self.assertRaises(ValidationError):
            JournalItem.objects.bulk_create([JournalItem(entry=e, account=account('1110'), debit=D('1'), credit=0)])
        # moving an open entry INTO the closed period is refused too
        open_entry = _entry(OPEN_DAY, 'T-OPEN2')
        with self.assertRaises(ValidationError):
            JournalEntry.objects.filter(pk=open_entry.pk).update(date=LOCKED_DAY)

        e.refresh_from_db()
        self.assertEqual(e.status, JournalEntry.POSTED)
        self.assertEqual(e.date, LOCKED_DAY)
        self.assertEqual(e.items.count(), 2)
        self.assertEqual(e.get_total_debit(), D('100'))

        # a balance-neutral field (description) may still be corrected
        e.description = 'وصف مصحح'
        e.save()
        self.assertEqual(JournalEntry.objects.get(pk=e.pk).description, 'وصف مصحح')

    def _posted_voucher(self, date):
        v = Voucher.objects.create(
            voucher_type=Voucher.RECEIPT, amount=D('150'), partner=self.w.customer, date=date,
            account=self.w.customer.receivable_account, payment_method=Voucher.CASH,
            treasury=self.w.treasury, branch=self.w.branch,
        )
        v.confirm_voucher()
        v.refresh_from_db()
        return v

    def test_reversing_locked_period_voucher_works(self):
        v = self._posted_voucher(LOCKED_DAY)
        original = v.journal_entry
        self.lock()
        v.cancel_voucher(user=self.w.admin)
        v.refresh_from_db()
        self.assertEqual(v.status, Voucher.CANCELLED)
        reversal = JournalEntry.objects.get(reversal_of=original)
        self.assertEqual(reversal.date, TODAY)                    # reversal dated in the open period
        self.assertEqual(reversal.status, JournalEntry.POSTED)
        original.refresh_from_db()
        self.assertEqual(original.status, JournalEntry.POSTED)  # original untouched
        self.assertEqual(original.date, LOCKED_DAY)

    def test_cancel_view_on_locked_voucher_works(self):
        v = self._posted_voucher(LOCKED_DAY)
        self.lock()
        c = login(self.client, self.w.admin)
        r = c.post(reverse('accounting:voucher_cancel', args=[v.pk]))
        self.assertEqual(r.status_code, 302)
        v.refresh_from_db()
        self.assertEqual(v.status, Voucher.CANCELLED)

    def test_editing_locked_voucher_rejected_with_message(self):
        v = self._posted_voucher(LOCKED_DAY)
        original = v.journal_entry
        self.lock()
        c = login(self.client, self.w.admin)
        r = c.post(reverse('accounting:voucher_update', args=[v.pk]), {
            'date': LOCKED_DAY.isoformat(), 'amount': '200', 'partner': self.w.customer.pk,
            'payment_method': Voucher.CASH, 'treasury': self.w.treasury.pk, 'description': 'edit',
        })
        self.assertEqual(r.status_code, 200)              # form re-rendered, not a 500
        self.assertIn('الفترة المحاسبية مقفلة', r.content.decode())
        v.refresh_from_db()
        self.assertEqual(v.status, Voucher.POSTED)
        self.assertEqual(v.amount, D('150'))
        self.assertFalse(JournalEntry.objects.filter(reversal_of=original).exists())

    def test_editing_locked_voucher_to_open_date_rejected(self):
        """Even moving the date out of the closed period is refused for a posted voucher."""
        v = self._posted_voucher(LOCKED_DAY)
        self.lock()
        c = login(self.client, self.w.admin)
        r = c.post(reverse('accounting:voucher_update', args=[v.pk]), {
            'date': OPEN_DAY.isoformat(), 'amount': '150', 'partner': self.w.customer.pk,
            'payment_method': Voucher.CASH, 'treasury': self.w.treasury.pk, 'description': 'edit',
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn('الفترة المحاسبية مقفلة', r.content.decode())
        v.refresh_from_db()
        self.assertEqual(v.date, LOCKED_DAY)

    def test_manual_journal_form_shows_lock_error(self):
        self.lock()
        c = login(self.client, self.w.admin)
        r = c.post(reverse('accounting:journal_create'), {
            'date': LOCKED_DAY.isoformat(), 'description': 'manual',
            'items-TOTAL_FORMS': '2', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            'items-0-account': account('1110').pk, 'items-0-debit': '10', 'items-0-credit': '0',
            'items-1-account': account('3100').pk, 'items-1-debit': '0', 'items-1-credit': '10',
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn('الفترة المحاسبية مقفلة', r.content.decode())
        self.assertFalse(JournalEntry.objects.filter(description='manual').exists())

    def test_journal_pages_show_lock_notice(self):
        e = _entry(LOCKED_DAY, 'T-NOTICE')
        c = login(self.client, self.w.admin)
        r = c.get(reverse('accounting:journal_list'))
        self.assertNotIn('الفترة مقفلة حتى', r.content.decode())
        self.lock()
        r = c.get(reverse('accounting:journal_list'))
        self.assertIn('الفترة مقفلة حتى', r.content.decode())
        self.assertIn(LOCK.strftime('%Y-%m-%d'), r.content.decode())
        r = c.get(reverse('accounting:journal_detail', args=[e.pk]))
        self.assertIn('الفترة مقفلة حتى', r.content.decode())
        self.assertIn('هذا القيد ضمن الفترة المقفلة', r.content.decode())


class AutoPostTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def _create_receipt(self, **extra):
        data = {
            'date': TODAY.isoformat(), 'amount': '120', 'partner': self.w.customer.pk,
            'payment_method': Voucher.CASH, 'treasury': self.w.treasury.pk, 'description': 'auto-post test',
        }
        data.update(extra)
        c = login(self.client, self.w.admin)
        r = c.post(reverse('accounting:receipt_create'), data)
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        return Voucher.objects.get(description='auto-post test'), r

    def _create_expense(self):
        c = login(self.client, self.w.admin)
        r = c.post(reverse('accounting:expense_create'), {
            'date': TODAY.isoformat(), 'branch': self.w.branch.pk,
            'expense_account': account('5210').pk, 'payment_account': self.w.treasury.account.pk,
            'amount': '80', 'vat_percent': '0', 'withholding_tax_percent': '0', 'description': 'auto-post exp',
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            'items-0-description': 'كهرباء', 'items-0-amount': '80',
        })
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        return Expense.objects.get(description='auto-post exp'), r

    def test_voucher_auto_posted_when_setting_on(self):
        system_settings(auto_post_journals=True)
        v, _ = self._create_receipt()
        self.assertEqual(v.status, Voucher.POSTED)
        self.assertIsNotNone(v.journal_entry)

    def test_voucher_left_draft_when_setting_off(self):
        system_settings(auto_post_journals=False)
        v, r = self._create_receipt()
        self.assertEqual(v.status, Voucher.DRAFT)
        self.assertIsNone(v.journal_entry)
        self.assertTrue(any('تم الحفظ كمسودة' in m for m in _messages(r)))

    def test_expense_auto_posted_when_setting_on(self):
        system_settings(auto_post_journals=True)
        e, _ = self._create_expense()
        self.assertEqual(e.status, Expense.POSTED)
        self.assertIsNotNone(e.journal_entry)
        self.assertTrue(e.journal_entry.is_balanced())

    def test_expense_left_draft_when_setting_off(self):
        system_settings(auto_post_journals=False)
        e, r = self._create_expense()
        self.assertEqual(e.status, Expense.DRAFT)
        self.assertIsNone(e.journal_entry)
        self.assertTrue(any('تم الحفظ كمسودة' in m for m in _messages(r)))


class BranchPosDefaultsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.branch_treasury = make_treasury(cls.w.branch, 'خزينة افتراضية للفرع')
        # global default = the main treasury, branch override = branch_treasury
        system_settings(default_pos_treasury=cls.w.treasury)
        cls.w.branch.default_pos_treasury = cls.branch_treasury
        cls.w.branch.save(update_fields=['default_pos_treasury'])

    def test_branch_default_treasury_used_for_voucher(self):
        c = login(self.client, self.w.admin)
        r = c.post(reverse('accounting:receipt_create'), {
            'date': TODAY.isoformat(), 'amount': '60', 'partner': self.w.customer.pk,
            'payment_method': Voucher.CASH, 'description': 'branch default',
        })
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        v = Voucher.objects.get(description='branch default')
        self.assertEqual(v.treasury, self.branch_treasury)

    def test_model_fallback_uses_document_branch_default(self):
        v = Voucher.objects.create(
            voucher_type=Voucher.RECEIPT, amount=D('10'), partner=self.w.customer,
            account=self.w.customer.receivable_account, payment_method=Voucher.CASH, branch=self.w.branch,
        )
        self.assertEqual(v.payment_account, self.branch_treasury.ensure_account())
        self.assertEqual(Voucher.objects.get(pk=v.pk).treasury, self.branch_treasury)
