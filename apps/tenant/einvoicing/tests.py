"""ETA e-invoicing: the document follows the Tax Authority arithmetic and rules."""
from decimal import Decimal as D
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.models import SystemSetting
from apps.tenant.core.tests.factories import build_world, login, make_invoice, make_partner, stock_in
from apps.tenant.einvoicing.models import EInvoiceLog, TaxIntegrationSettings
from apps.tenant.einvoicing.services import ETAValidationError, build_invoice_json, refresh_status
from apps.tenant.invoicing.models import Invoice, InvoiceLine


def r5(x):
    return round(float(x), 5)


class ETATestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        s = SystemSetting.get_settings()
        s.store_name = 'سوبر ماركت النور'
        s.save()
        cls.w.product.egs_code = 'EG-123456789-RICE1KG'
        cls.w.product.save()
        cls.w.product2.egs_code = 'EG-123456789-RICE5'
        cls.w.product2.unit = 'KG'
        cls.w.product2.save()

    def setUp(self):
        from apps.tenant.einvoicing import services
        services._TOKEN_CACHE.clear()
        TaxIntegrationSettings.objects.update_or_create(id=1, defaults=dict(
            client_id='cid', client_secret='sec', company_id='123456789', taxpayer_activity_code='4711',
            enable_einvoicing=True, governate='القاهرة', region_city='المعادي', street='شارع 9',
            building_number='12', branch_code='0'))
        stock_in(self.w.product, self.w.warehouse, 100, '4')
        stock_in(self.w.product2, self.w.warehouse, 100, '30')

    def check_identities(self, doc):
        """Every ETA reconciliation rule, line by line and on the totals."""
        sums = {'sales': 0, 'disc': 0, 'net': 0, 'total': 0}
        for ln in doc['invoiceLines']:
            self.assertAlmostEqual(ln['salesTotal'], r5(ln['quantity'] * ln['unitValue']['amountEGP']), places=4)
            self.assertAlmostEqual(ln['netTotal'], r5(ln['salesTotal'] - ln['discount']['amount']), places=4)
            taxes = sum(t['amount'] * (-1 if t['taxType'] == 'T4' else 1) for t in ln['taxableItems'])
            self.assertAlmostEqual(ln['total'], r5(ln['netTotal'] + taxes + ln['totalTaxableFees'] - ln['itemsDiscount']),
                                   places=4)
            self.assertEqual(ln['itemsDiscount'], 0)
            sums['sales'] += ln['salesTotal']; sums['disc'] += ln['discount']['amount']
            sums['net'] += ln['netTotal']; sums['total'] += ln['total']
        self.assertAlmostEqual(doc['totalSalesAmount'], sums['sales'], places=4)
        self.assertAlmostEqual(doc['totalDiscountAmount'], sums['disc'], places=4)
        self.assertAlmostEqual(doc['netAmount'], sums['net'], places=4)
        self.assertAlmostEqual(doc['totalAmount'], sums['total'] - doc['extraDiscountAmount'], places=4)


class DocumentTests(ETATestBase):
    def test_issuer_from_settings_and_real_address(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 2, 10, 14)])
        doc = build_invoice_json(inv.id)
        self.assertEqual(doc['issuer']['name'], 'سوبر ماركت النور')        # from general settings
        self.assertEqual(doc['issuer']['address']['street'], 'شارع 9')
        self.assertEqual(doc['issuer']['address']['regionCity'], 'المعادي')
        self.assertEqual(doc['documentType'], 'I')
        self.assertEqual(doc['internalID'], inv.invoice_number)
        self.assertEqual(doc['taxpayerActivityCode'], '4711')
        self.assertTrue(doc['dateTimeIssued'].endswith('Z'))
        self.check_identities(doc)

    def test_codes_units_and_tax_subtypes(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 2, 10, 14), (self.w.product2, 3, 50, 0)])
        doc = build_invoice_json(inv.id)
        l1, l2 = doc['invoiceLines']
        self.assertEqual((l1['itemType'], l1['itemCode'], l1['unitType']), ('EGS', 'EG-123456789-RICE1KG', 'EA'))
        self.assertEqual((l2['itemType'], l2['itemCode'], l2['unitType']), ('EGS', 'EG-123456789-RICE5', 'KGM'))
        self.assertEqual(l1['taxableItems'][0]['subType'], 'V009')
        self.assertEqual(l2['taxableItems'][0]['subType'], 'V003')   # 0% = exempt
        self.check_identities(doc)

    def test_line_discount_not_double_counted(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 4, 10, 14)], confirm=False)
        line = inv.lines.first()
        line.discount_pct = D('10')
        line.save()
        inv.recalculate_totals()
        doc = build_invoice_json(inv.id)
        ln = doc['invoiceLines'][0]
        self.assertAlmostEqual(ln['discount']['amount'], 4.0, places=4)
        self.assertAlmostEqual(ln['netTotal'], 36.0, places=4)
        self.check_identities(doc)

    def test_withholding_tax_t4_reconciles(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 10, 10, 14)], confirm=False)
        line = inv.lines.first()
        line.wht_rate = D('1')
        line.save()
        inv.recalculate_totals()
        inv.refresh_from_db()
        self.assertGreater(inv.wht_amount, 0)
        doc = build_invoice_json(inv.id)
        types = {t['taxType']: t['amount'] for t in doc['taxTotals']}
        self.assertIn('T4', types)
        self.check_identities(doc)
        self.assertAlmostEqual(doc['totalAmount'], float(inv.total_amount), places=2)

    def test_missing_item_code_blocks_sending(self):
        self.w.product.egs_code = ''
        self.w.product.save()
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        with self.assertRaises(ETAValidationError) as cm:
            build_invoice_json(inv.id)
        self.assertTrue(any('ليس له كود EGS' in e for e in cm.exception.errors))

    def test_incomplete_settings_listed(self):
        TaxIntegrationSettings.objects.filter(id=1).update(street='', taxpayer_activity_code='47')
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        with self.assertRaises(ETAValidationError) as cm:
            build_invoice_json(inv.id)
        text = ' '.join(cm.exception.errors)
        self.assertIn('الشارع', text)
        self.assertIn('كود النشاط', text)

    def test_individual_over_threshold_needs_national_id(self):
        person = make_partner('أحمد علي', 'CUSTOMER')
        TaxIntegrationSettings.objects.filter(id=1).update(receiver_id_threshold=D('100'))
        inv = make_invoice(self.w, lines=[(self.w.product, 20, 10, 14)], partner=person)
        with self.assertRaises(ETAValidationError):
            build_invoice_json(inv.id)
        person.national_id = '29001011234567'
        person.save()
        doc = build_invoice_json(inv.id)
        self.assertEqual(doc['receiver'], {'type': 'P', 'name': 'أحمد علي', 'address': {'country': 'EG'},
                                           'id': '29001011234567'})

    def test_business_receiver(self):
        company = make_partner('شركة الأمل', 'CUSTOMER', tax_id='987654321', address='شارع التحرير')
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)], partner=company)
        with self.assertRaises(ETAValidationError):                 # a company needs its own address
            build_invoice_json(inv.id)
        company.eta_governate, company.eta_region_city, company.eta_building_number = 'الجيزة', 'الدقي', '12'
        company.save()
        rec = build_invoice_json(inv.id)['receiver']
        self.assertEqual((rec['type'], rec['id']), ('B', '987654321'))
        self.assertEqual(rec['address'], {'country': 'EG', 'governate': 'الجيزة', 'regionCity': 'الدقي',
                                          'street': 'شارع التحرير', 'buildingNumber': '12'})


