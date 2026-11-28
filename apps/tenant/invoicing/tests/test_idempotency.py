"""Tests for P2-2: Idempotency keys and duplicate-submission protection."""
import datetime
import json
import uuid
from decimal import Decimal as D
from unittest.mock import patch

from django.db import IntegrityError
from django.db.models.query import QuerySet
from django.test import TestCase
from django.urls import reverse

from apps.tenant.accounting.models import InternalTransfer, Voucher
from apps.tenant.core.tests.factories import (
    account, assertInventoryMatchesLedger, build_world, login, make_invoice,
    make_treasury, make_user, stock_in,
)
from apps.tenant.inventory.models import StockMovement
from apps.tenant.invoicing import van_services as van
from apps.tenant.invoicing.models import Invoice, POSSession, SalesRep


class IdempotencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_pos_complete_sale_duplicate_payload_creates_single_invoice_and_single_stock_movement(self):
        make_invoice(self.w, invoice_type='PURCHASE', lines=[(self.w.product, 10, D('5'))], payment_type='CREDIT')
        POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        login(self.client, self.w.admin)

        idem_key = str(uuid.uuid4())
        payload = {
            'idempotency_key': idem_key,
            'cart': [{'product_id': self.w.product.id, 'quantity': 2, 'tax_rate': 14}],
            'payment_type': 'CASH',
            'warehouse_id': self.w.warehouse.id,
            'branch_id': self.w.branch.id,
            'treasury_id': self.w.treasury.id,
            'invoice_type': 'SALE',
            'discount_percentage': 0,
        }
        url = reverse('invoicing:complete_sale')

        r1 = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(r1.status_code, 200, r1.content)
        d1 = r1.json()
        self.assertTrue(d1['success'], d1)

        r2 = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(r2.status_code, 200, r2.content)
        d2 = r2.json()
        self.assertTrue(d2['success'], d2)

        self.assertEqual(d1['invoice_id'], d2['invoice_id'])
        self.assertEqual(d1['invoice_number'], d2['invoice_number'])
        self.assertEqual(Invoice.objects.filter(invoice_type=Invoice.SALE).count(), 1)
        self.assertEqual(
            StockMovement.objects.filter(product=self.w.product, movement_type='OUT').count(),
            1,
        )
        self.assertEqual(self.w.product.get_stock(self.w.warehouse), D('8'))
        assertInventoryMatchesLedger(self, warehouse=self.w.warehouse)

    def test_complete_sale_handles_race_integrity_error(self):
        stock_in(self.w.product, self.w.warehouse, 10, D('5'))
        POSSession.objects.create(branch=self.w.branch, treasury=self.w.treasury, user=self.w.admin)
        existing = make_invoice(self.w, lines=[(self.w.product, 1, D('10'))])
        idem_key = uuid.uuid4()
        Invoice.objects.filter(pk=existing.pk).update(idempotency_key=idem_key)

        login(self.client, self.w.admin)
        payload = {
            'idempotency_key': str(idem_key),
            'cart': [{'product_id': self.w.product.id, 'quantity': 2, 'tax_rate': 0}],
            'payment_type': 'CASH',
            'warehouse_id': self.w.warehouse.id,
            'branch_id': self.w.branch.id,
            'treasury_id': self.w.treasury.id,
            'invoice_type': 'SALE',
            'discount_percentage': 0,
        }

        orig_filter = QuerySet.filter
        seen_idem_lookups = {'count': 0}

        def racy_filter(qs_self, *args, **kwargs):
            if qs_self.model is Invoice and 'idempotency_key' in kwargs:
                seen_idem_lookups['count'] += 1
                if seen_idem_lookups['count'] == 1:
                    return qs_self.none()
            return orig_filter(qs_self, *args, **kwargs)

        with patch.object(QuerySet, 'filter', autospec=True, side_effect=racy_filter):
            resp = self.client.post(
                reverse('invoicing:complete_sale'),
                data=json.dumps(payload),
                content_type='application/json',
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data['success'], data)
        self.assertEqual(data['invoice_id'], existing.id)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_rep_sale_and_rep_collect_idempotency(self):
        rep_user = make_user('idem_rep', role='SALES_REP', branch=self.w.branch, perms=('invoicing.use_rep_app',))
        rep = SalesRep.objects.create(employee=rep_user.employee_profile, source_warehouse=self.w.warehouse)
        van.ensure_rep_setup(rep, create_van=True, create_treasury=True)
        rep.refresh_from_db()
        stock_in(self.w.product, rep.van_warehouse, 10, D('5'))

        login(self.client, rep_user)
        sale_key = str(uuid.uuid4())
        sale_payload = {
            'mode': 'sale',
            'payment': 'CASH',
            'idempotency_key': sale_key,
            'cart': [{'product_id': self.w.product.id, 'qty': '3'}],
        }
        r1 = self.client.post(
            reverse('invoicing:rep_app_sell'),
            data=json.dumps(sale_payload),
            content_type='application/json',
        )
        r2 = self.client.post(
            reverse('invoicing:rep_app_sell'),
            data=json.dumps(sale_payload),
            content_type='application/json',
        )
        self.assertEqual(r1.status_code, 200, r1.content)
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertEqual(r1.json()['invoice_id'], r2.json()['invoice_id'])
        self.assertEqual(Invoice.objects.filter(sales_rep=rep).count(), 1)
        self.assertEqual(self.w.product.get_stock(rep.van_warehouse), D('7'))

        collect_key = str(uuid.uuid4())
        v1 = van.rep_collect(rep, rep_user, self.w.customer, '50', idempotency_key=collect_key)
        v2 = van.rep_collect(rep, rep_user, self.w.customer, '50', idempotency_key=collect_key)
        self.assertEqual(v1.pk, v2.pk)
        self.assertEqual(Voucher.objects.filter(treasury=rep.treasury).count(), 1)

    def test_unique_constraint_on_invoice_voucher_and_internal_transfer(self):
        # Multiple NULL keys are allowed
        today = datetime.date.today()
        Invoice.objects.create(
            invoice_type=Invoice.SALE, branch=self.w.branch, warehouse=self.w.warehouse,
            partner=self.w.customer, payment_type=Invoice.CASH, treasury=self.w.treasury,
            date=today, idempotency_key=None,
        )
        Invoice.objects.create(
            invoice_type=Invoice.SALE, branch=self.w.branch, warehouse=self.w.warehouse,
            partner=self.w.customer, payment_type=Invoice.CASH, treasury=self.w.treasury,
            date=today, idempotency_key=None,
        )

        # Duplicate non-null UUID on Invoice must raise IntegrityError
        inv_key = uuid.uuid4()
        Invoice.objects.create(
            invoice_type=Invoice.SALE, branch=self.w.branch, warehouse=self.w.warehouse,
            partner=self.w.customer, payment_type=Invoice.CASH, treasury=self.w.treasury,
            date=today, idempotency_key=inv_key,
        )
        with self.assertRaises(IntegrityError):
            Invoice.objects.create(
                invoice_type=Invoice.SALE, branch=self.w.branch, warehouse=self.w.warehouse,
                partner=self.w.customer, payment_type=Invoice.CASH, treasury=self.w.treasury,
                date=today, idempotency_key=inv_key,
            )

        # Duplicate non-null UUID on Voucher must raise IntegrityError, while NULL is allowed twice
        Voucher.objects.create(
            voucher_type=Voucher.RECEIPT, amount=D('10'), account=account('4200'),
            treasury=self.w.treasury, idempotency_key=None,
        )
        Voucher.objects.create(
            voucher_type=Voucher.RECEIPT, amount=D('20'), account=account('4200'),
            treasury=self.w.treasury, idempotency_key=None,
        )
        v_key = uuid.uuid4()
        Voucher.objects.create(
            voucher_type=Voucher.RECEIPT, amount=D('30'), account=account('4200'),
            treasury=self.w.treasury, idempotency_key=v_key,
        )
        with self.assertRaises(IntegrityError):
            Voucher.objects.create(
                voucher_type=Voucher.RECEIPT, amount=D('30'), account=account('4200'),
                treasury=self.w.treasury, idempotency_key=v_key,
            )

        # Duplicate non-null UUID on InternalTransfer must raise IntegrityError, while NULL is allowed twice
        t2 = make_treasury(self.w.branch, 'خزينة ثانية')
        InternalTransfer.objects.create(
            amount=D('10'), from_treasury=self.w.treasury, to_treasury=t2, idempotency_key=None,
        )
        InternalTransfer.objects.create(
            amount=D('20'), from_treasury=self.w.treasury, to_treasury=t2, idempotency_key=None,
        )
        tr_key = uuid.uuid4()
        InternalTransfer.objects.create(
            amount=D('30'), from_treasury=self.w.treasury, to_treasury=t2, idempotency_key=tr_key,
        )
        with self.assertRaises(IntegrityError):
            InternalTransfer.objects.create(
                amount=D('30'), from_treasury=self.w.treasury, to_treasury=t2, idempotency_key=tr_key,
            )
