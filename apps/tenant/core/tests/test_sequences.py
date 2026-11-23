import datetime
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.core.models import DocumentSequence
from apps.tenant.core.sequences import next_sequence
from apps.tenant.core.tests.factories import build_world, make_invoice


class NextSequenceTests(TestCase):
    def test_increments(self):
        self.assertEqual(next_sequence('T:A-'), 1)
        self.assertEqual(next_sequence('T:A-'), 2)
        self.assertEqual(next_sequence('T:A-'), 3)
        self.assertEqual(DocumentSequence.objects.get(key='T:A-').last_value, 3)

    def test_seed_used_only_on_first_call(self):
        calls = []

        def seed():
            calls.append(1)
            return 41

        self.assertEqual(next_sequence('T:SEED-', seed=seed), 42)
        self.assertEqual(next_sequence('T:SEED-', seed=seed), 43)
        self.assertEqual(len(calls), 1)

    def test_distinct_keys_are_independent(self):
        self.assertEqual(next_sequence('T:X-'), 1)
        self.assertEqual(next_sequence('T:X-'), 2)
        self.assertEqual(next_sequence('T:Y-'), 1)
        self.assertEqual(next_sequence('T:X-'), 3)


class InvoiceNumberingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def _draft(self):
        return make_invoice(self.w, 'SALE', lines=[(self.w.product, 1, 10)], confirm=False,
                            payment_type='CREDIT')

    def test_two_invoices_get_sequential_numbers(self):
        year = datetime.date.today().year
        a = self._draft()
        b = self._draft()
        self.assertTrue(a.invoice_number.startswith(f'SALE-{year}-'))
        na = int(a.invoice_number.rsplit('-', 1)[1])
        nb = int(b.invoice_number.rsplit('-', 1)[1])
        self.assertEqual(nb, na + 1)

    def test_numbering_continues_past_99999(self):
        from apps.tenant.invoicing.models import Invoice
        year = datetime.date.today().year
        # Existing invoice created before the counter row exists -> seed = 99999
        Invoice.objects.create(
            invoice_number=f'SALE-{year}-99999', invoice_type='SALE', partner=self.w.customer,
            branch=self.w.branch, warehouse=self.w.warehouse, date=datetime.date.today(),
        )
        a = self._draft()
        b = self._draft()
        self.assertEqual(a.invoice_number, f'SALE-{year}-100000')
        self.assertEqual(b.invoice_number, f'SALE-{year}-100001')

    def test_purchase_and_sale_have_separate_counters(self):
        s = self._draft()
        p = make_invoice(self.w, 'PURCHASE', lines=[(self.w.product, 1, 5)], confirm=False,
                         payment_type='CREDIT')
        self.assertTrue(p.invoice_number.startswith('PUR-'))
        self.assertTrue(s.invoice_number.endswith('00001'))
        self.assertTrue(p.invoice_number.endswith('00001'))