class LifecycleTests(ETATestBase):
    def test_return_becomes_credit_note_referencing_original(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 3, 10, 14)])
        log = EInvoiceLog.objects.get(invoice=sale)
        log.status, log.uuid = 'VALID', 'UUID-ORIGINAL'
        log.save()
        login(self.client, self.w.admin)
        import json
        data = self.client.post(reverse('invoicing:process_invoice_return', args=[sale.id]),
                                data=json.dumps({'items': [{'line_id': sale.lines.first().id, 'quantity': '1'}],
                                                 'payment_type': 'CASH', 'treasury_id': self.w.treasury.id}),
                                content_type='application/json').json()
        ret = Invoice.objects.get(pk=data['invoice_id'])
        self.assertTrue(EInvoiceLog.objects.filter(invoice=ret).exists())
        doc = build_invoice_json(ret.id)
        self.assertEqual(doc['documentType'], 'C')
        self.assertEqual(doc['references'], ['UUID-ORIGINAL'])
        self.check_identities(doc)

    def test_credit_note_waits_for_original_acceptance(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 3, 10, 14)])
        ret = make_invoice(self.w, 'RETURN_SALE', lines=[(self.w.product, 1, 10, 14)], parent_invoice=sale)
        with self.assertRaises(ETAValidationError) as cm:
            build_invoice_json(ret.id)
        self.assertIn('الفاتورة الأصلية', ' '.join(cm.exception.errors))

    def test_cancel_drops_unsent_and_flags_sent(self):
        from apps.tenant.services.invoice_service import cancel_invoice
        a = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        b = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        EInvoiceLog.objects.filter(invoice=b).update(status='VALID', uuid='U-B')
        cancel_invoice(a.id, user=self.w.admin)
        cancel_invoice(b.id, user=self.w.admin)
        self.assertFalse(EInvoiceLog.objects.filter(invoice=a).exists())
        self.assertEqual(EInvoiceLog.objects.get(invoice=b).status, 'CANCEL_PENDING')

    def test_approve_refuses_incomplete_document(self):
        self.w.product.egs_code = ''
        self.w.product.save()
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        log = EInvoiceLog.objects.get(invoice=inv)
        login(self.client, self.w.admin)
        r = self.client.post(reverse('einvoicing:approve_invoice', args=[log.id]), follow=True)
        log.refresh_from_db()
        self.assertEqual(log.status, 'WAITING_APPROVAL')
        self.assertContains(r, 'ليس له كود EGS')

    def test_refresh_status_maps_eta_result(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        log = EInvoiceLog.objects.get(invoice=inv)
        log.status, log.uuid = 'SUBMITTED', 'U-1'
        log.save()
        token = mock.Mock(status_code=200, json=lambda: {'access_token': 't'})
        details = mock.Mock(status_code=200, json=lambda: {'status': 'Invalid', 'validationResults': {'x': 1}})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=token), \
                mock.patch('apps.tenant.einvoicing.services.requests.get', return_value=details):
            refresh_status(log)
        log.refresh_from_db()
        self.assertEqual(log.status, 'INVALID')

    def test_settings_page_shows_store_name_and_missing_items(self):
        TaxIntegrationSettings.objects.filter(id=1).update(building_number='')
        login(self.client, self.w.admin)
        r = self.client.get(reverse('einvoicing:tax_settings'))
        self.assertContains(r, 'سوبر ماركت النور')
        self.assertContains(r, 'رقم المبنى')


