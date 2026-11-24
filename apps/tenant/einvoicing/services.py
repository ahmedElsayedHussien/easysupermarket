"""
Egyptian Tax Authority (ETA) e-invoicing: document building, submission, status and
cancellation.

Arithmetic follows the ETA document rules (all amounts rounded to 5 decimals):
  line:    salesTotal = quantity x unitValue.amountEGP
           netTotal   = salesTotal - discount.amount
           total      = netTotal + T1 - T4 + totalTaxableFees - itemsDiscount
  invoice: totalSalesAmount    = sum(salesTotal)
           totalDiscountAmount = sum(discount.amount)
           netAmount           = sum(netTotal)
           taxTotals           = per tax type sums
           totalAmount         = sum(line total) - extraDiscountAmount
A document that would be rejected for missing data raises ETAValidationError with a
readable Arabic list *before* anything is sent.
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.utils import timezone

from apps.tenant.invoicing.models import Invoice

from .models import EInvoiceLog, TaxIntegrationSettings

ZERO = Decimal('0')
Q5 = Decimal('0.00001')

# Product.unit / UoM names -> ETA unit types (UN/ECE Rec. 20 codes used by the ETA code list)
UNIT_TYPES = {
    'PIECE': 'EA', 'KG': 'KGM', 'LITER': 'LTR', 'BOX': 'BX', 'DOZEN': 'DZN',
    'GRAM': 'GRM', 'METER': 'MTR', 'PACK': 'PK',
}
UNIT_NAME_TYPES = {
    'قطعة': 'EA', 'قطعه': 'EA', 'حبة': 'EA', 'حبه': 'EA',
    'كيلو': 'KGM', 'كجم': 'KGM', 'كيلوجرام': 'KGM', 'كيلو جرام': 'KGM',
    'لتر': 'LTR', 'كرتونة': 'BX', 'كرتونه': 'BX', 'كرتون': 'BX',
    'دستة': 'DZN', 'دسته': 'DZN', 'جرام': 'GRM', 'جم': 'GRM', 'متر': 'MTR',
    'عبوة': 'PK', 'عبوه': 'PK', 'باكت': 'PK',
}


class ETAValidationError(Exception):
    """The document can't be accepted by the ETA: ``errors`` lists what to fix."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__(' | '.join(self.errors))


def q5(value):
    return Decimal(str(value or 0)).quantize(Q5, rounding=ROUND_HALF_UP)


def num(value):
    """Decimal -> JSON number (5 decimals, no float noise beyond that)."""
    return float(q5(value))


def get_settings():
    return TaxIntegrationSettings.objects.first()


def issuer_name(settings_obj):
    """Shop name from Settings (core.company), the e-invoicing field only as fallback."""
    from apps.tenant.core.company import company_profile
    return (company_profile()['name'] or (settings_obj.company_name or '')).strip()


def issuer_address(settings_obj):
    addr = {
        'branchID': (settings_obj.branch_code or '0').strip(),
        'country': 'EG',
        'governate': settings_obj.governate.strip(),
        'regionCity': settings_obj.region_city.strip(),
        'street': settings_obj.street.strip(),
        'buildingNumber': settings_obj.building_number.strip(),
    }
    for key, field in (('postalCode', 'postal_code'), ('floor', 'floor'), ('room', 'room'),
                       ('landmark', 'landmark')):
        val = (getattr(settings_obj, field, '') or '').strip()
        if val:
            addr[key] = val
    return addr


def settings_errors(settings_obj):
    """What is missing in the e-invoicing settings (empty list = ready)."""
    if settings_obj is None:
        return ['إعدادات الفاتورة الإلكترونية غير موجودة.']
    errors = []
    if not (settings_obj.company_id or '').strip().isdigit() or len(settings_obj.company_id.strip()) != 9:
        errors.append('رقم التسجيل الضريبي للشركة يجب أن يكون 9 أرقام (الإعدادات).')
    if not issuer_name(settings_obj):
        errors.append('اسم المحل غير مسجل — اكتبه في الإعدادات العامة (اسم المؤسسة / المتجر).')
    code = (settings_obj.taxpayer_activity_code or '').strip()
    if not (code.isdigit() and len(code) == 4):
        errors.append('كود النشاط يجب أن يكون 4 أرقام كما هو مسجل في الضرائب (الإعدادات).')
    for field, label in (('governate', 'المحافظة'), ('region_city', 'المدينة / المنطقة'),
                         ('street', 'الشارع'), ('building_number', 'رقم المبنى')):
        if not (getattr(settings_obj, field, '') or '').strip():
            errors.append(f'عنوان المحل ناقص: {label} (إعدادات الفاتورة الإلكترونية).')
    return errors


