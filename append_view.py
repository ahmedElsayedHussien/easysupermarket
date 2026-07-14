with open('e:/easysupermarket/apps/tenant/einvoicing/views.py', 'a', encoding='utf-8') as f:
    f.write('''

def resend_invoice(request, log_id):
    if request.method == 'POST':
        log = get_object_or_404(EInvoiceLog, id=log_id)
        log.status = 'WAITING_APPROVAL'
        log.signed_payload = None
        log.eta_response = None
        log.save()
        messages.success(request, 'تم إعادة الفاتورة لشاشة الاعتماد بنجاح.')
    return redirect('einvoicing:invoice_history')
''')
