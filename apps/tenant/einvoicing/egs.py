"""
Registering products' internal codes (EGS) at the Tax Authority
(Create EGS Code Usage API: POST /api/v1.0/codetypes/requests/codes).

The code format is "EG-<taxpayer registration number>-<internal code>". A request goes to
ETA employees for approval; until approved the code must not be used in documents
(the invoice builder refuses products whose EGS code is still REQUESTED).

Note from the official page: errors ("Bad request" = code already exists, "Not Found" =
parent GPC code unknown) come back with HTTP 200, so the body is inspected too.
"""
import datetime
import re

from django.utils import timezone

from .services import _base_url, _cid, _error_messages, eta_request, get_settings

GPC_RE = re.compile(r'^\d{8}$')


def internal_code(product):
    raw = (product.sku or product.barcode or str(product.id) or '').strip()
    return re.sub(r'[^A-Za-z0-9\-]', '', raw) or str(product.id)


def egs_code_for(product, settings_obj):
    return f'EG-{(settings_obj.company_id or "").strip()}-{internal_code(product)}'


def product_errors(product):
    errors = []
    if not GPC_RE.match((product.gpc_code or '').strip()):
        errors.append('كود GPC يجب أن يكون 8 أرقام (المستوى الرابع من تصنيف GS1).')
    if not (product.name_en or '').strip():
        errors.append('اسم الصنف بالإنجليزي مطلوب.')
    return errors


def _response_error(resp):
    """Error text of a code request response ('' = accepted). Errors may come with HTTP 200."""
    if resp.status_code not in (200, 201, 202):
        try:
            return ' • '.join(_error_messages((resp.json() or {}).get('error', resp.json()))) or f'HTTP {resp.status_code}'
        except Exception:  # noqa: BLE001
            return f'HTTP {resp.status_code}: {resp.text[:200]}'
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None
    found = []

    def scan(node):
        if isinstance(node, dict):
            if node.get('error'):
                found.extend(_error_messages(node['error']) or [str(node['error'])])
            for key in ('errors', 'items', 'details', 'result'):
                if isinstance(node.get(key), list):
                    for sub in node[key]:
                        scan(sub)
        elif isinstance(node, list):
            for sub in node:
                scan(sub)
    scan(data)
    text = (resp.text or '')
    if not found and re.search(r'\b(Bad request|Not Found)\b', text, re.I):
        found.append(text[:300])
    return ' • '.join(found)


def friendly(message):
    low = (message or '').lower()
    if 'bad request' in low or 'exist' in low:
        return 'الكود مسجل بالفعل لدى الضرائب — إن كان كودك، اضغط "معتمد" بعد التأكد من البوابة. ' + message
    if 'not found' in low:
        return 'كود GPC (الأب) غير موجود لدى الضرائب — راجع الكود. ' + message
    return message


def request_egs_code(product, settings_obj=None, reason='New product line item'):
    """Send one product's EGS code for registration. Updates the product, returns (ok, message)."""
    settings_obj = settings_obj or get_settings()
    errors = product_errors(product)
    company = (settings_obj.company_id or '').strip() if settings_obj else ''
    if not (company.isdigit() and len(company) == 9):
        errors.append('رقم التسجيل الضريبي للشركة (9 أرقام) غير مسجل في إعدادات الفاتورة الإلكترونية.')
    if errors:
        return False, ' • '.join(errors)

    code = egs_code_for(product, settings_obj)
    today = timezone.now().astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT00:00:00Z')
    item = {
        'codeType': 'EGS',
        'parentCode': product.gpc_code.strip(),
        'itemCode': code,
        'codeName': product.name_en.strip()[:200],
        'codeNameAr': product.name.strip()[:200],
        'activeFrom': today,
        'description': (product.name_en or '').strip()[:500],
        'descriptionAr': (product.description or product.name or '').strip()[:500],
        'requestReason': reason,
    }
    url = f'{_base_url(settings_obj)}/api/v1.0/codetypes/requests/codes'
    try:
        resp = eta_request('post', settings_obj, url, json={'items': [item]})
    except Exception as e:  # network / login / throttling
        return False, str(e)
    error = _response_error(resp)
    product.egs_requested_at = timezone.now()
    if error:
        product.egs_status = product.EGS_FAILED
        product.egs_message = (friendly(error) + _cid(resp))[:500]
        product.save(update_fields=['egs_status', 'egs_message', 'egs_requested_at', 'updated_at'])
        return False, product.egs_message
    product.egs_code = code
    product.egs_status = product.EGS_REQUESTED
    product.egs_message = 'تم إرسال الطلب — في انتظار موافقة موظفي الضرائب.'
    product.save(update_fields=['egs_code', 'egs_status', 'egs_message', 'egs_requested_at', 'updated_at'])
    return True, code



# ---------------------------------------------------------------------------
# Search my EGS code usage requests (GET /api/v1.0/codetypes/requests/my)
# ---------------------------------------------------------------------------
MY_REQUESTS_PAGE_SIZE = 100
MY_REQUESTS_MAX_PAGES = 20