class SubmissionResponseTests(ETATestBase):
    """Response shape from the official 'Submit Documents' API page."""

    def _signed_log(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        log = EInvoiceLog.objects.get(invoice=inv)
        log.status, log.signed_payload = 'SIGNED', {'doc': 1}
        log.save()
        login(self.client, self.w.admin)
        return log

    def test_accepted_document_stores_submission_uuid_and_long_id(self):
        log = self._signed_log()
        resp = mock.Mock(status_code=202, json=lambda: {
            'submissionUUID': 'TZRKK8MFZCPSTW9XCYWBMKME10',
            'acceptedDocuments': [{'uuid': 'TZRKK8MFZCPSTW9XCYWBMKME11', 'longId': 'LONG123',
                                   'internalId': log.invoice.invoice_number}],
            'rejectedDocuments': []})
        with mock.patch('apps.tenant.einvoicing.views.submit_to_eta', return_value=resp), \
                mock.patch('apps.tenant.einvoicing.services.refresh_status', side_effect=Exception('offline')):
            self.client.post(reverse('einvoicing:confirm_eta_submission', args=[log.id]))
        log.refresh_from_db()
        self.assertEqual(log.submission_id, 'TZRKK8MFZCPSTW9XCYWBMKME10')
        self.assertEqual(log.uuid, 'TZRKK8MFZCPSTW9XCYWBMKME11')
        self.assertEqual(log.long_id, 'LONG123')
        self.assertEqual(log.status, 'SUBMITTED')     # final Valid/Invalid comes from the validation

    def test_rejected_document(self):
        log = self._signed_log()
        resp = mock.Mock(status_code=202, json=lambda: {
            'submissionUUID': 'S1', 'acceptedDocuments': [],
            'rejectedDocuments': [{'internalId': 'X', 'error': {'code': 'BadArgument', 'message': 'bad'}}]})
        with mock.patch('apps.tenant.einvoicing.views.submit_to_eta', return_value=resp):
            self.client.post(reverse('einvoicing:confirm_eta_submission', args=[log.id]))
        log.refresh_from_db()
        self.assertEqual(log.status, 'INVALID')

    def test_duplicate_submission_keeps_document_signed(self):
        log = self._signed_log()
        resp = mock.Mock(status_code=422, headers={'Retry-After': '120'}, json=lambda: {})
        with mock.patch('apps.tenant.einvoicing.views.submit_to_eta', return_value=resp):
            r = self.client.post(reverse('einvoicing:confirm_eta_submission', args=[log.id]), follow=True)
        log.refresh_from_db()
        self.assertEqual(log.status, 'SIGNED')
        self.assertContains(r, 'مكرر')


class CancelDocumentTests(ETATestBase):
    """Behaviour from the official 'Cancel Document' API page."""

    def _log(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        log = EInvoiceLog.objects.get(invoice=inv)
        log.status, log.uuid = 'CANCEL_PENDING', 'U-CANCEL'
        log.save()
        return log

    def _put(self, log, put_resp, reason='Wrong buyer details'):
        from apps.tenant.einvoicing.services import cancel_on_eta
        token = mock.Mock(status_code=200, json=lambda: {'access_token': 't'})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=token), \
                mock.patch('apps.tenant.einvoicing.services.requests.put', return_value=put_resp) as put:
            cancel_on_eta(log, reason)
        return put

    def test_request_body_and_success(self):
        log = self._log()
        put = self._put(log, mock.Mock(status_code=200))
        url = put.call_args[0][0]
        self.assertTrue(url.endswith('/api/v1.0/documents/state/U-CANCEL/state'))
        self.assertEqual(put.call_args[1]['json'], {'status': 'cancelled', 'reason': 'Wrong buyer details'})
        log.refresh_from_db()
        self.assertEqual(log.status, 'CANCEL_REQUESTED')

    def test_period_over_explains_credit_note(self):
        log = self._log()
        resp = mock.Mock(status_code=400, text='', json=lambda: {'error': {'code': 'OperationPeriodOver'}})
        with self.assertRaises(Exception) as cm:
            self._put(log, resp)
        self.assertIn('إشعار دائن', str(cm.exception))
        log.refresh_from_db()
        self.assertEqual(log.status, 'CANCEL_PENDING')

    def test_referencing_documents_message(self):
        log = self._log()
        resp = mock.Mock(status_code=400, text='', json=lambda: {'error': {'code': 'ActiveReferencingDocuments'}})
        with self.assertRaises(Exception) as cm:
            self._put(log, resp)
        self.assertIn('ألغِ المستند المرتبط أولاً', str(cm.exception))


class SearchSyncTests(ETATestBase):
    """Behaviour from the official 'Search Documents' API page."""

    def _token(self):
        return mock.Mock(status_code=200, json=lambda: {'access_token': 't'})

    def test_paginates_and_updates_log(self):
        import datetime
        from apps.tenant.einvoicing.services import sync_with_eta
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        log = EInvoiceLog.objects.get(invoice=inv)
        log.status, log.uuid = 'SUBMITTED', 'U1'
        log.save()
        page1 = mock.Mock(status_code=200, json=lambda: {
            'result': [{'uuid': 'U1', 'internalId': inv.invoice_number, 'status': 'Valid', 'longId': 'L1',
                        'submissionUUID': 'S1', 'publicUrl': 'https://eta.test/U1/share/L1'}],
            'metadata': {'continuationToken': 'tok-2'}})
        page2 = mock.Mock(status_code=200, json=lambda: {
            'result': [{'uuid': 'X9', 'internalId': 'SALE-OTHER-1', 'status': 'Valid'}],
            'metadata': {'continuationToken': 'EndofResultSet'}})
        sleeps = []
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.get', side_effect=[page1, page2]) as get:
            s = sync_with_eta(datetime.date(2026, 11, 1), datetime.date(2026, 11, 7), sleep=sleeps.append)
        first = dict(get.call_args_list[0][1]['params'])
        second = dict(get.call_args_list[1][1]['params'])
        self.assertEqual(first['direction'], 'Sent')
        self.assertEqual(first['continuationToken'], '')
        self.assertEqual(second['continuationToken'], 'tok-2')
        self.assertEqual(first['submissionDateFrom'], second['submissionDateFrom'])
        self.assertEqual(sleeps, [2])
        self.assertEqual((s['checked'], s['updated'], s['unknown']), (2, 1, ['SALE-OTHER-1']))
        log.refresh_from_db()
        self.assertEqual((log.status, log.long_id, log.public_url), ('VALID', 'L1', 'https://eta.test/U1/share/L1'))

    def test_period_limit_30_days(self):
        import datetime
        from apps.tenant.einvoicing.services import sync_with_eta
        with self.assertRaises(ValueError):
            sync_with_eta(datetime.date(2026, 10, 1), datetime.date(2026, 11, 15))

    def test_cancel_request_pending_at_receiver(self):
        import datetime
        from apps.tenant.einvoicing.services import sync_with_eta
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        EInvoiceLog.objects.filter(invoice=inv).update(status='VALID', uuid='U2')
        page = mock.Mock(status_code=200, json=lambda: {
            'result': [{'uuid': 'U2', 'status': 'Valid', 'cancelRequestDate': '2026-11-20T10:00:00Z'}],
            'metadata': {'continuationToken': 'EndofResultSet'}})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.get', return_value=page):
            sync_with_eta(datetime.date(2026, 11, 18), datetime.date(2026, 11, 24))
        self.assertEqual(EInvoiceLog.objects.get(invoice=inv).status, 'CANCEL_REQUESTED')

    def test_history_page_has_sync_form(self):
        login(self.client, self.w.admin)
        r = self.client.get(reverse('einvoicing:invoice_history'))
        self.assertContains(r, 'مطابقة مع الضرائب')


class DocumentDetailsTests(ETATestBase):
    """Behaviour from the official 'Get Document Details' API page."""

    def _refresh(self, payload, status='SUBMITTED'):
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        log = EInvoiceLog.objects.get(invoice=inv)
        log.status, log.uuid = status, 'U-D'
        log.save()
        token = mock.Mock(status_code=200, json=lambda: {'access_token': 't'})
        resp = mock.Mock(status_code=200, json=lambda: payload)
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=token), \
                mock.patch('apps.tenant.einvoicing.services.requests.get', return_value=resp) as get:
            refresh_status(log)
        self.assertTrue(get.call_args[0][0].endswith('/api/v1.0/documents/U-D/details'))
        log.refresh_from_db()
        return log

    def test_invalid_lists_readable_reasons(self):
        log = self._refresh({
            'status': 'invalid', 'submissionUUID': 'S-9', 'longId': 'LONG-9',
            'validationResults': {'status': 'Invalid', 'validationSteps': [
                {'name': 'Step01-Structure validator', 'status': 'Valid'},
                {'name': 'GS1 code validator', 'status': 'Invalid',
                 'error': {'code': 'X', 'message': 'Invalid item code', 'target': 'itemCode',
                           'details': [{'message': 'Code not registered'}]}},
            ]}})
        self.assertEqual(log.status, 'INVALID')
        self.assertEqual((log.submission_id, log.long_id), ('S-9', 'LONG-9'))
        self.assertEqual(log.eta_response['errors'],
                         ['GS1 code validator: Invalid item code (itemCode)', 'GS1 code validator: Code not registered'])
        login(self.client, self.w.admin)
        self.assertContains(self.client.get(reverse('einvoicing:invoice_history')), 'Code not registered')

    def test_cancellation_requested_then_declined(self):
        log = self._refresh({'status': 'valid', 'cancelRequestDate': '2026-11-20T10:00:00Z'}, status='VALID')
        self.assertEqual(log.status, 'CANCEL_REQUESTED')
        log = self._refresh({'status': 'valid', 'cancelRequestDate': '2026-11-20T10:00:00Z',
                             'declineCancelRequestDate': '2026-11-21T10:00:00Z'}, status='CANCEL_REQUESTED')
        self.assertEqual(log.status, 'CANCEL_DECLINED')

    def test_cancelled_after_acceptance(self):
        log = self._refresh({'status': 'cancelled'}, status='CANCEL_REQUESTED')
        self.assertEqual(log.status, 'CANCELLED')


