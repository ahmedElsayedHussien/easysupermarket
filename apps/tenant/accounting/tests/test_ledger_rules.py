"""ثوابت دفتر الأستاذ: الترحيل من باب واحد (post) بفحص كامل، والقيد المرحّل ثابت — التصحيح بقيد عكسي."""
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.tenant.accounting.models import JournalEntry, JournalItem
from apps.tenant.core.tests.factories import account, build_world, make_partner


class LedgerRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.cash = cls.w.treasury.ensure_account()            # a leaf (1110-x)

    def draft(self, debit_acc=None, amount=D('100'), ref='R'):
        e = JournalEntry.objects.create(date=__import__('datetime').date.today(), reference=ref, description='t')
        JournalItem.objects.create(entry=e, account=debit_acc or self.cash, debit=amount, credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=amount)
        return e

    def test_created_posted_is_a_draft_until_post(self):
        e = JournalEntry.objects.create(date=__import__('datetime').date.today(), reference='X', description='t',
                                        status=JournalEntry.POSTED)
        self.assertEqual(e.status, JournalEntry.DRAFT)

    def test_post_checks_and_records_who_and_when(self):
        e = self.draft()
        e.post(self.w.admin)
        e.refresh_from_db()
        self.assertEqual((e.status, e.posted_by), (JournalEntry.POSTED, self.w.admin))
        self.assertIsNotNone(e.posted_at)
        with self.assertRaisesMessage(ValidationError, 'مرحّل بالفعل'):
            e.post()

    def test_empty_unbalanced_and_group_accounts_refused(self):
        empty = JournalEntry.objects.create(date=__import__('datetime').date.today(), reference='E', description='t')
        with self.assertRaisesMessage(ValidationError, 'فاضي'):
            empty.post()
        bad = self.draft(ref='B')
        JournalItem.objects.create(entry=bad, account=self.cash, debit=D('5'), credit=0)
        with self.assertRaisesMessage(ValidationError, 'غير متوازن'):
            bad.post()
        group = self.draft(debit_acc=account('1110'), ref='G')                 # 1110 has sub-accounts
        with self.assertRaisesMessage(ValidationError, 'حساب رئيسي'):
            group.post()

    def test_posted_entry_is_immutable(self):
        e = self.draft()
        e.post()
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            e.delete()
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            JournalItem.objects.create(entry=e, account=self.cash, debit=D('1'), credit=0)
        item = e.items.first()
        item.debit = D('999')
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            item.save()
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            item.delete()
        e.date = e.date.replace(year=e.date.year - 1)
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            e.save()
        e.refresh_from_db()
        e.description = 'وصف أوضح'                                              # a description may change
        e.save()

    def test_partner_opening_change_reverses_instead_of_rewriting(self):
        p = make_partner('عميل برصيد', 'CUSTOMER', opening_balance=D('500'), opening_balance_type='DEBIT')
        p.refresh_from_db()
        first = p.opening_journal_entry
        self.assertEqual(first.status, JournalEntry.POSTED)
        p.opening_balance = D('800')
        p.save()
        p.refresh_from_db()
        second = p.opening_journal_entry
        self.assertNotEqual(first.pk, second.pk)
        first.refresh_from_db()
        self.assertEqual(sum(i.debit for i in first.items.all()), D('500'))        # the old one untouched
        self.assertTrue(JournalEntry.objects.filter(reversal_of=first).exists())   # …and reversed
        self.assertEqual(p.receivable_account.current_balance, D('800.00'))
        p.save()                                                                    # unchanged → nothing new
        self.assertEqual(JournalEntry.objects.filter(reference__startswith=f'OPENING-PRT-{p.pk}').count(), 2)

    def test_inactive_account_refused(self):
        stopped = account('1195')
        stopped.is_active = False
        stopped.save(update_fields=['is_active'])
        e = self.draft(debit_acc=stopped, ref='IN')
        with self.assertRaisesMessage(ValidationError, 'حساب موقوف'):
            e.post()

    def test_central_post_entry(self):
        from apps.tenant.accounting.posting import post_entry
        e = post_entry(reference='CENTRAL', description='تجربة', user=self.w.admin, source=self.w.treasury,
                       lines=[(self.cash, D('50'), 0, 'نقدية'), (account('3100'), 0, D('50'), ''),
                              (account('3100'), 0, 0, 'سطر صفري يتشال')])
        self.assertEqual((e.status, e.items.count(), e.posted_by), (JournalEntry.POSTED, 2, self.w.admin))
        self.assertEqual(e.source_document, self.w.treasury)
        again = post_entry(reference='CENTRAL', description='نفس المرجع',
                           lines=[(self.cash, D('1'), 0), (account('3100'), 0, D('1'))])
        self.assertNotEqual(again.reference, e.reference)                     # a used reference gets a suffix
        with self.assertRaisesMessage(ValidationError, 'غير متوازن'):
            post_entry(reference='BAD', description='x', lines=[(self.cash, D('5'), 0), (account('3100'), 0, D('4'))])
        self.assertFalse(JournalEntry.objects.filter(reference='BAD').exists())   # nothing half-written


class BulkWriteGuardTests(TestCase):
    """The queryset (bulk) paths follow the same rule: a posted entry is changed only by a reversal."""
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.cash = cls.w.treasury.ensure_account()

    def setUp(self):
        import datetime
        self.e = JournalEntry.objects.create(date=datetime.date.today(), reference='BULK', description='t')
        JournalItem.objects.create(entry=self.e, account=self.cash, debit=D('10'), credit=0)
        JournalItem.objects.create(entry=self.e, account=account('3100'), debit=0, credit=D('10'))
        self.e.post()

    def test_bulk_update_and_delete_refused(self):
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            JournalItem.objects.filter(entry=self.e).update(debit=0)
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            JournalItem.objects.filter(entry=self.e).delete()
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            JournalItem.objects.bulk_create([JournalItem(entry=self.e, account=self.cash, debit=D('1'))])
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            JournalEntry.objects.filter(pk=self.e.pk).update(status=JournalEntry.DRAFT)
        with self.assertRaisesMessage(ValidationError, 'قيد عكسي'):
            JournalEntry.objects.filter(pk=self.e.pk).delete()
        self.assertEqual(self.e.get_totals(), (D('10'), D('10')))
        JournalEntry.objects.filter(pk=self.e.pk).update(description='بيان جديد')   # no balance effect: allowed


class SplitVoucherExactTests(TestCase):
    def test_split_parts_must_equal_amount(self):
        from apps.tenant.accounting.models import Voucher
        w = build_world()
        v = Voucher.objects.create(voucher_type=Voucher.RECEIPT, amount=D('100'), partner=w.customer,
                                   account=w.customer.receivable_account, payment_method=Voucher.SPLIT,
                                   treasury=w.treasury, paid_cash=D('100.03'))
        with self.assertRaisesMessage(ValidationError, 'لا يساوي'):
            v.confirm_voucher()
        Voucher.objects.filter(pk=v.pk).update(paid_cash=D('100'))
        v.refresh_from_db()
        v.confirm_voucher()
        self.assertEqual(v.status, Voucher.POSTED)