def unit_type(line):
    if line.uom_id and getattr(line.uom, 'uom', None):
        name = ' '.join((line.uom.uom.name or '').split())
        if name in UNIT_NAME_TYPES:
            return UNIT_NAME_TYPES[name]
    return UNIT_TYPES.get(getattr(line.product, 'unit', '') or 'PIECE', 'EA')


def item_code(product):
    """(itemType, itemCode) registered at the ETA, or None."""
    # Coding policy: EGS only (GS1 codes are not used).
    egs = (product.egs_code or '').strip()
    # A code registered from the program is usable only once the ETA approved it
    # (a code typed in by hand, egs_status = '', is trusted as already approved).
    if egs and getattr(product, 'egs_status', '') not in ('REQUESTED', 'FAILED'):
        return 'EGS', egs
    # No code of its own: the EGS code of its category (or the nearest parent category)
    cat = category_code_holder(product)
    if cat is not None:
        return 'EGS', cat.egs_code.strip()
    return None


def _category_chain(product):
    cat = getattr(product, 'category', None)
    if cat is None:
        return []
    try:
        return list(cat.get_ancestors(ascending=True, include_self=True))
    except Exception:  # noqa: BLE001 - not an MPTT tree
        return [cat]


def category_code_holder(product):
    """Nearest category (self, then parents) with an approved EGS code."""
    for cat in _category_chain(product):
        if (cat.egs_code or '').strip() and cat.egs_status not in ('REQUESTED', 'FAILED'):
            return cat
    return None


def pending_category(product):
    for cat in _category_chain(product):
        if (cat.egs_code or '').strip() and cat.egs_status == 'REQUESTED':
            return cat
    return None


def vat_subtype(rate):
    """T1 sub-type by rate: 14% general items, other positive rates, 0% = exempt."""
    rate = q5(rate)
    if rate == 0:
        return 'V003'
    if rate == Decimal('14'):
        return 'V009'
    return 'V010'


def issue_datetime(invoice):
    """Actual issue moment in UTC (ETA rejects dates in the future)."""
    moment = invoice.created_at or timezone.now()
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    moment = min(moment, timezone.now())
    return moment.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _receiver(invoice, settings_obj, total_amount, errors):
    partner = invoice.partner
    name = (getattr(partner, 'name', '') or '').strip() or 'عميل نقدي'
    tax_id = (getattr(partner, 'tax_id', '') or '').strip() if partner else ''
    national_id = (getattr(partner, 'national_id', '') or '').strip() if partner else ''
    if tax_id:
        address = {
            'country': 'EG',
            'governate': settings_obj.governate.strip() or 'Cairo',
            'regionCity': settings_obj.region_city.strip() or 'Cairo',
            'street': ((getattr(partner, 'address', '') or '').strip() or settings_obj.street.strip())[:200],
            'buildingNumber': '0',
        }
        return {'type': 'B', 'id': tax_id, 'name': name, 'address': address}
    receiver = {'type': 'P', 'name': name, 'address': {'country': 'EG'}}
    if national_id:
        receiver['id'] = national_id
    elif total_amount >= Decimal(str(settings_obj.receiver_id_threshold or 0)) > 0:
        errors.append(f'الفاتورة بقيمة {total_amount:,.2f} ج.م لعميل فرد: لازم الرقم القومي للعميل "{name}" '
                      f'(الحد {settings_obj.receiver_id_threshold:,.0f} ج.م) — أضفه في بيانات العميل.')
    return receiver