def my_code_requests(settings_obj=None, sleep=None, **filters):
    """All our 'New code' requests, newest first, every page (Pn / Ps)."""
    import time
    from .services import _base_url as base_url
    sleep = sleep or time.sleep
    settings_obj = settings_obj or get_settings()
    url = f'{base_url(settings_obj)}/api/v1.0/codetypes/requests/my'
    out = []
    for page in range(1, MY_REQUESTS_MAX_PAGES + 1):
        if page > 1:
            sleep(1)
        params = {'RequestType': 'New', 'OrderDirections': 'Descending',
                  'Pn': page, 'Ps': MY_REQUESTS_PAGE_SIZE, **filters}
        resp = eta_request('get', settings_obj, url, params=params)
        if resp.status_code != 200:
            raise Exception(f'تعذر الاستعلام عن طلبات الأكواد (HTTP {resp.status_code}): {resp.text[:300]}' + _cid(resp))
        data = resp.json()
        rows = data if isinstance(data, list) else (data.get('result') or data.get('items') or [])
        out.extend(rows)
        if len(rows) < MY_REQUESTS_PAGE_SIZE:
            break
    return out


def sync_egs_requests(settings_obj=None, sleep=None):
    """Update products from the ETA's answer to their EGS requests (Approved / Rejected / Submitted)."""
    from apps.tenant.inventory.models import Product
    summary = {'checked': 0, 'approved': 0, 'rejected': 0, 'pending': 0}
    by_code = {}
    for row in my_code_requests(settings_obj, sleep):
        code = (row.get('itemCode') or '').strip()
        if code and code not in by_code:          # newest request per code wins
            by_code[code] = row
    if not by_code:
        return summary
    from apps.tenant.inventory.models import Category
    holders = list(Product.objects.filter(egs_code__in=list(by_code))) + \
        list(Category.objects.filter(egs_code__in=list(by_code)))
    for p in holders:
        row = by_code[p.egs_code]
        summary['checked'] += 1
        status = (row.get('status') or '').lower()
        if status == 'approved' and row.get('active', True) is not False:
            new, msg = Product.EGS_APPROVED, 'اعتمدت الضرائب الكود.'
            summary['approved'] += 1
        elif status == 'rejected':
            new, msg = Product.EGS_FAILED, 'رفضت الضرائب طلب تسجيل الكود — راجع البيانات (GPC / الاسم) وأعد الطلب.'
            summary['rejected'] += 1
        elif status == 'approved':
            new, msg = Product.EGS_FAILED, 'الكود معتمد لكنه غير فعّال حالياً لدى الضرائب.'
            summary['rejected'] += 1
        else:
            new, msg = Product.EGS_REQUESTED, 'في انتظار موافقة موظفي الضرائب.'
            summary['pending'] += 1
        if (p.egs_status, p.egs_message) != (new, msg):
            p.egs_status, p.egs_message = new, msg
            fields = ['egs_status', 'egs_message'] + (['updated_at'] if isinstance(p, Product) else [])
            p.save(update_fields=fields)
    return summary



# ---------------------------------------------------------------------------
# Category-level EGS codes (one code for a whole group of products)
# ---------------------------------------------------------------------------
def category_egs_code(category, settings_obj):
    return f'EG-{(settings_obj.company_id or "").strip()}-CAT{category.id}'


def request_category_egs_code(category, settings_obj=None, reason='Product group code'):
    """Register one EGS code for a category; its products without a code of their own use it."""
    settings_obj = settings_obj or get_settings()
    errors = []
    if not GPC_RE.match((category.gpc_code or '').strip()):
        errors.append('كود GPC يجب أن يكون 8 أرقام (المستوى الرابع من تصنيف GS1).')
    if not (category.name_en or '').strip():
        errors.append('اسم الفئة بالإنجليزي مطلوب.')
    company = (settings_obj.company_id or '').strip() if settings_obj else ''
    if not (company.isdigit() and len(company) == 9):
        errors.append('رقم التسجيل الضريبي للشركة (9 أرقام) غير مسجل في إعدادات الفاتورة الإلكترونية.')
    if errors:
        return False, ' • '.join(errors)

    code = category_egs_code(category, settings_obj)
    today = timezone.now().astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT00:00:00Z')
    item = {
        'codeType': 'EGS', 'parentCode': category.gpc_code.strip(), 'itemCode': code,
        'codeName': category.name_en.strip()[:200], 'codeNameAr': category.name.strip()[:200],
        'activeFrom': today,
        'description': category.name_en.strip()[:500],
        'descriptionAr': (category.description or category.name or '').strip()[:500],
        'requestReason': reason,
    }
    url = f'{_base_url(settings_obj)}/api/v1.0/codetypes/requests/codes'
    try:
        resp = eta_request('post', settings_obj, url, json={'items': [item]})
    except Exception as e:
        return False, str(e)
    error = _response_error(resp)
    category.egs_requested_at = timezone.now()
    if error:
        category.egs_status = 'FAILED'
        category.egs_message = (friendly(error) + _cid(resp))[:500]
        category.save(update_fields=['egs_status', 'egs_message', 'egs_requested_at'])
        return False, category.egs_message
    category.egs_code = code
    category.egs_status = 'REQUESTED'
    category.egs_message = 'تم إرسال الطلب — في انتظار موافقة موظفي الضرائب.'
    category.save(update_fields=['egs_code', 'egs_status', 'egs_message', 'egs_requested_at'])
    return True, code