class RejectionRequestTests(ETATestBase):
    """Our invoice rejected by its receiver (official 'Reject Document' API page)."""

    def test_states(self):
        from apps.tenant.einvoicing.services import eta_state
        self.assertEqual(eta_state({'status': 'valid', 'rejectRequestDate': '2026-11-20T10:00:00Z'}, 'VALID'),
                         'REJECT_REQUESTED')
        self.assertEqual(eta_state({'status': 'valid', 'rejectRequestDate': 'x', 'declineRejectRequestDate': 'y'},
                                   'VALID'), 'VALID')
        self.assertEqual(eta_state({'status': 'rejected'}, 'REJECT_REQUESTED'), 'REJECTED')


class LoginTests(ETATestBase):
    """Behaviour from the official 'Login as Taxpayer System' API page."""

    def _settings(self):
        return TaxIntegrationSettings.objects.get(id=1)

    def test_basic_auth_and_token_reuse(self):
        from apps.tenant.einvoicing.services import get_eta_access_token
        ok = mock.Mock(status_code=200, json=lambda: {'access_token': 'T1', 'expires_in': 3600})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=ok) as post:
            self.assertEqual(get_eta_access_token(self._settings()), 'T1')
            self.assertEqual(get_eta_access_token(self._settings()), 'T1')     # cached, no 2nd login
        self.assertEqual(post.call_count, 1)
        kwargs = post.call_args[1]
        self.assertEqual(kwargs['auth'], ('cid', 'sec'))                       # Basic (RFC 2617)
        self.assertEqual(kwargs['data'], {'grant_type': 'client_credentials', 'scope': 'InvoicingAPI'})
        self.assertNotIn('onbehalfof', kwargs['headers'])

    def test_intermediary_header(self):
        from apps.tenant.einvoicing.services import get_eta_access_token
        TaxIntegrationSettings.objects.filter(id=1).update(on_behalf_of='100015840')
        ok = mock.Mock(status_code=200, json=lambda: {'access_token': 'T2', 'expires_in': 3600})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=ok) as post:
            get_eta_access_token(self._settings())
        self.assertEqual(post.call_args[1]['headers']['onbehalfof'], '100015840')
        self.assertEqual(post.call_args[1]['headers']['Accept-Language'], 'ar')

    def test_invalid_client_message(self):
        from apps.tenant.einvoicing.services import get_eta_access_token
        bad = mock.Mock(status_code=400, json=lambda: {'error': 'invalid_client'})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=bad):
            with self.assertRaises(Exception) as cm:
                get_eta_access_token(self._settings())
        self.assertIn('Client Secret', str(cm.exception))

    def test_expired_token_renewed_once_on_401(self):
        from apps.tenant.einvoicing.services import eta_request
        tokens = iter([mock.Mock(status_code=200, json=lambda: {'access_token': 'OLD', 'expires_in': 3600}),
                       mock.Mock(status_code=200, json=lambda: {'access_token': 'NEW', 'expires_in': 3600})])
        calls = []

        def fake_get(url, headers=None, **kw):
            calls.append(headers['Authorization'])
            return mock.Mock(status_code=401 if len(calls) == 1 else 200)
        with mock.patch('apps.tenant.einvoicing.services.requests.post', side_effect=lambda *a, **k: next(tokens)), \
                mock.patch('apps.tenant.einvoicing.services.requests.get', side_effect=fake_get):
            resp = eta_request('get', self._settings(), 'https://x/api')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls, ['Bearer OLD', 'Bearer NEW'])