def build_invoice_json(invoice_id):
    """ETA document (dict) for a SALE invoice (I) or a sales return (credit note C)."""
    invoice = Invoice.objects.select_related('partner', 'parent_invoice').get(id=invoice_id)
    settings_obj = get_settings()
    errors = settings_errors(settings_obj)
    if settings_obj is None:
        raise ETAValidationError(errors)

    if invoice.invoice_type == Invoice.SALE:
        document_type = 'I'
    elif invoice.invoice_type == Invoice.RETURN_SALE:
        document_type = 'C'
    else:
        raise ETAValidationError(['نوع المستند لا يُرسل لمنظومة الفاتورة الإلكترونية.'])

    lines, sums = [], {'sales': ZERO, 'discount': ZERO, 'net': ZERO, 'total': ZERO, 't1': ZERO, 't4': ZERO}
    for line in invoice.lines.select_related('product', 'uom__uom').all():
        product = line.product
        code = item_code(product)
        if code is None:
            if getattr(product, 'egs_status', '') == 'REQUESTED':
                errors.append(f'كود EGS للصنف "{product.name}" ما زال في انتظار موافقة الضرائب.')
            elif pending_category(product) is not None:
                errors.append(f'كود EGS لفئة "{pending_category(product).name}" (للصنف "{product.name}") '
                              f'ما زال في انتظار موافقة الضرائب.')
            else:
                errors.append(f'الصنف "{product.name}" ليس له كود EGS — '
                              f'سجّله من شاشة "تسجيل أكواد الأصناف" أو سجّل كوداً لفئته من شاشة "تسجيل أكواد الأصناف".')
            continue
        qty = q5(line.quantity)
        unit_value = q5(line.unit_price)
        sales_total = q5(qty * unit_value)
        net_total = q5(line.subtotal)
        discount = q5(max(sales_total - net_total, ZERO))
        net_total = q5(sales_total - discount)               # keep the ETA identity exact
        t1 = q5(line.tax_amount)
        t4 = q5(line.wht_amount)
        total = q5(net_total + t1 - t4)

        taxable = [{'taxType': 'T1', 'amount': num(t1), 'subType': vat_subtype(line.tax_rate),
                    'rate': num(line.tax_rate)}]
        if t4 > 0:
            taxable.append({'taxType': 'T4', 'amount': num(t4),
                            'subType': (settings_obj.wht_subtype or 'W010').strip(),
                            'rate': num(line.wht_rate)})
        lines.append({
            'description': product.name[:500],
            'itemType': code[0],
            'itemCode': code[1],
            'unitType': unit_type(line),
            'quantity': num(qty),
            'internalCode': str(product.sku or product.barcode or product.id),
            'salesTotal': num(sales_total),
            'total': num(total),
            'valueDifference': 0,
            'totalTaxableFees': 0,
            'netTotal': num(net_total),
            'itemsDiscount': 0,
            'unitValue': {'currencySold': 'EGP', 'amountEGP': num(unit_value)},
            'discount': {'rate': num(line.discount_pct or 0), 'amount': num(discount)},
            'taxableItems': taxable,
        })
        sums['sales'] += sales_total
        sums['discount'] += discount
        sums['net'] += net_total
        sums['total'] += total
        sums['t1'] += t1
        sums['t4'] += t4

    if not lines and not errors:
        errors.append('الفاتورة لا تحتوي على أصناف.')

    receiver = _receiver(invoice, settings_obj, q5(sums['total']), errors)

    references = []
    if document_type == 'C':
        parent_log = getattr(invoice.parent_invoice, 'einvoice_log', None) if invoice.parent_invoice_id else None
        if parent_log is None or not parent_log.uuid:
            errors.append('المرتجع لا يُرسل إلا بعد قبول الفاتورة الأصلية لدى الضرائب (لها رقم UUID).')
        else:
            references = [parent_log.uuid]

    if errors:
        raise ETAValidationError(errors)

    tax_totals = [{'taxType': 'T1', 'amount': num(sums['t1'])}]
    if sums['t4'] > 0:
        tax_totals.append({'taxType': 'T4', 'amount': num(sums['t4'])})

    document = {
        'issuer': {'address': issuer_address(settings_obj), 'type': 'B',
                   'id': settings_obj.company_id.strip(), 'name': issuer_name(settings_obj)},
        'receiver': receiver,
        'documentType': document_type,
        'documentTypeVersion': '1.0',
        'dateTimeIssued': issue_datetime(invoice),
        'taxpayerActivityCode': settings_obj.taxpayer_activity_code.strip(),
        'internalID': invoice.invoice_number or str(invoice.id),
        'invoiceLines': lines,
        'totalSalesAmount': num(sums['sales']),
        'totalDiscountAmount': num(sums['discount']),
        'netAmount': num(sums['net']),
        'taxTotals': tax_totals,
        'extraDiscountAmount': 0,
        'totalItemsDiscountAmount': 0,
        'totalAmount': num(sums['total']),
    }
    if references:
        document['references'] = references
    return document


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def _base_url(settings_obj):
    return 'https://api.invoicing.eta.gov.eg' if settings_obj.is_production else 'https://api.preprod.invoicing.eta.gov.eg'


