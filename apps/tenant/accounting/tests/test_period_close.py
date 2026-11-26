"""إقفال الشهور (accounting.periods) وعكس القيود اليدوية من الشاشة."""
import datetime
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting import periods
from apps.tenant.accounting.models import FiscalYearClose, JournalEntry, JournalItem, PeriodClose
from apps.tenant.core.forms import SystemSettingForm
from apps.tenant.core.models import SystemSetting
from apps.tenant.core.tests.factories import account, build_world, login

D1 = datetime.date(2025, 3, 10)


class PeriodCloseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.cash = cls.w.treasury.ensure_account()

    def entry(self, date=D1, post=True, ref='P'):
        e = JournalEntry.objects.create(date=date, reference=ref, description='t')
        JournalItem.objects.create(entry=e, account=self.cash, debit=D('50'), credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=D('50'))
        if post:
            e.post()
        return e

    def test_close_sets_lock_and_records_history(self):
        self.entry()
        periods.close_until(datetime.date(2025, 3, 31), self.w.admin, 'مارس')
        self.assertEqual(SystemSetting.get_settings().accounting_lock_date, datetime.date(2025, 3, 31))
        log = PeriodClose.objects.get()
        self.assertEqual((log.action, log.previous_lock_date, log.user, log.note),
                         (PeriodClose.CLOSE, None, self.w.admin, 'مارس'))
        with self.assertRaises(ValidationError):                    # the period is now frozen
            self.entry(date=datetime.date(2025, 3, 20), ref='Q')

    def test_close_refused_with_drafts_or_not_after_lock(self):
        self.entry(post=False)
        with self.assertRaisesMessage(ValidationError, 'مسودة'):
            periods.close_until(datetime.date(2025, 3, 31))
        JournalEntry.objects.all().delete()
        periods.close_until(datetime.date(2025, 3, 31))
        with self.assertRaisesMessage(ValidationError, 'مقفولة أصلاً'):
            periods.close_until(datetime.date(2025, 3, 1))

    def test_reopen_needs_reason_and_respects_closed_years(self):
        periods.close_until(datetime.date(2025, 3, 31))
        with self.assertRaisesMessage(ValidationError, 'سبب'):
            periods.reopen_to(datetime.date(2025, 2, 28), self.w.admin, '')
        periods.reopen_to(datetime.date(2025, 2, 28), self.w.admin, 'تصحيح فاتورة')
        self.assertEqual(SystemSetting.get_settings().accounting_lock_date, datetime.date(2025, 2, 28))
        self.assertEqual(PeriodClose.objects.filter(action=PeriodClose.REOPEN).count(), 1)
        FiscalYearClose.objects.create(year=2024)
        with self.assertRaisesMessage(ValidationError, 'سنة 2024 مقفولة'):
            periods.reopen_to(datetime.date(2024, 6, 30), self.w.admin, 'تصحيح')
        with self.assertRaisesMessage(ValidationError, 'سنة 2024 مقفولة'):
            periods.reopen_to(None, self.w.admin, 'تصحيح')

    def test_settings_form_cannot_edit_lock_date(self):
        self.assertNotIn('accounting_lock_date', SystemSettingForm().fields)

    def test_screen_close_and_reopen(self):
        login(self.client, self.w.admin)
        url = reverse('accounting:fiscal_years')
        self.client.post(url, {'action': 'close_period', 'until': '2025-03-31', 'note': 'x'})
        self.assertEqual(periods.current_lock(), datetime.date(2025, 3, 31))
        self.client.post(url, {'action': 'reopen_period', 'to': '', 'reason': ''})
        self.assertEqual(periods.current_lock(), datetime.date(2025, 3, 31))      # no reason -> refused
        self.client.post(url, {'action': 'reopen_period', 'to': '', 'reason': 'غلط في مارس'})
        self.assertIsNone(periods.current_lock())
        self.assertContains(self.client.get(url), 'غلط في مارس')


class ManualReversalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.cash = cls.w.treasury.ensure_account()

    def setUp(self):
        login(self.client, self.w.admin)
        self.e = JournalEntry.objects.create(date=datetime.date.today(), reference='M1', description='يدوي')
        JournalItem.objects.create(entry=self.e, account=self.cash, debit=D('70'), credit=0)
        JournalItem.objects.create(entry=self.e, account=account('3100'), debit=0, credit=D('70'))
        self.e.post()

    def test_reason_required_then_reversed(self):
        url = reverse('accounting:journal_reverse', args=[self.e.pk])
        self.client.post(url, {'reason': ''})
        self.assertIsNone(self.e.reversing_entry)
        self.assertContains(self.client.get(reverse('accounting:journal_detail', args=[self.e.pk])), 'قيد عكسي')
        self.client.post(url, {'reason': 'اتسجل بالغلط'})
        self.e.refresh_from_db()
        rev = self.e.reversing_entry
        self.assertIsNotNone(rev)
        self.assertEqual(rev.status, JournalEntry.POSTED)
        self.assertIn('اتسجل بالغلط', rev.description)
        self.assertEqual(rev.items.get(account=self.cash).credit, D('70'))
        self.client.post(url, {'reason': 'تاني'})                               # never twice
        self.assertEqual(JournalEntry.objects.filter(reversal_of=self.e).count(), 1)

    def test_document_entry_refused(self):
        from django.contrib.contenttypes.models import ContentType
        JournalEntry.objects.filter(pk=self.e.pk).update(
            content_type=ContentType.objects.get_for_model(self.w.treasury), object_id=self.w.treasury.pk)
        self.client.post(reverse('accounting:journal_reverse', args=[self.e.pk]), {'reason': 'سبب كافي'})
        self.assertFalse(JournalEntry.objects.filter(reversal_of=self.e).exists())
