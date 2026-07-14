from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
from .models import TaxIntegrationSettings, EInvoiceLog
from apps.tenant.invoicing.models import Invoice
from .services import build_invoice_json, submit_to_eta

def get_pending_invoices(request):
    """
    API endpoint for Local Signer to get invoices that are ready to be signed.
    We look for invoices that are confirmed ('SALE' type) and not yet submitted.
    """
    # Assuming 'status' for confirmed invoice is 'CONFIRMED' in Invoice model
    # And we check EInvoiceLog for status
    pending_logs = EInvoiceLog.objects.filter(status='PENDING')
    
    pending_list = []
    for log in pending_logs:
        try:
            invoice_data = build_invoice_json(log.invoice.id)
            pending_list.append({
                "id": log.invoice.id,
                "data": invoice_data
            })
        except Exception as e:
            print(f"Error building JSON for Invoice {log.invoice.id}: {str(e)}")
            continue

    return JsonResponse(pending_list, safe=False)

@csrf_exempt
def submit_signed_invoice(request, invoice_id):
    """
    API endpoint for Local Signer to post the signed CAdES-BES JSON back,
    and then Django submits it to ETA servers.
    """
    if request.method != 'POST':
        return JsonResponse({"error": "Only POST allowed"}, status=405)
        
    try:
        signed_json = json.loads(request.body)
        log = EInvoiceLog.objects.get(invoice__id=invoice_id)
        settings_obj = TaxIntegrationSettings.objects.first()
        
        # 1. Update log status to SIGNED and save payload
        log.status = 'SIGNED'
        log.signed_payload = signed_json
        log.save()
        
        # User requested to NOT submit automatically. We return success here.
        return JsonResponse({"status": "success", "message": "Invoice signed successfully. Awaiting manual submission."})
            
    except Exception as e:
        if 'log' in locals():
            log.status = 'ERROR'
            log.eta_response = {"error_text": str(e)}
            log.save()
        return JsonResponse({"status": "error", "message": str(e)}, status=500)