# Access tokens are valid for expires_in seconds (1 hour): reuse until shortly before expiry.
_TOKEN_CACHE = {}
TOKEN_RENEW_MARGIN = 60

LOGIN_ERRORS = {
    'invalid_client': 'بيانات الدخول للضرائب غير صحيحة: راجع Client ID و Client Secret (وهل هي للبيئة الصحيحة: تجريبية / فعلية).',
    'invalid_grant': 'تم رفض الدخول: حساب النظام لدى الضرائب موقوف أو منتهي.',
    'unauthorized_client': 'هذا الحساب غير مصرح له بالدخول بهذه الطريقة لدى الضرائب.',
    'invalid_scope': 'الصلاحية المطلوبة (InvoicingAPI) غير متاحة لهذا الحساب.',
    'invalid_request': 'طلب الدخول غير صحيح (راجع الإعدادات).',
}


def _token_key(settings_obj):
    from django.db import connection
    return (getattr(connection, 'schema_name', ''), settings_obj.client_id, bool(settings_obj.is_production),
            (getattr(settings_obj, 'on_behalf_of', '') or '').strip())


def get_eta_access_token(settings_obj, force=False):
    """OAuth2 client credentials on the identity service (Basic auth, RFC 2617)."""
    import time
    key = _token_key(settings_obj)
    cached = _TOKEN_CACHE.get(key)
    if cached and not force and cached[1] > time.time():
        return cached[0]
    auth_url = 'https://id.eta.gov.eg/connect/token' if settings_obj.is_production else 'https://id.preprod.eta.gov.eg/connect/token'
    headers = {'Accept': 'application/json', 'Accept-Language': 'ar'}
    on_behalf = (getattr(settings_obj, 'on_behalf_of', '') or '').strip()
    if on_behalf:
        headers['onbehalfof'] = on_behalf
    response = requests.post(auth_url, data={'grant_type': 'client_credentials', 'scope': 'InvoicingAPI'},
                             auth=(settings_obj.client_id, settings_obj.client_secret), headers=headers,
                             timeout=(10, 30))
    if response.status_code == 200:
        data = response.json()
        token = data.get('access_token')
        lifetime = int(data.get('expires_in') or 3600)
        _TOKEN_CACHE[key] = (token, time.time() + max(lifetime - TOKEN_RENEW_MARGIN, 30))
        return token
    _TOKEN_CACHE.pop(key, None)
    try:
        err = response.json()
    except ValueError:
        err = {}
    code = err.get('error', '') if isinstance(err, dict) else ''
    detail = err.get('error_description', '') if isinstance(err, dict) else ''
    raise Exception(LOGIN_ERRORS.get(code, f'فشل الدخول لمنظومة الضرائب (HTTP {response.status_code} {code})')
                    + (f' — {detail}' if detail else ''))


STANDARD_HEADERS = {'Accept': 'application/json', 'Accept-Language': 'ar', 'Content-Type': 'application/json'}


def _headers(settings_obj, force=False):
    return {**STANDARD_HEADERS, 'Authorization': f'Bearer {get_eta_access_token(settings_obj, force)}'}


def correlation_id(resp):
    """ETA correlationId of a response (give it to ETA support when reporting a problem)."""
    headers = getattr(resp, 'headers', None) if resp is not None else None
    try:
        value = headers.get('correlationId') if headers is not None else None
    except Exception:  # noqa: BLE001
        value = None
    return value if isinstance(value, str) else ''


def _cid(resp):
    cid = correlation_id(resp)
    return f' [رقم المتابعة لدى الضرائب: {cid}]' if cid else ''


def eta_request(method, settings_obj, url, **kwargs):
    """Authenticated call; on 401 (token expired / revoked) log in again and retry once."""
    call = getattr(requests, method)
    kwargs.setdefault('timeout', (10, 60))
    resp = call(url, headers=_headers(settings_obj), **kwargs)
    if resp.status_code == 401:
        resp = call(url, headers=_headers(settings_obj, force=True), **kwargs)
    if resp.status_code == 429:
        h = resp.headers if isinstance(getattr(resp, 'headers', None), dict) or hasattr(resp.headers, 'get') else {}
        when = h.get('X-Rate-Limit-Reset') or h.get('Retry-After') or ''
        when = when if isinstance(when, str) else ''
        raise Exception('تم تجاوز الحد المسموح من الطلبات لدى الضرائب'
                        + (f' — أعد المحاولة بعد {when}' if when else ' — أعد المحاولة بعد قليل')
                        + '.' + _cid(resp))
    return resp


