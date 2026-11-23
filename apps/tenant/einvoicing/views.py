from django.urls import reverse_lazy
from django.views.generic import UpdateView, ListView
from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from django import forms
import logging

logger = logging.getLogger(__name__)
from .models import TaxIntegrationSettings, EInvoiceLog
from apps.tenant.core.mixins import CustomPermissionRequiredMixin
from apps.tenant.core.decorators import custom_permission_required

class TaxIntegrationSettingsForm(forms.ModelForm):
    client_secret = forms.CharField(
        label="كلمة السر (Client Secret)",
        required=False,
        max_length=255,
        widget=forms.PasswordInput(render_value=False),
        help_text="اتركه فارغاً للإبقاء على كلمة السر الحالية.",
    )

    class Meta:
        model = TaxIntegrationSettings
        fields = ['enable_einvoicing', 'client_id', 'client_secret', 'taxpayer_activity_code',
                  'is_production', 'company_id', 'company_name']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._old_secret = self.instance.client_secret if self.instance and self.instance.pk else ''

    def clean_client_secret(self):
        value = self.cleaned_data.get('client_secret') or ''
        if not value:
            if not self._old_secret:
                raise forms.ValidationError('كلمة السر مطلوبة.')
            return self._old_secret  # keep the stored secret
        return value


class TaxIntegrationSettingsUpdateView(CustomPermissionRequiredMixin, UpdateView):
    permission_required = 'einvoicing.change_taxintegrationsettings'
    model = TaxIntegrationSettings
    form_class = TaxIntegrationSettingsForm
    template_name = 'einvoicing/settings.html'
    success_url = reverse_lazy('einvoicing:tax_settings')

    def get_object(self, queryset=None):
        obj, created = TaxIntegrationSettings.objects.get_or_create(id=1)
        if not obj.api_token:
            obj.save(update_fields=['api_token'])
        return obj

    def post(self, request, *args, **kwargs):
        if request.POST.get('action') == 'regenerate_token':
            self.get_object().regenerate_api_token()
            messages.success(request, 'تم توليد مفتاح ربط جديد لبرنامج التوقيع. حدّث ملف config.ini في جهاز التوقيع.')
            return redirect(self.success_url)
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['api_token'] = self.object.api_token
        context['has_client_secret'] = bool(self.object.client_secret)
        return context

    def form_valid(self, form):
        messages.success(self.request, 'تم حفظ إعدادات الفاتورة الإلكترونية بنجاح.')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'يرجى مراجعة الأخطاء في النموذج.')
        return super().form_invalid(form)

class InvoiceApprovalListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'einvoicing.view_einvoicelog'
    model = EInvoiceLog
    template_name = 'einvoicing/invoice_list.html'
    context_object_name = 'logs'
    paginate_by = 25

    def get_queryset(self):
        return EInvoiceLog.objects.filter(status='WAITING_APPROVAL').order_by('-created_at')

class EInvoiceHistoryListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'einvoicing.view_einvoicelog'
    model = EInvoiceLog
    template_name = 'einvoicing/invoice_history.html'
    context_object_name = 'logs'
    paginate_by = 25

    def get_queryset(self):
        # Exclude 'WAITING_APPROVAL' so we only see ones that have entered the pipeline
        return EInvoiceLog.objects.exclude(status='WAITING_APPROVAL').order_by('-updated_at')

@custom_permission_required('einvoicing.change_einvoicelog', redirect_url='core:main_screen')
def approve_invoice_for_eta(request, log_id):
    if request.method == 'POST':
        log = get_object_or_404(EInvoiceLog, id=log_id)
        if log.status == 'WAITING_APPROVAL':
            log.status = 'PENDING'
            log.save()
            messages.success(request, f'تم إرسال الفاتورة رقم {log.invoice.invoice_number} إلى برنامج التوقيع بنجاح.')
    return redirect('einvoicing:invoice_approval_list')

from .services import submit_to_eta

@custom_permission_required('einvoicing.change_einvoicelog', redirect_url='core:main_screen')
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
            except Exception:
                logger.exception('ETA submission failed for log %s', log.id)
                log.status = 'ERROR'
                log.eta_response = {"error_text": "حدث خطأ أثناء الاتصال بمنظومة الضرائب. راجع سجل الخادم."}
                log.save()
                messages.error(request, 'حدث خطأ غير متوقع أثناء الرفع لمصلحة الضرائب.')
        else:
            messages.error(request, 'الفاتورة ليست جاهزة للرفع (غير موقعة).')
            
    return redirect('einvoicing:invoice_history')

@custom_permission_required('einvoicing.change_einvoicelog', redirect_url='core:main_screen')
def resend_invoice(request, log_id):
    if request.method == 'POST':
        log = get_object_or_404(EInvoiceLog, id=log_id)
        log.status = 'WAITING_APPROVAL'
        log.signed_payload = None
        log.eta_response = None
        log.save()
        messages.success(request, 'تم إعادة الفاتورة لشاشة الاعتماد بنجاح.')
    return redirect('einvoicing:invoice_history')