class ReceivedDocumentsTests(ETATestBase):
    """Supplier invoices received at the ETA and the official 'Reject Document' API."""

    def _token(self):
        return mock.Mock(status_code=200, json=lambda: {'access_token': 't', 'expires_in': 3600})

    def test_list_received_and_reject_button_only_when_allowed(self):
        page = mock.Mock(status_code=200, json=lambda: {'result': [
            {'uuid': 'R1', 'internalId': 'SUP-1', 'issuerName': 'مورد الأرز', 'issuerId': '111222333',
             'status': 'Valid', 'total': 1140, 'dateTimeIssued': '2026-11-20T10:00:00Z'},
            {'uuid': 'R2', 'internalId': 'SUP-2', 'issuerName': 'مورد الزيت', 'status': 'Valid',
             'rejectRequestDate': '2026-11-21T10:00:00Z', 'total': 50},
        ], 'metadata': {'continuationToken': 'EndofResultSet'}})
        login(self.client, self.w.admin)
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.get', return_value=page) as get:
            r = self.client.get(reverse('einvoicing:received_documents') + '?load=1')
        self.assertEqual(get.call_args[1]['params']['direction'], 'Received')
        self.assertContains(r, 'مورد الأرز')
        self.assertContains(r, 'تم طلب الرفض')
        self.assertContains(r, 'name="uuid" value="R1"')
        self.assertNotContains(r, 'name="uuid" value="R2"')

    def test_reject_request_body(self):
        login(self.client, self.w.admin)
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.put',
                           return_value=mock.Mock(status_code=200)) as put:
            r = self.client.post(reverse('einvoicing:reject_received_document'),
                                 {'uuid': 'R1', 'internal_id': 'SUP-1', 'reason': 'Wrong invoice details'}, follow=True)
        self.assertTrue(put.call_args[0][0].endswith('/api/v1.0/documents/state/R1/state'))
        self.assertEqual(put.call_args[1]['json'], {'status': 'rejected', 'reason': 'Wrong invoice details'})
        self.assertContains(r, 'تم رفض الفاتورة SUP-1')

    def test_reject_period_over_message(self):
        login(self.client, self.w.admin)
        bad = mock.Mock(status_code=400, text='', json=lambda: {'error': {'code': 'OperationPeriodOver'}})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.put', return_value=bad):
            r = self.client.post(reverse('einvoicing:reject_received_document'),
                                 {'uuid': 'R1', 'internal_id': 'SUP-1'}, follow=True)
        self.assertContains(r, 'انتهت مدة الرفض')

    def test_cashier_cannot_reject(self):
        login(self.client, self.w.cashier)
        with mock.patch('apps.tenant.einvoicing.services.requests.put') as put:
            self.client.post(reverse('einvoicing:reject_received_document'), {'uuid': 'R1'})
        put.assert_not_called()


class StandardHeadersTests(ETATestBase):
    """Behaviour from the official 'Standard Header Parameters' page."""

    def _token(self):
        return mock.Mock(status_code=200, json=lambda: {'access_token': 't', 'expires_in': 3600})

    def test_request_headers(self):
        from apps.tenant.einvoicing.services import eta_request
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.get',
                           return_value=mock.Mock(status_code=200)) as get:
            eta_request('get', TaxIntegrationSettings.objects.get(id=1), 'https://x/api')
        h = get.call_args[1]['headers']
        self.assertEqual((h['Accept'], h['Accept-Language'], h['Content-Type']), ('application/json', 'ar', 'application/json'))
        self.assertEqual(h['Authorization'], 'Bearer t')

    def test_throttled_429_readable(self):
        from apps.tenant.einvoicing.services import eta_request
        throttled = mock.Mock(status_code=429, headers={'X-Rate-Limit-Reset': '2026-11-24T12:23:41Z',
                                                        'correlationId': 'CID-1'})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.get', return_value=throttled):
            with self.assertRaises(Exception) as cm:
                eta_request('get', TaxIntegrationSettings.objects.get(id=1), 'https://x/api')
        self.assertIn('تجاوز الحد', str(cm.exception))
        self.assertIn('CID-1', str(cm.exception))

    def test_error_carries_correlation_id(self):
        from apps.tenant.einvoicing.services import cancel_on_eta
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        log = EInvoiceLog.objects.get(invoice=inv)
        log.status, log.uuid = 'CANCEL_PENDING', 'U-X'
        log.save()
        bad = mock.Mock(status_code=400, text='', headers={'correlationId': 'CID-2'},
                        json=lambda: {'error': {'code': 'IncorrectState'}})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.put', return_value=bad):
            with self.assertRaises(Exception) as cm:
                cancel_on_eta(log, 'Wrong invoice details')
        self.assertIn('CID-2', str(cm.exception))


