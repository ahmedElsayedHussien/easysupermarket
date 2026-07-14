import requests
from django.utils import timezone
from decimal import Decimal
from .models import TaxIntegrationSettings, EInvoiceLog
from apps.tenant.invoicing.models import Invoice

def build_invoice_json(invoice_id):
    """
    Builds the ETA-compliant JSON structure for a specific invoice.
    """
    invoice = Invoice.objects.get(id=invoice_id)
    settings_obj = TaxIntegrationSettings.objects.first()
    
    if not settings_obj:
        raise Exception("إعدادات الضرائب غير متوفرة")

    # Assuming customer is the partner on the invoice
    receiver_id = getattr(invoice.partner, 'tax_id', '') if invoice.partner else ""
    receiver_name = getattr(invoice.partner, 'name', 'عميل نقدي') if invoice.partner else "عميل نقدي"
    receiver_type = "B" if receiver_id else "P" # Business if tax_id exists, else Person
    
    # Ensure datetime is formatted ISO 8601 (ETA requires Z for UTC)
    issue_date = invoice.date.strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = []
    total_sales = Decimal('0.0')
    total_t1 = Decimal('0.0')
    total_t4 = Decimal('0.0')

    for line in invoice.lines.all():
        net_total = line.subtotal
        qty = line.quantity
        unit_price = line.unit_price
        
        # Determine tax amount for this line (VAT T1)
        tax_amount = line.tax_amount or Decimal('0.0')
        total_t1 += tax_amount
        
        # Item code mapping (prefer gs1_code, fallback to internal SKU or ID)
        item_code = line.product.gs1_code or f"EG-{settings_obj.company_id}-{line.product.id}"
        
        line_data = {
            "description": line.product.name,
            "itemType": "GS1" if line.product.gs1_code else "EGS",
            "itemCode": item_code,
            "unitType": "EA", # Defaulting to Each. Can be mapped from Product.UNIT_CHOICES
            "quantity": float(qty),
            "internalCode": str(line.product.sku or line.product.id),
            "salesTotal": float(qty * unit_price),
            "total": float(net_total + tax_amount),
            "valueDifference": 0.00,
            "totalTaxableFees": 0,
            "netTotal": float(net_total),
            "itemsDiscount": float((qty * unit_price) - net_total),
            "unitValue": {
                "currencySold": "EGP",
                "amountEGP": float(unit_price)
            },
            "discount": {
                "rate": float(line.discount_pct or 0.0),
                "amount": float((qty * unit_price) - net_total)
            },
            "taxableItems": [
                {
                    "taxType": "T1",
                    "amount": float(tax_amount),
                    "subType": "V009",
                    "rate": float(line.tax_rate or 0.0)
                }
            ]
        }
        lines.append(line_data)
        total_sales += (qty * unit_price)

    # Assuming T4 (WHT) is calculated globally on the invoice or lines. 
    # For now, let's look at the invoice level if it has WHT.
    # Invoice model might not have WHT explicitly, so we set T4=0 if not applicable.
    # If there is a WHT percentage logic, we can apply it here.
    
    tax_totals = [
        {"taxType": "T1", "amount": float(total_t1)}
    ]
    
    if total_t4 > 0:
        tax_totals.append({"taxType": "T4", "amount": float(total_t4)})

    net_amount = float(invoice.total_amount - total_t1) # Since total_amount includes VAT usually
    total_amount = float(invoice.total_amount)

    receiver_data = {
        "address": {
            "country": "EG",
            "governate": "Cairo",
            "regionCity": "Cairo",
            "street": "Street",
            "buildingNumber": "1"
        },
        "type": receiver_type,
        "name": receiver_name
    }
    if receiver_id:
        receiver_data["id"] = receiver_id

    invoice_data = {
        "issuer": {
            "address": {
                "branchID": "0",
                "country": "EG",
                "governate": "Cairo",
                "regionCity": "Cairo",
                "street": "Street",
                "buildingNumber": "1"
            },
            "type": "B",
            "id": settings_obj.company_id,
            "name": settings_obj.company_name
        },
        "receiver": receiver_data,
        "documentType": "I",
        "documentTypeVersion": "1.0",
        "dateTimeIssued": issue_date,
        "taxpayerActivityCode": settings_obj.taxpayer_activity_code,
        "internalID": str(invoice.id),
        "invoiceLines": lines,
        "totalDiscountAmount": float(total_sales - Decimal(str(net_amount))),
        "totalSalesAmount": float(total_sales),
        "netAmount": float(net_amount),
        "taxTotals": tax_totals,
        "totalAmount": total_amount,
        "extraDiscountAmount": 0,
        "totalItemsDiscountAmount": 0,
    }
    
    return invoice_data


def get_eta_access_token(settings_obj):
    auth_url = "https://id.eta.gov.eg/connect/token" if settings_obj.is_production else "https://id.preprod.eta.gov.eg/connect/token"
    
    payload = {
        'grant_type': 'client_credentials',
        'client_id': settings_obj.client_id,
        'client_secret': settings_obj.client_secret,
        'scope': 'InvoicingAPI'
    }
    
    response = requests.post(auth_url, data=payload)
    if response.status_code == 200:
        return response.json().get('access_token')
    else:
        raise Exception(f"فشل الحصول على مفتاح المرور: {response.text}")


def submit_to_eta(signed_invoice_data, settings_obj):
    access_token = get_eta_access_token(settings_obj)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {"documents": [signed_invoice_data]}
    
    base_url = "https://api.invoicing.eta.gov.eg" if settings_obj.is_production else "https://api.preprod.invoicing.eta.gov.eg"
    submission_url = f"{base_url}/api/v1.0/documentsubmissions"
    
    eta_response = requests.post(submission_url, json=payload, headers=headers)
    return eta_response
