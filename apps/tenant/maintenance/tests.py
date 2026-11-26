"""Maintenance tickets: status edits and spare-part stock."""
import unittest
from decimal import Decimal as D

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login, stock_in
from apps.tenant.maintenance.models import MaintenanceTicket, TicketPart


class MaintenanceTicketTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 10, '3')
        self.ticket = MaintenanceTicket.objects.create(
            branch=self.w.branch, customer=self.w.customer, device_model='Phone X', issue_description='screen',
        )
        login(self.client, self.w.admin)

    def test_status_edit_to_delivered_refused(self):
        resp = self.client.post(reverse('maintenance:ticket_edit', args=[self.ticket.pk]),
                                {'field': 'status', 'value': MaintenanceTicket.STATUS_DELIVERED})
        self.assertEqual(resp.status_code, 400)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, MaintenanceTicket.STATUS_PENDING)
        self.assertIsNone(self.ticket.journal_entry_id)

    def test_status_edit_to_open_status_allowed(self):
        resp = self.client.post(reverse('maintenance:ticket_edit', args=[self.ticket.pk]),
                                {'field': 'status', 'value': MaintenanceTicket.STATUS_IN_PROGRESS})
        self.assertEqual(resp.status_code, 200)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, MaintenanceTicket.STATUS_IN_PROGRESS)

    def test_part_deducts_and_delete_part_restores_stock(self):
        part = TicketPart.objects.create(ticket=self.ticket, product=self.w.product, warehouse=self.w.warehouse,
                                         quantity=D('2'), selling_price=D('20'))
        self.assertEqual(part.actual_cost, D('6'))
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('8'))
        self.w.product.refresh_from_db()
        last_price = self.w.product.last_purchase_price

        resp = self.client.post(reverse('maintenance:delete_part', args=[part.pk]))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(TicketPart.objects.filter(pk=part.pk).exists())
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('10'))
        self.w.product.refresh_from_db()
        self.assertEqual(self.w.product.last_purchase_price, last_price)

    # Regression: add_part() used an undefined `branch` name (NameError → HTTP 400).
    def test_add_part_view_deducts_stock(self):
        resp = self.client.post(reverse('maintenance:add_part', args=[self.ticket.pk]), {
            'product_id': self.w.product.id, 'warehouse_id': self.w.warehouse.id,
            'quantity': '3', 'selling_price': '15',
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('7'))

    def test_add_part_negative_quantity_rejected(self):
        resp = self.client.post(reverse('maintenance:add_part', args=[self.ticket.pk]), {
            'product_id': self.w.product.id, 'warehouse_id': self.w.warehouse.id,
            'quantity': '-1', 'selling_price': '15',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('10'))


class MaintenanceLifecycleTests(TestCase):
    """Delivery entry is POSTED; cancelling puts parts back; a closed ticket's parts are frozen; cash counts
    in the cashier's shift."""
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        stock_in(self.w.product, self.w.warehouse, 10, '3')
        self.ticket = MaintenanceTicket.objects.create(
            branch=self.w.branch, customer=self.w.customer, device_model='Phone Y', issue_description='battery',
            labor_cost=D('50'))
        TicketPart.objects.create(ticket=self.ticket, product=self.w.product, warehouse=self.w.warehouse,
                                  quantity=D('2'), selling_price=D('20'))

    def test_delivery_entry_is_posted_and_parts_frozen(self):
        from apps.tenant.accounting.models import JournalEntry
        self.ticket.deliver(self.w.admin, treasury=self.w.treasury)
        self.ticket.refresh_from_db()
        je = self.ticket.journal_entry
        self.assertEqual(je.status, JournalEntry.POSTED)
        self.assertEqual(je.get_totals(), (D('96'), D('96')))           # 50 + 40 collected, + 6 cost
        part = self.ticket.parts.get()
        with self.assertRaises(ValueError):
            part.delete()
        with self.assertRaises(ValueError):
            TicketPart.objects.create(ticket=self.ticket, product=self.w.product, warehouse=self.w.warehouse,
                                      quantity=D('1'), selling_price=D('1'))
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('8'))

    def test_cancel_returns_parts_and_blocks_delivery(self):
        with self.assertRaises(ValueError):
            self.ticket.cancel(self.w.admin, '')                       # a reason is required
        self.ticket.cancel(self.w.admin, 'العميل رفض التكلفة')
        self.assertEqual(self.ticket.status, MaintenanceTicket.STATUS_CANCELLED)
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('10'))
        self.assertFalse(self.ticket.parts.exists())
        self.assertIsNone(self.ticket.journal_entry_id)
        with self.assertRaises(ValueError):
            self.ticket.deliver(self.w.admin, treasury=self.w.treasury)

    def test_free_warranty_ticket_books_cost_only(self):
        MaintenanceTicket.objects.filter(pk=self.ticket.pk).update(labor_cost=0)
        self.ticket.parts.update(selling_price=0)
        self.ticket.refresh_from_db()
        self.ticket.deliver(self.w.admin, treasury=self.w.treasury)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.journal_entry.get_totals(), (D('6'), D('6')))   # Dr parts cost / Cr stock

    def test_cash_delivery_counts_in_the_shift(self):
        from apps.tenant.invoicing.models import POSSession
        from apps.tenant.invoicing.shift_services import shift_cash_summary
        session = POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        login(self.client, self.w.admin)
        resp = self.client.post(reverse('maintenance:deliver_ticket', args=[self.ticket.pk]),
                                {'payment_method': 'CASH', 'treasury_id': self.w.treasury.id})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.pos_session_id, session.pk)
        self.assertEqual(shift_cash_summary(session)['total_cash_maintenance'], D('90'))
