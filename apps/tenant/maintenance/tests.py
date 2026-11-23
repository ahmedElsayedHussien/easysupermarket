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
