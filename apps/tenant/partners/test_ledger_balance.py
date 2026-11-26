"""رصيد الشريك وكشف حسابه من دفتر الأستاذ: بيطابقوا المستندات في كل الحالات، والقيد اليدوي على حسابه بيظهر."""
import datetime
from decimal import Decimal as D

from django.test import TestCase

from apps.tenant.accounting.models import Cheque, JournalEntry, JournalItem, Voucher
from apps.tenant.core.tests.factories import build_world, make_invoice, make_partner, stock_in
from apps.tenant.partners.models import Partner
from apps.tenant.partners.services import batch_get_partners_balances, get_partner_full_statement
from apps.tenant.services.invoice_service import cancel_invoice


class LedgerBalanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 500, D('4'))

    def fresh(self, partner):
        return Partner.objects.get(pk=partner.pk)

    def assert_consistent(self, partner):
        p = self.fresh(partner)
        docs = p.document_balances()
        self.assertEqual((p.customer_balance, p.supplier_balance), docs)
        stmt = get_partner_full_statement(p)
        self.assertEqual(stmt['final_balance'], p.customer_balance - p.supplier_balance)
        self.assertEqual(batch_get_partners_balances(partner_qs=Partner.objects.filter(pk=p.pk))[p.pk]['final_balance'],
                         stmt['final_balance'])
        return p, stmt

    def test_customer_documents_match_ledger(self):
        c = make_partner('عميل الأستاذ', 'CUSTOMER', opening_balance=D('100'), opening_balance_type='DEBIT')
        sale = make_invoice(self.w, lines=[(self.w.product, 10, 10)], payment_type='CREDIT', partner=c)   # +100
        make_invoice(self.w, lines=[(self.w.product, 2, 10)], payment_type='CASH', partner=c)               # 0
        cancelled = make_invoice(self.w, lines=[(self.w.product, 3, 10)], payment_type='CREDIT', partner=c)
        cancel_invoice(cancelled.id, self.w.admin)                                                          # 0
        v = Voucher.objects.create(voucher_type=Voucher.RECEIPT, amount=D('40'), partner=c,
                                   account=self.fresh(c).receivable_account, payment_method=Voucher.CASH,
                                   treasury=self.w.treasury)
        v.confirm_voucher()                                                                                  # −40
        chq = Cheque.objects.create(cheque_type='RECEIVABLE', cheque_number='77', amount=D('30'), partner=c,
                                    due_date=datetime.date.today())
        chq.action_receive()                                                                                 # −30
        chq.action_bounce('رصيد غير كافي')                                                                   # +30
        p, stmt = self.assert_consistent(c)
        self.assertEqual(p.customer_balance, D('160'))
        types = [t['doc_type'] for t in stmt['transactions']]
        self.assertIn('فاتورة مبيعات', types)
        self.assertIn('سند قبض', types)
        self.assertIn('ارتداد شيك قبض', types)
        self.assertIn('رصيد أول مدة (افتتاحي)', types)
        self.assertNotIn(cancelled.invoice_number, [t['reference'] for t in stmt['transactions']])   # cancelled: out
        sale_row = next(t for t in stmt['transactions'] if t['reference'] == sale.invoice_number)
        self.assertTrue(sale_row['has_lines'])

    def test_supplier_documents_match_ledger(self):
        s = make_partner('مورد الأستاذ', 'SUPPLIER')
        make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.w.product, 10, 5)], payment_type='CREDIT', partner=s)
        make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.w.product, 4, 5)], payment_type='CASH', partner=s)
        p, _ = self.assert_consistent(s)
        self.assertEqual(p.supplier_balance, D('50'))

    def test_manual_entry_on_partner_account_counts(self):
        c = make_partner('عميل إعدام', 'CUSTOMER')
        make_invoice(self.w, lines=[(self.w.product, 10, 10)], payment_type='CREDIT', partner=c)
        rec = self.fresh(c).receivable_account
        from apps.tenant.services.journal_service import PAYMENT_FEES_CODE, _get_system_account
        e = JournalEntry.objects.create(date=datetime.date.today(), reference='WRITE-OFF', description='إعدام دين')
        JournalItem.objects.create(entry=e, account=_get_system_account(PAYMENT_FEES_CODE), debit=D('25'), credit=0)
        JournalItem.objects.create(entry=e, account=rec, debit=0, credit=D('25'))
        e.post()
        p = self.fresh(c)
        self.assertEqual(p.customer_balance, D('75'))                      # the ledger, like the trial balance
        self.assertEqual(p.document_balances()[0], D('100'))               # the documents alone miss it
        row = next(t for t in get_partner_full_statement(p)['transactions'] if t['reference'] == 'WRITE-OFF')
        self.assertEqual((row['doc_type'], row['credit']), ('قيد يومية', D('25')))

    def test_statement_pages_render(self):
        from django.urls import reverse
        from apps.tenant.core.tests.factories import login
        c = make_partner('عميل صفحة', 'CUSTOMER')
        make_invoice(self.w, lines=[(self.w.product, 1, 10)], payment_type='CREDIT', partner=c)
        login(self.client, self.w.admin)
        for params in ({}, {'show_items': '1'}, {'start_date': datetime.date.today().isoformat()}):
            r = self.client.get(reverse('partners:partner_ledger', args=[c.pk]), params)
            self.assertEqual(r.status_code, 200)
        r = self.client.get(reverse('partners:partner_ledger_export_excel', args=[c.pk]))
        self.assertEqual(r.status_code, 200)