class EGSCodeRequestTests(ETATestBase):
    """Behaviour from the official 'Create EGS Code Usage' API page."""

    def _token(self):
        return mock.Mock(status_code=200, json=lambda: {'access_token': 't', 'expires_in': 3600})

    def _product(self):
        p = self.w.product
        p.gs1_code, p.egs_code, p.egs_status = '', '', ''
        p.sku, p.gpc_code, p.name_en = 'RICE1', '10000051', 'Rice 1kg'
        p.save()
        return p

    def test_request_body_and_pending_state_blocks_invoices(self):
        from apps.tenant.einvoicing.egs import request_egs_code
        p = self._product()
        with mock.patch('apps.tenant.einvoicing.services.requests.post',
                        side_effect=[self._token(), mock.Mock(status_code=200, text='', json=lambda: None)]) as post:
            ok, code = request_egs_code(p)
        self.assertTrue(ok)
        self.assertEqual(code, 'EG-123456789-RICE1')
        url, kwargs = post.call_args_list[1][0][0], post.call_args_list[1][1]
        self.assertTrue(url.endswith('/api/v1.0/codetypes/requests/codes'))
        item = kwargs['json']['items'][0]
        self.assertEqual((item['codeType'], item['parentCode'], item['itemCode'], item['codeName']),
                         ('EGS', '10000051', 'EG-123456789-RICE1', 'Rice 1kg'))
        self.assertTrue(item['activeFrom'].endswith('T00:00:00Z'))
        p.refresh_from_db()
        self.assertEqual((p.egs_code, p.egs_status), ('EG-123456789-RICE1', 'REQUESTED'))
        inv = make_invoice(self.w, lines=[(p, 1, 10, 14)])
        with self.assertRaises(ETAValidationError) as cm:
            build_invoice_json(inv.id)
        self.assertIn('انتظار موافقة', ' '.join(cm.exception.errors))
        p.egs_status = 'APPROVED'
        p.save()
        self.assertEqual(build_invoice_json(inv.id)['invoiceLines'][0]['itemCode'], 'EG-123456789-RICE1')

    def test_error_returned_with_http_200(self):
        from apps.tenant.einvoicing.egs import request_egs_code
        p = self._product()
        bad = mock.Mock(status_code=200, text='Not Found',
                        json=lambda: {'error': {'code': 'Not Found', 'message': 'Parent code not found'}})
        with mock.patch('apps.tenant.einvoicing.services.requests.post', side_effect=[self._token(), bad]):
            ok, msg = request_egs_code(p)
        self.assertFalse(ok)
        p.refresh_from_db()
        self.assertEqual(p.egs_status, 'FAILED')
        self.assertIn('GPC', p.egs_message)
        self.assertFalse(p.egs_code)

    def test_missing_gpc_and_english_name(self):
        from apps.tenant.einvoicing.egs import request_egs_code
        p = self._product()
        p.gpc_code, p.name_en = '12', ''
        p.save()
        with mock.patch('apps.tenant.einvoicing.services.requests.post') as post:
            ok, msg = request_egs_code(p)
        self.assertFalse(ok)
        post.assert_not_called()
        self.assertIn('GPC', msg)
        self.assertIn('بالإنجليزي', msg)

    def test_screen_request_and_approve(self):
        p = self._product()
        p.gpc_code = ''
        p.save()
        login(self.client, self.w.admin)
        r = self.client.get(reverse('einvoicing:egs_codes'))
        self.assertContains(r, 'EG-123456789-RICE1')
        with mock.patch('apps.tenant.einvoicing.services.requests.post',
                        side_effect=[self._token(), mock.Mock(status_code=200, text='', json=lambda: None)]):
            self.client.post(reverse('einvoicing:egs_codes'), {'action': 'request', 'selected': [str(p.id)],
                                                              f'gpc_{p.id}': '10000051', f'name_en_{p.id}': 'Rice'})
        p.refresh_from_db()
        self.assertEqual((p.gpc_code, p.egs_status), ('10000051', 'REQUESTED'))
        self.client.post(reverse('einvoicing:egs_codes'), {'action': 'approve', 'product_id': p.id})
        p.refresh_from_db()
        self.assertEqual(p.egs_status, 'APPROVED')