def submit_to_eta(signed_invoice_data, settings_obj):
    payload = {'documents': [signed_invoice_data]}
    url = f'{_base_url(settings_obj)}/api/v1.0/documentsubmissions'
    return eta_request('post', settings_obj, url, json=payload)


STATUS_MAP = {'valid': 'VALID', 'invalid': 'INVALID', 'rejected': 'REJECTED', 'cancelled': 'CANCELLED',
              'submitted': 'SUBMITTED'}


def eta_state(doc, fallback):
    """Our log status from an ETA document (details or search summary).
    A Valid document with a cancellation request stays Valid at the ETA until the
    receiver accepts (or the delay passes) — or declines it."""
    status = STATUS_MAP.get((doc.get('status') or '').lower(), fallback)
    if status == 'VALID':
        if doc.get('declineCancelRequestDate'):
            return 'CANCEL_DECLINED'
        if doc.get('cancelRequestDate'):
            return 'CANCEL_REQUESTED'
        # The receiver asked to reject it: Rejected after the delay unless we decline
        if doc.get('rejectRequestDate') and not doc.get('declineRejectRequestDate'):
            return 'REJECT_REQUESTED'
    return status


def _error_messages(error):
    """Flatten an ETA Error structure (message + nested details) into strings."""
    if not isinstance(error, dict):
        return [str(error)] if error else []
    out = []
    msg = error.get('message') or error.get('code')
    target = error.get('target') or error.get('propertyPath')
    if msg:
        out.append(f'{msg} ({target})' if target else msg)
    for sub in error.get('details') or []:
        out.extend(_error_messages(sub))
    return out


def validation_errors(validation_results):
    """Readable reasons of an Invalid document from validationResults.validationSteps."""
    errors = []
    for step in (validation_results or {}).get('validationSteps') or []:
        if (step.get('status') or '').lower() == 'invalid':
            msgs = _error_messages(step.get('error')) or ['غير صالح']
            errors.extend(f"{step.get('name') or 'تحقق'}: {m}" for m in msgs)
    return errors


def refresh_status(log, settings_obj=None):
    """Ask the ETA for the document's final status (Valid / Invalid / Cancelled ...)."""
    settings_obj = settings_obj or get_settings()
    base = _base_url(settings_obj)
    if log.uuid:
        resp = eta_request('get', settings_obj, f'{base}/api/v1.0/documents/{log.uuid}/details')
        if resp.status_code != 200:
            raise Exception(f'تعذر الاستعلام عن الفاتورة (HTTP {resp.status_code}): {resp.text[:300]}' + _cid(resp))
        data = resp.json()
        new_status = eta_state(data, log.status)
        log.long_id = data.get('longId') or log.long_id
        log.submission_id = data.get('submissionUUID') or log.submission_id
        details = data.get('validationResults')
    elif log.submission_id:
        resp = eta_request('get', settings_obj, f'{base}/api/v1.0/documentSubmissions/{log.submission_id}')
        if resp.status_code != 200:
            raise Exception(f'تعذر الاستعلام عن الإرسال (HTTP {resp.status_code}): {resp.text[:300]}' + _cid(resp))
        data = resp.json()
        summary = (data.get('documentSummary') or [{}])[0]
        log.uuid = summary.get('uuid') or log.uuid
        new_status = eta_state({'status': summary.get('status') or data.get('overallStatus')}, log.status)
        details = data
    else:
        raise Exception('لا يوجد رقم إرسال أو UUID للاستعلام.')
    log.status = new_status
    log.eta_response = {'status': new_status, 'errors': validation_errors(details), 'details': details,
                        'correlationId': correlation_id(resp)}
    log.save(update_fields=['status', 'uuid', 'long_id', 'submission_id', 'eta_response', 'updated_at'])
    return log.status


# Reasons named by the official "Cancel Document" API page
CANCEL_REASONS = ['Wrong invoice details', 'Wrong buyer details']

