"""
API endpoints used by the Local Signer desktop app.

Authentication: ``Authorization: Bearer <TaxIntegrationSettings.api_token>``
(compared in constant time). No session/cookie auth is used, so CSRF
exemption on the POST endpoint is safe.
"""
import base64
import binascii
import hmac
import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import TaxIntegrationSettings, EInvoiceLog
from .services import build_invoice_json

logger = logging.getLogger(__name__)

MAX_PENDING_BATCH = 50


def _authenticate(request):
    """Return the settings object if the bearer token is valid, else None."""
    settings_obj = TaxIntegrationSettings.objects.first()
    if not settings_obj or not settings_obj.api_token:
        return None
    header = request.META.get('HTTP_AUTHORIZATION', '')
    scheme, _, token = header.partition(' ')
    if scheme.lower() != 'bearer' or not token.strip():
        return None
    if not hmac.compare_digest(token.strip().encode('utf-8'), settings_obj.api_token.encode('utf-8')):
        return None
    return settings_obj


def _unauthorized():
    resp = JsonResponse({'status': 'error', 'message': 'unauthorized'}, status=401)
    resp['WWW-Authenticate'] = 'Bearer'
    return resp


@require_GET
def get_pending_invoices(request):
    """Invoices approved for signing (EInvoiceLog.status == 'PENDING')."""
    if _authenticate(request) is None:
        return _unauthorized()

    pending_logs = (EInvoiceLog.objects.filter(status='PENDING')
                    .select_related('invoice').order_by('id')[:MAX_PENDING_BATCH])
    pending_list = []
    for log in pending_logs:
        try:
            pending_list.append({'id': log.invoice_id, 'data': build_invoice_json(log.invoice_id)})
        except Exception:
            logger.exception('Error building ETA JSON for invoice %s', log.invoice_id)
            continue
    return JsonResponse(pending_list, safe=False)


def _normalize(obj):
    """Round-trip through JSON so Python floats/str compare like the posted body."""
    return json.loads(json.dumps(obj))


def _valid_signatures(signatures):
    if not isinstance(signatures, list) or not signatures:
        return False
    for sig in signatures:
        if not isinstance(sig, dict) or set(sig.keys()) != {'signatureType', 'value'}:
            return False
        if sig['signatureType'] != 'I' or not isinstance(sig['value'], str) or not sig['value']:
            return False
        try:
            der = base64.b64decode(sig['value'], validate=True)
        except (binascii.Error, ValueError):
            return False
        # CMS ContentInfo is a DER SEQUENCE; reject obvious placeholders.
        if len(der) < 256 or der[0] != 0x30:
            return False
    return True


@csrf_exempt  # token-authenticated machine endpoint, no cookie auth
@require_POST
def submit_signed_invoice(request, invoice_id):
    """
    The Local Signer posts the signed document back. Accepted only when the
    invoice is PENDING and the document equals what the server would generate
    for it (apart from the signatures). Submission to ETA stays manual.
    """
    if _authenticate(request) is None:
        return _unauthorized()

    try:
        signed_json = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'message': 'invalid JSON'}, status=400)
    if not isinstance(signed_json, dict):
        return JsonResponse({'status': 'error', 'message': 'invalid document'}, status=400)

    try:
        with transaction.atomic():
            log = (EInvoiceLog.objects.select_for_update()
                   .filter(invoice_id=invoice_id).first())
            if log is None:
                return JsonResponse({'status': 'error', 'message': 'not found'}, status=404)
            if log.status != 'PENDING':
                return JsonResponse({'status': 'error', 'message': 'invoice is not pending signature'}, status=409)

            document = dict(signed_json)
            signatures = document.pop('signatures', None)
            if not _valid_signatures(signatures):
                return JsonResponse({'status': 'error', 'message': 'invalid signatures'}, status=400)
            expected = _normalize(build_invoice_json(invoice_id))
            if document != expected:
                logger.warning('Signed document for invoice %s does not match the server-built document', invoice_id)
                return JsonResponse({'status': 'error', 'message': 'document does not match invoice'}, status=400)

            payload = dict(expected)
            payload['signatures'] = signatures
            log.status = 'SIGNED'
            log.signed_payload = payload
            log.save(update_fields=['status', 'signed_payload', 'updated_at'])
    except Exception:
        logger.exception('Failed to store signed invoice %s', invoice_id)
        return JsonResponse({'status': 'error', 'message': 'server error'}, status=500)

    return JsonResponse({'status': 'success', 'message': 'Invoice signed successfully. Awaiting manual submission.'})
