from django.urls import reverse_lazy
from django.views.generic import UpdateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from .models import TaxIntegrationSettings, EInvoiceLog
from apps.tenant.core.views import AdminRequiredMixin

class TaxIntegrationSettingsUpdateView(LoginRequiredMixin, AdminRequiredMixin, UpdateView):
    model = TaxIntegrationSettings
    fields = ['enable_einvoicing', 'client_id', 'client_secret', 'taxpayer_activity_code', 'is_production', 'company_id', 'company_name']
    template_name = 'einvoicing/settings.html'
    success_url = reverse_lazy('einvoicing:tax_settings')

    def get_object(self, queryset=None):
        obj, created = TaxIntegrationSettings.objects.get_or_create(id=1)
        return obj

    def form_valid(self, form):
        messages.success(self.request, 'تم حفظ إعدادات الفاتورة الإلكترونية بنجاح.')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'يرجى مراجعة الأخطاء في النموذج.')
        return super().form_invalid(form)

from django.views.generic import ListView
from django.shortcuts import redirect, get_object_or_404

class InvoiceApprovalListView(LoginRequiredMixin, AdminRequiredMixin, ListView):
    model = EInvoiceLog
    template_name = 'einvoicing/invoice_list.html'
    context_object_name = 'logs'

    def get_queryset(self):
        return EInvoiceLog.objects.filter(status='WAITING_APPROVAL').order_by('-created_at')

class EInvoiceHistoryListView(LoginRequiredMixin, AdminRequiredMixin, ListView):
    model = EInvoiceLog
    template_name = 'einvoicing/invoice_history.html'
    context_object_name = 'logs'

    def get_queryset(self):
        # Exclude 'WAITING_APPROVAL' so we only see ones that have entered the pipeline
        return EInvoiceLog.objects.exclude(status='WAITING_APPROVAL').order_by('-updated_at')

def approve_invoice_for_eta(request, log_id):
    if request.method == 'POST':
        log = get_object_or_404(EInvoiceLog, id=log_id)
        if log.status == 'WAITING_APPROVAL':
            log.status = 'PENDING'
            log.save()
            messages.success(request, f'تم إرسال الفاتورة رقم {log.invoice.invoice_number} إلى برنامج التوقيع بنجاح.')
    return redirect('einvoicing:invoice_approval_list')

from .services import submit_to_eta

def confirm_eta_submission(request, log_id):
    if request.method == 'POST':
        log = get_object_or_404(EInvoiceLog, id=log_id)
        if log.status == 'SIGNED' and log.signed_payload:
            try:
                settings_obj = TaxIntegrationSettings.objects.first()
                eta_res = submit_to_eta(log.signed_payload, settings_obj)
                
                if eta_res.status_code in [200, 202]:
                    eta_data = eta_res.json()
                    log.status = 'SUBMITTED'
                    log.submission_id = eta_data.get('submissionId')
                    log.eta_response = eta_data
                    if eta_data.get('rejectedDocuments') and len(eta_data.get('rejectedDocuments')) > 0:
                        log.status = 'INVALID'
                    elif eta_data.get('acceptedDocuments') and len(eta_data.get('acceptedDocuments')) > 0:
                        log.uuid = eta_data['acceptedDocuments'][0].get('uuid')
                        log.status = 'VALID'
                    log.save()
                    messages.success(request, 'تم رفع الفاتورة لمصلحة الضرائب بنجاح!')
                else:
                    log.status = 'ERROR'
                    try:
                        error_json = eta_res.json()
                        log.eta_response = {"error_code": eta_res.status_code, "details": error_json}
                    except ValueError:
                        log.eta_response = {"error_code": eta_res.status_code, "text": eta_res.text}
                    log.save()
                    messages.error(request, 'حدث خطأ أثناء الرفع، يرجى التحقق من التفاصيل.')
            except Exception as e:
                log.status = 'ERROR'
                log.eta_response = {"error_text": str(e)}
                log.save()
                messages.error(request, f'حدث خطأ غير متوقع: {str(e)}')
        else:
            messages.error(request, 'الفاتورة ليست جاهزة للرفع (غير موقعة).')
            
    return redirect('einvoicing:invoice_history')


def resend_invoice(request, log_id):
    if request.method == 'POST':
        log = get_object_or_404(EInvoiceLog, id=log_id)
        log.status = 'WAITING_APPROVAL'
        log.signed_payload = None
        log.eta_response = None
        log.save()
        messages.success(request, 'تم إعادة الفاتورة لشاشة الاعتماد بنجاح.')
    return redirect('einvoicing:invoice_history')