CANCEL_ERRORS = {
    'OperationPeriodOver': 'انتهت مدة الإلغاء المسموحة لدى الضرائب — اعمل مرتجع (إشعار دائن) على الفاتورة بدلاً من الإلغاء.',
    'IncorrectState': 'حالة الفاتورة لدى الضرائب لا تسمح بالإلغاء (قد تكون مرفوضة أو غير صالحة بالفعل).',
    'ActiveReferencingDocuments': 'الفاتورة مرتبط بها مستند آخر (مثل مرتجع / إشعار دائن) — ألغِ المستند المرتبط أولاً ثم الفاتورة.',
    'TryingToCancelFreezedDocument': 'الفاتورة مجمّدة لدى الضرائب ولا يمكن إلغاؤها.',
    'Forbidden': 'غير مسموح بالإلغاء لهذا الحساب أو لهذه الفاتورة لدى الضرائب.',
}


def _eta_error_message(resp):
    """Human readable message of a standard error response (Arabic with Accept-Language: ar)."""
    try:
        data = resp.json()
    except ValueError:
        return ''
    err = data.get('error', data) if isinstance(data, dict) else {}
    return ' • '.join(_error_messages(err)) if isinstance(err, dict) else ''


def _eta_error_code(resp):
    try:
        data = resp.json()
    except ValueError:
        return ''
    err = data.get('error', data) if isinstance(data, dict) else {}
    if isinstance(err, dict):
        return err.get('code') or ''
    return ''


def cancel_on_eta(log, reason, settings_obj=None):
    """Request cancellation of an accepted document (allowed for a limited time after issue)."""
    if not log.uuid:
        raise Exception('الفاتورة ليس لها UUID لدى الضرائب.')
    reason = (reason or '').strip() or CANCEL_REASONS[0]
    settings_obj = settings_obj or get_settings()
    url = f'{_base_url(settings_obj)}/api/v1.0/documents/state/{log.uuid}/state'
    resp = eta_request('put', settings_obj, url, json={'status': 'cancelled', 'reason': reason[:500]})
    if resp.status_code != 200:
        code = _eta_error_code(resp)
        msg = (CANCEL_ERRORS.get(code) or _eta_error_message(resp)
               or f'رفضت الضرائب طلب الإلغاء (HTTP {resp.status_code} {code}): {resp.text[:300]}')
        raise Exception(msg + _cid(resp))
    log.status = 'CANCEL_REQUESTED'
    log.eta_response = {'cancel_reason': reason}
    log.save(update_fields=['status', 'eta_response', 'updated_at'])
    return log.status


# ---------------------------------------------------------------------------
# Search Documents API (GET /api/v1.0/documents/search): reconcile our log with the ETA
# ---------------------------------------------------------------------------
SEARCH_MAX_DAYS = 30            # ETA limit between the From/To filters
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES = 20           # one run: up to 2,000 documents
SEARCH_THROTTLE_SECONDS = 2     # ETA: 1 request every 2 seconds per taxpayer
END_OF_RESULTS = 'EndofResultSet'


def _utc_param(dt):
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def search_sent_documents(date_from, date_to, settings_obj=None, sleep=None):
    """Yield the ETA document summaries we SENT, submitted between date_from and date_to."""
    return search_documents(date_from, date_to, 'Sent', settings_obj, sleep)


def search_documents(date_from, date_to, direction, settings_obj=None, sleep=None, max_pages=SEARCH_MAX_PAGES):
    """Search Documents API, Sent or Received, all pages (continuationToken)."""
    import time
    sleep = sleep or time.sleep
    if date_to < date_from:
        raise ValueError('تاريخ النهاية قبل تاريخ البداية.')
    if (date_to - date_from).days >= SEARCH_MAX_DAYS:
        raise ValueError(f'الفترة يجب ألا تتجاوز {SEARCH_MAX_DAYS} يوماً (قيد من الضرائب).')
    settings_obj = settings_obj or get_settings()
    start = datetime.datetime.combine(date_from, datetime.time.min)
    end = datetime.datetime.combine(date_to, datetime.time(23, 59, 59))
    params = {
        'submissionDateFrom': _utc_param(start),
        'submissionDateTo': _utc_param(end),
        'direction': direction,
        'pageSize': SEARCH_PAGE_SIZE,
        'continuationToken': '',
    }
    url = f'{_base_url(settings_obj)}/api/v1.0/documents/search'
    for page in range(max_pages):
        if page:
            sleep(SEARCH_THROTTLE_SECONDS)
        resp = eta_request('get', settings_obj, url, params=dict(params))
        if resp.status_code != 200:
            raise Exception(f'تعذر البحث في مستندات الضرائب (HTTP {resp.status_code}): {resp.text[:300]}' + _cid(resp))
        data = resp.json()
        for doc in data.get('result') or []:
            yield doc
        token = (data.get('metadata') or {}).get('continuationToken') or END_OF_RESULTS
        if token == END_OF_RESULTS:
            return
        params['continuationToken'] = token   # the other filters must stay unchanged