class EGSRequestsSyncTests(ETATestBase):
    """Behaviour from the official 'Search my EGS code usage requests' API page."""

    def _token(self):
        return mock.Mock(status_code=200, json=lambda: {'access_token': 't', 'expires_in': 3600})

    def _products(self):
        a, b = self.w.product, self.w.product2
        for p, code in ((a, 'EG-123456789-A1'), (b, 'EG-123456789-B2')):
            p.gs1_code, p.egs_code, p.egs_status = '', code, 'REQUESTED'
            p.save()
        return a, b

    def test_statuses_and_query(self):
        from apps.tenant.einvoicing.egs import sync_egs_requests
        a, b = self._products()
        rows = [
            {'itemCode': 'EG-123456789-A1', 'status': 'Approved', 'active': True},
            {'itemCode': 'EG-123456789-B2', 'status': 'Rejected'},
            {'itemCode': 'EG-123456789-A1', 'status': 'Submitted'},     # older request of the same code
        ]
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.get',
                           return_value=mock.Mock(status_code=200, json=lambda: rows)) as get:
            s = sync_egs_requests(sleep=lambda x: None)
        url, params = get.call_args[0][0], get.call_args[1]['params']
        self.assertTrue(url.endswith('/api/v1.0/codetypes/requests/my'))
        self.assertEqual((params['RequestType'], params['OrderDirections'], params['Pn']), ('New', 'Descending', 1))
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.egs_status, b.egs_status), ('APPROVED', 'FAILED'))
        self.assertEqual((s['approved'], s['rejected']), (1, 1))
        inv = make_invoice(self.w, lines=[(a, 1, 10, 14)])
        self.assertEqual(build_invoice_json(inv.id)['invoiceLines'][0]['itemCode'], 'EG-123456789-A1')

    def test_paginates_until_short_page(self):
        from apps.tenant.einvoicing import egs
        self._products()
        full = [{'itemCode': f'EG-123456789-X{i}', 'status': 'Submitted'} for i in range(egs.MY_REQUESTS_PAGE_SIZE)]
        last = [{'itemCode': 'EG-123456789-A1', 'status': 'Approved'}]
        pages = iter([mock.Mock(status_code=200, json=lambda: full), mock.Mock(status_code=200, json=lambda: last)])
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.get', side_effect=lambda *a, **k: next(pages)) as get:
            egs.sync_egs_requests(sleep=lambda x: None)
        self.assertEqual([c[1]['params']['Pn'] for c in get.call_args_list], [1, 2])
        self.assertEqual(self.w.product.__class__.objects.get(pk=self.w.product.pk).egs_status, 'APPROVED')

    def test_screen_sync_button(self):
        self._products()
        login(self.client, self.w.admin)
        with mock.patch('apps.tenant.einvoicing.services.requests.post', return_value=self._token()), \
                mock.patch('apps.tenant.einvoicing.services.requests.get',
                           return_value=mock.Mock(status_code=200, json=lambda: [
                               {'itemCode': 'EG-123456789-A1', 'status': 'Approved'}])):
            r = self.client.post(reverse('einvoicing:egs_codes'), {'action': 'sync'}, follow=True)
        self.assertContains(r, 'معتمد 1')


class CategoryEGSTests(ETATestBase):
    """One EGS code per category for all its products without a code of their own."""

    def _token(self):
        return mock.Mock(status_code=200, json=lambda: {'access_token': 't', 'expires_in': 3600})

    def _plain_product(self):
        p = self.w.product
        p.gs1_code, p.egs_code, p.egs_status = '', '', ''
        p.save()
        return p

    def test_product_uses_category_code_with_real_name(self):
        p = self._plain_product()
        cat = p.category
        cat.egs_code, cat.egs_status = 'EG-123456789-CAT1', 'APPROVED'
        cat.save()
        inv = make_invoice(self.w, lines=[(p, 1, 10, 14)])
        line = build_invoice_json(inv.id)['invoiceLines'][0]
        self.assertEqual((line['itemType'], line['itemCode']), ('EGS', 'EG-123456789-CAT1'))
        self.assertEqual(line['description'], p.name)

    def test_parent_category_code_covers_sub_categories(self):
        from apps.tenant.inventory.models import Category
        p = self._plain_product()
        parent = Category.objects.create(name='أدوات كهربائية', egs_code='EG-123456789-CAT9', egs_status='APPROVED')
        child = Category.objects.create(name='شنيورات', parent=parent)
        p.category = child
        p.save()
        inv = make_invoice(self.w, lines=[(p, 1, 10, 14)])
        self.assertEqual(build_invoice_json(inv.id)['invoiceLines'][0]['itemCode'], 'EG-123456789-CAT9')

    def test_own_code_wins_and_pending_category_blocks(self):
        p = self._plain_product()
        cat = p.category
        cat.egs_code, cat.egs_status = 'EG-123456789-CAT1', 'REQUESTED'
        cat.save()
        inv = make_invoice(self.w, lines=[(p, 1, 10, 14)])
        with self.assertRaises(ETAValidationError) as cm:
            build_invoice_json(inv.id)
        self.assertIn('لفئة', ' '.join(cm.exception.errors))
        p.egs_code, p.egs_status = 'EG-123456789-OWN', ''
        p.save()
        self.assertEqual(build_invoice_json(inv.id)['invoiceLines'][0]['itemCode'], 'EG-123456789-OWN')

    def test_gs1_code_is_ignored(self):
        p = self._plain_product()
        p.gs1_code = '5449000000996'
        p.save()
        inv = make_invoice(self.w, lines=[(p, 1, 10, 14)])
        with self.assertRaises(ETAValidationError):
            build_invoice_json(inv.id)

    def test_request_category_code_and_sync(self):
        from apps.tenant.einvoicing.egs import request_category_egs_code, sync_egs_requests
        cat = self.w.category
        cat.gpc_code, cat.name_en = '10000051', 'Electrical tools'
        cat.save()
        with mock.patch('apps.tenant.einvoicing.services.requests.post',
                        side_effect=[self._token(), mock.Mock(status_code=200, text='', json=lambda: None)]) as post:
            ok, code = request_category_egs_code(cat)
        self.assertTrue(ok)
        self.assertEqual(code, f'EG-123456789-CAT{cat.id}')
        self.assertEqual(post.call_args_list[1][1]['json']['items'][0]['codeNameAr'], cat.name)
        cat.refresh_from_db()
        self.assertEqual(cat.egs_status, 'REQUESTED')
        with mock.patch('apps.tenant.einvoicing.services.requests.get',
                        return_value=mock.Mock(status_code=200, json=lambda: [{'itemCode': code, 'status': 'Approved'}])):
            sync_egs_requests(sleep=lambda x: None)
        cat.refresh_from_db()
        self.assertEqual(cat.egs_status, 'APPROVED')

    def test_screen_lists_categories_and_coverage(self):
        p = self._plain_product()
        cat = p.category
        cat.egs_code, cat.egs_status = 'EG-123456789-CAT1', 'APPROVED'
        cat.save()
        login(self.client, self.w.admin)
        r = self.client.get(reverse('einvoicing:egs_codes'))
        self.assertContains(r, 'أكواد الفئات')
        self.assertContains(r, 'يُرسل بكود الفئة')


