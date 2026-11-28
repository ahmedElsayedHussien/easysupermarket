"""P2-2: شاشة السند والتحويل بتبعت مفتاح منع التكرار — نفس الحفظ مرتين = مستند واحد، والمفتاح بيتطابق مع نفس المستخدم بس."""
import datetime
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import InternalTransfer, Voucher
from apps.tenant.core.tests.factories import build_world, login, make_bank_account


class FormIdempotencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.bank = make_bank_account('بنك التحويل')
        from apps.tenant.accounting.models import JournalEntry, JournalItem
        from apps.tenant.core.tests.factories import account
        e = JournalEntry.objects.create(date=datetime.date.today(), reference='CAP-IDEM', description='رأس مال')
        JournalItem.objects.create(entry=e, account=cls.w.treasury.ensure_account(), debit=D('1000'), credit=0)
        JournalItem.objects.create(entry=e, account=account('3100'), debit=0, credit=D('1000'))
        e.post()

    def setUp(self):
        login(self.client, self.w.admin)

    def test_voucher_double_submit_makes_one_voucher(self):
        page = self.client.get(reverse('accounting:receipt_create'))
        key = str(page.context['idempotency_key'])
        self.assertContains(page, f'name="idempotency_key" value="{key}"')
        data = {'date': datetime.date.today().isoformat(), 'amount': '60', 'partner': self.w.customer.pk,
                'payment_method': Voucher.CASH, 'treasury': self.w.treasury.pk, 'description': 'مرتين',
                'idempotency_key': key}
        r1 = self.client.post(reverse('accounting:receipt_create'), data)
        r2 = self.client.post(reverse('accounting:receipt_create'), data)
        self.assertEqual((r1.status_code, r2.status_code), (302, 302))
        self.assertEqual(Voucher.objects.filter(description='مرتين').count(), 1)
        self.assertEqual(Voucher.objects.get(description='مرتين').created_by, self.w.admin)

    def test_transfer_double_submit_makes_one_transfer(self):
        page = self.client.get(reverse('accounting:transfer_create'))
        key = str(page.context['idempotency_key'])
        self.assertContains(page, f'name="idempotency_key" value="{key}"')
        data = {'date': datetime.date.today().isoformat(), 'amount': '40', 'from_account_type': 'TREASURY',
                'from_treasury': self.w.treasury.pk, 'to_account_type': 'BANK', 'to_bank_account': self.bank.pk,
                'description': 'تحويل مرتين', 'idempotency_key': key}
        r1 = self.client.post(reverse('accounting:transfer_create'), data)
        self.assertEqual(r1.status_code, 302, getattr(r1, 'context', None) and r1.context['form'].errors)
        self.client.post(reverse('accounting:transfer_create'), data)
        self.assertEqual(InternalTransfer.objects.filter(description='تحويل مرتين').count(), 1)

    def test_a_key_never_returns_another_users_document(self):
        from apps.tenant.core.idempotency import find_by_idempotency_key
        import uuid
        k = uuid.uuid4()
        Voucher.objects.create(voucher_type=Voucher.RECEIPT, amount=D('5'), account=self.w.customer.receivable_account,
                               treasury=self.w.treasury, idempotency_key=k, created_by=self.w.admin)
        from apps.tenant.core.tests.factories import make_user
        other = make_user('idem_other')
        self.assertIsNone(find_by_idempotency_key(Voucher, k, created_by=other))
        self.assertIsNotNone(find_by_idempotency_key(Voucher, k, created_by=self.w.admin))