def sync_with_eta(date_from, date_to, settings_obj=None, sleep=None):
    """
    Update our log from the ETA's own records for a period:
    status, uuid, longId, submission id and the public link. Returns a summary with the
    documents found at the ETA that the program does not know (internalId / uuid).
    """
    summary = {'checked': 0, 'updated': 0, 'unknown': []}
    for doc in search_sent_documents(date_from, date_to, settings_obj, sleep):
        summary['checked'] += 1
        uuid, internal = doc.get('uuid') or '', doc.get('internalId') or ''
        log = (EInvoiceLog.objects.filter(uuid=uuid).first() if uuid else None) or               (EInvoiceLog.objects.filter(invoice__invoice_number=internal).first() if internal else None)
        if log is None:
            summary['unknown'].append(internal or uuid)
            continue
        status = eta_state(doc, log.status)
        new = {'status': status, 'uuid': uuid or log.uuid,
               'long_id': doc.get('longId') or log.long_id,
               'submission_id': doc.get('submissionUUID') or log.submission_id,
               'public_url': doc.get('publicUrl') or log.public_url}
        changed = [k for k, v in new.items() if getattr(log, k) != v]
        if changed:
            for k in changed:
                setattr(log, k, new[k])
            log.save(update_fields=changed + ['updated_at'])
            summary['updated'] += 1
    return summary


# ---------------------------------------------------------------------------
# Received documents (from suppliers) and rejecting them (Reject Document API)
# ---------------------------------------------------------------------------
REJECT_REASONS = [
    ('Received incorrect invoice from the seller', 'فاتورة غير صحيحة من البائع'),
    ('Wrong invoice details', 'بيانات الفاتورة خاطئة'),
    ('Wrong buyer details', 'بيانات المشتري (نحن) خاطئة'),
]
REJECT_ERRORS = {
    'OperationPeriodOver': 'انتهت مدة الرفض المسموحة — اطلب من المورد عمل إشعار دائن لتصحيح الفاتورة.',
    'IncorrectState': 'لا يمكن رفض الفاتورة إلا وهي بحالة "صالحة" (Valid).',
    'ActiveReferencingDocuments': 'الفاتورة مرتبط بها مستند آخر (مثل إشعار دائن من المورد) — إن لزم الرفض ارفض المستند المرتبط أولاً.',
    'TryingToRejectFreezedDocument': 'الفاتورة مجمّدة لدى الضرائب ولا يمكن رفضها.',
    'Forbidden': 'غير مسموح برفض هذه الفاتورة لهذا الحساب (ليست مستلمة باسمك أو الرفض موقوف للحساب).',
}


def received_documents(date_from, date_to, settings_obj=None, sleep=None, max_pages=5):
    """Documents suppliers issued to us (only Valid / Rejected / Cancelled are visible to a receiver)."""
    docs = list(search_documents(date_from, date_to, 'Received', settings_obj, sleep, max_pages))
    for d in docs:
        d['can_reject'] = ((d.get('status') or '').lower() == 'valid' and not d.get('rejectRequestDate')
                           and not d.get('cancelRequestDate'))
    return docs


def reject_document(uuid, reason, settings_obj=None):
    """Reject a document received from a supplier — allowed once, within the rejection period."""
    uuid = (uuid or '').strip()
    if not uuid:
        raise Exception('رقم الفاتورة (UUID) غير محدد.')
    reason = (reason or '').strip() or REJECT_REASONS[0][0]
    settings_obj = settings_obj or get_settings()
    url = f'{_base_url(settings_obj)}/api/v1.0/documents/state/{uuid}/state'
    resp = eta_request('put', settings_obj, url, json={'status': 'rejected', 'reason': reason[:500]})
    if resp.status_code != 200:
        code = _eta_error_code(resp)
        msg = (REJECT_ERRORS.get(code) or _eta_error_message(resp)
               or f'رفضت الضرائب طلب الرفض (HTTP {resp.status_code} {code}): {resp.text[:300]}')
        raise Exception(msg + _cid(resp))
    return True