class SecurityAndBranchTests(ETATestBase):
    """Secrets encrypted at rest; one settings row; a branch's own ETA code/address; the log is kept."""

    def test_secrets_encrypted_in_the_database(self):
        from django.db import connection
        s = TaxIntegrationSettings.objects.get()
        with connection.cursor() as c:
            c.execute('SELECT client_secret, api_token FROM einvoicing_taxintegrationsettings WHERE id = %s', [s.pk])
            raw_secret, raw_token = c.fetchone()
        self.assertTrue(raw_secret.startswith('enc1$'))
        self.assertTrue(raw_token.startswith('enc1$'))
        self.assertEqual(s.client_secret, 'sec')                        # read back in clear
        self.assertFalse(s.api_token.startswith('enc1$'))

    def test_single_settings_row(self):
        with self.assertRaises(ValueError):
            TaxIntegrationSettings.objects.create(client_id='x', client_secret='y')

    def test_branch_with_its_own_eta_code(self):
        b = self.w.branch
        b.eta_branch_code, b.eta_governate, b.eta_region_city, b.eta_street, b.eta_building_number = (
            '3', 'الجيزة', 'الهرم', 'شارع الهرم', '40')
        b.save()
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        addr = build_invoice_json(inv.id)['issuer']['address']
        self.assertEqual((addr['branchID'], addr['governate'], addr['street']), ('3', 'الجيزة', 'شارع الهرم'))

    def test_log_is_never_deleted_with_its_invoice(self):
        from django.db.models import ProtectedError
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        self.assertTrue(EInvoiceLog.objects.filter(invoice=inv).exists())
        Invoice.objects.filter(pk=inv.pk).update(status=Invoice.CANCELLED)
        inv.refresh_from_db()
        with self.assertRaises(ProtectedError):
            inv.delete()


class BuilderRulesTests(ETATestBase):
    """Ids cleaned / checked, taxes as the ETA re-computes them, issue time = posting, credit notes on Valid only."""

    def test_ids_cleaned_and_checked(self):
        co = make_partner('شركة شرطات', 'CUSTOMER', tax_id='123-456-789', address='ش 1',
                          eta_governate='القاهرة', eta_region_city='مصر الجديدة')
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)], partner=co)
        self.assertEqual(build_invoice_json(inv.id)['receiver']['id'], '123456789')
        bad = make_partner('شركة ناقصة', 'CUSTOMER', tax_id='12345', address='ش 1', eta_governate='ق',
                           eta_region_city='م')
        inv2 = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)], partner=bad)
        with self.assertRaises(ETAValidationError) as cm:
            build_invoice_json(inv2.id)
        self.assertIn('9 أرقام', ' '.join(cm.exception.errors))

    def test_tax_is_netTotal_times_rate_at_5_decimals(self):
        from decimal import Decimal
        inv = make_invoice(self.w, lines=[(self.w.product, '0.333', '7.77', 14)])
        doc = build_invoice_json(inv.id)
        ln = doc['invoiceLines'][0]
        t1 = next(t for t in ln['taxableItems'] if t['taxType'] == 'T1')['amount']
        expected = float((Decimal(str(ln['netTotal'])) * 14 / 100).quantize(Decimal('0.00001')))
        self.assertEqual(t1, expected)
        self.check_identities(doc)

    def test_issue_time_is_when_posted(self):
        import datetime as dt
        from django.utils import timezone
        inv = make_invoice(self.w, lines=[(self.w.product, 1, 10, 14)])
        Invoice.objects.filter(pk=inv.pk).update(created_at=timezone.now() - dt.timedelta(days=5))   # old draft
        issued = build_invoice_json(inv.id)['dateTimeIssued']
        self.assertEqual(issued[:10], timezone.now().astimezone(dt.timezone.utc).strftime('%Y-%m-%d'))

    def test_credit_note_refused_when_original_invalid(self):
        sale = make_invoice(self.w, lines=[(self.w.product, 3, 10, 14)])
        EInvoiceLog.objects.filter(invoice=sale).update(status='INVALID', uuid='U-BAD')
        ret = make_invoice(self.w, 'RETURN_SALE', lines=[(self.w.product, 1, 10, 14)], parent_invoice=sale)
        with self.assertRaises(ETAValidationError) as cm:
            build_invoice_json(ret.id)
        self.assertIn('Valid', ' '.join(cm.exception.errors))


class EtaRoundingRecordTests(ETATestBase):
    """The document keeps the official 5-place formulas; the log records what it says next to the invoice's
    own 2-place VAT: 3 × 33.3333 at 14% -> invoice 14.00, document 13.99999, difference 0.00001."""

    def test_difference_recorded_when_the_document_is_built(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 3, D('33.3333'), 14)])
        self.assertEqual(inv.tax_amount, D('14.00'))
        log = EInvoiceLog.objects.get(invoice=inv)
        login(self.client, self.w.admin)
        self.client.post(reverse('einvoicing:approve_invoice', args=[log.id]))
        log.refresh_from_db()
        self.assertEqual(log.status, 'PENDING')
        self.assertEqual(log.eta_tax_total, D('13.99999'))
        self.assertEqual(log.rounding_diff, D('0.00001'))
        self.assertEqual(log.eta_total, D(str(build_invoice_json(inv.id)['totalAmount'])))

    def test_document_formulas_unchanged(self):
        inv = make_invoice(self.w, lines=[(self.w.product, 3, D('33.3333'), 14)])
        doc = build_invoice_json(inv.id)
        self.assertEqual(doc['invoiceLines'][0]['taxableItems'][0]['amount'], 13.99999)   # 5 places, official
        self.check_identities(doc)
