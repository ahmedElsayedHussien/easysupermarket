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
                  'is_production', 'company_id',
                  'branch_code', 'governate', 'region_city', 'street', 'building_number',
                  'postal_code', 'floor', 'room', 'landmark',
                  'wht_subtype', 'receiver_id_threshold', 'on_behalf_of']

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
        from .services import issuer_name, settings_errors
        context['issuer_name'] = issuer_name(self.object)
        context['readiness_errors'] = settings_errors(self.object)
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
        return EInvoiceLog.objects.filter(status='WAITING_APPROVAL').select_related('invoice__partner').order_by('-created_at')

class EInvoiceHistoryListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'einvoicing.view_einvoicelog'
    model = EInvoiceLog
    template_name = 'einvoicing/invoice_history.html'
    context_object_name = 'logs'
    paginate_by = 25

    def get_context_data(self, **kwargs):
        import datetime
        context = super().get_context_data(**kwargs)
        context['sync_to'] = datetime.date.today()
        context['sync_from'] = context['sync_to'] - datetime.timedelta(days=6)
        return context

    def get_queryset(self):
        # Exclude 'WAITING_APPROVAL' so we only see ones that have entered the pipeline
        return EInvoiceLog.objects.exclude(status='WAITING_APPROVAL').select_related('invoice__partner').order_by('-updated_at')

@custom_permission_required('einvoicing.change_einvoicelog', redirect_url='core:main_screen')
def approve_invoice_for_eta(request, log_id):
    if request.method == 'POST':
        log = get_object_or_404(EInvoiceLog, id=log_id)
        if log.status == 'WAITING_APPROVAL':
            from .services import ETAValidationError, build_invoice_json
            try:
                log.record_eta_totals(build_invoice_json(log.invoice_id), save=False)
            except ETAValidationError as e:
                messages.error(request, f'الفاتورة {log.invoice.invoice_number} لن تقبلها الضرائب بحالتها — '
                                        + ' • '.join(e.errors))
                return redirect('einvoicing:invoice_approval_list')
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
                    # Official response: submissionUUID (+ acceptedDocuments[uuid, longId, internalId])
                    log.submission_id = eta_data.get('submissionUUID') or eta_data.get('submissionId')
                    from .services import correlation_id
                    log.eta_response = dict(eta_data, correlationId=correlation_id(eta_res))
                    if eta_data.get('rejectedDocuments') and len(eta_data.get('rejectedDocuments')) > 0:
                        log.status = 'INVALID'
                    elif eta_data.get('acceptedDocuments') and len(eta_data.get('acceptedDocuments')) > 0:
                        # Received by the ETA; the final Valid/Invalid comes from its validation.
                        accepted = eta_data['acceptedDocuments'][0]
                        log.uuid = accepted.get('uuid')
                        log.long_id = accepted.get('longId') or ''
                        log.status = 'SUBMITTED'
                    log.save()
                    if log.status == 'SUBMITTED':
                        try:
                            from .services import refresh_status
                            refresh_status(log, settings_obj)
                        except Exception:
                            logger.warning('Status refresh after submit failed for log %s', log.id, exc_info=True)
                    messages.success(request, f'تم رفع الفاتورة لمصلحة الضرائب — الحالة: {log.get_status_display()}.')
                elif eta_res.status_code == 422:
                    # DuplicateSubmission: identical payload within 10 minutes — retry later
                    retry = eta_res.headers.get('Retry-After', '')
                    messages.warning(request, 'الضرائب رفضت الإرسال لأنه مكرر خلال آخر 10 دقائق'
                                     + (f' — أعد المحاولة بعد {retry} ثانية.' if retry else '.')
                                     + ' استخدم "تحديث الحالة" لمعرفة نتيجة الإرسال السابق.')
                    return redirect('einvoicing:invoice_history')
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


@custom_permission_required('einvoicing.change_einvoicelog', redirect_url='core:main_screen')
def refresh_eta_status(request, log_id):
    """Ask the Tax Authority whether the document was accepted (Valid) or not."""
    if request.method == 'POST':
        from .services import refresh_status
        log = get_object_or_404(EInvoiceLog, id=log_id)
        try:
            refresh_status(log)
            messages.success(request, f'{log.invoice.invoice_number}: {log.get_status_display()}')
        except Exception as e:
            logger.warning('ETA status refresh failed for log %s', log.id, exc_info=True)
            messages.error(request, f'تعذر الاستعلام من الضرائب: {e}')
    return redirect('einvoicing:invoice_history')


@custom_permission_required('einvoicing.change_einvoicelog', redirect_url='core:main_screen')
def refresh_all_eta_status(request):
    if request.method == 'POST':
        from .services import refresh_status
        done = failed = 0
        for log in EInvoiceLog.objects.filter(status__in=['SUBMITTED', 'CANCEL_REQUESTED']).order_by('id')[:100]:
            try:
                refresh_status(log)
                done += 1
            except Exception:
                logger.warning('ETA status refresh failed for log %s', log.id, exc_info=True)
                failed += 1
        messages.success(request, f'تم تحديث حالة {done} فاتورة' + (f'، وتعذر {failed}' if failed else '') + '.')
    return redirect('einvoicing:invoice_history')


@custom_permission_required('einvoicing.cancel_einvoice', redirect_url='core:main_screen')
def cancel_eta_document(request, log_id):
    """Cancel at the Tax Authority a document whose invoice was cancelled in the program."""
    if request.method == 'POST':
        from .services import cancel_on_eta
        log = get_object_or_404(EInvoiceLog, id=log_id)
        if log.status not in ('CANCEL_PENDING', 'VALID', 'SUBMITTED'):
            messages.error(request, 'هذه الفاتورة ليست في حالة تسمح بطلب الإلغاء.')
        else:
            try:
                cancel_on_eta(log, request.POST.get('reason', ''))
                messages.success(request, f'تم طلب إلغاء الفاتورة {log.invoice.invoice_number} لدى الضرائب.')
            except Exception as e:
                logger.warning('ETA cancel failed for log %s', log.id, exc_info=True)
                messages.error(request, f'تعذر طلب الإلغاء: {e}')
    return redirect('einvoicing:invoice_history')


@custom_permission_required('einvoicing.change_einvoicelog', redirect_url='core:main_screen')
def sync_eta_documents(request):
    """Reconcile the log with the Tax Authority records for a period (Search Documents API)."""
    if request.method == 'POST':
        import datetime
        from django.utils.dateparse import parse_date
        from .services import sync_with_eta
        today = datetime.date.today()
        date_from = parse_date(request.POST.get('date_from') or '') or today - datetime.timedelta(days=6)
        date_to = parse_date(request.POST.get('date_to') or '') or today
        try:
            s = sync_with_eta(date_from, date_to)
            messages.success(request, f'مطابقة {date_from} إلى {date_to}: راجعنا {s["checked"]} مستند لدى الضرائب، '
                                      f'وتم تحديث {s["updated"]}.')
            if s['unknown']:
                messages.warning(request, f'مستندات لدى الضرائب غير موجودة في البرنامج ({len(s["unknown"])}): '
                                 + '، '.join(s['unknown'][:15]))
        except ValueError as e:
            messages.error(request, str(e))
        except Exception as e:
            logger.warning('ETA sync failed', exc_info=True)
            messages.error(request, f'تعذرت المطابقة مع الضرائب: {e}')
    return redirect('einvoicing:invoice_history')


def _received_period(params):
    import datetime
    from django.utils.dateparse import parse_date
    today = datetime.date.today()
    date_from = parse_date(params.get('date_from') or '') or today - datetime.timedelta(days=6)
    date_to = parse_date(params.get('date_to') or '') or today
    return date_from, date_to


@custom_permission_required('einvoicing.view_einvoicelog', redirect_url='core:main_screen')
def received_documents_view(request):
    """Invoices suppliers issued to us at the Tax Authority, with rejection of wrong ones."""
    from django.shortcuts import render
    from .services import REJECT_REASONS, received_documents
    date_from, date_to = _received_period(request.GET)
    docs, error = [], ''
    if request.GET.get('load'):
        try:
            docs = received_documents(date_from, date_to)
        except ValueError as e:
            error = str(e)
        except Exception as e:
            logger.warning('ETA received search failed', exc_info=True)
            error = f'تعذر جلب الفواتير الواردة من الضرائب: {e}'
    return render(request, 'einvoicing/received.html', {
        'docs': docs, 'error': error, 'loaded': bool(request.GET.get('load')),
        'date_from': date_from, 'date_to': date_to, 'reject_reasons': REJECT_REASONS,
        'title': 'الفواتير الواردة من الموردين',
    })


@custom_permission_required('einvoicing.reject_received_einvoice', redirect_url='core:main_screen')
def reject_received_document(request):
    from django.urls import reverse
    from .services import reject_document
    date_from, date_to = _received_period(request.POST)
    if request.method == 'POST':
        internal = request.POST.get('internal_id') or request.POST.get('uuid')
        try:
            reject_document(request.POST.get('uuid'), request.POST.get('reason'))
            messages.success(request, f'تم رفض الفاتورة {internal} لدى الضرائب — سيُبلَّغ المورد.')
        except Exception as e:
            logger.warning('ETA reject failed', exc_info=True)
            messages.error(request, f'تعذر رفض الفاتورة {internal}: {e}')
    return redirect(f"{reverse('einvoicing:received_documents')}?load=1&date_from={date_from}&date_to={date_to}")


EGS_BATCH_LIMIT = 50


@custom_permission_required('einvoicing.manage_egs_codes', redirect_url='core:main_screen')
def egs_codes_view(request):
    """Register products' EGS codes at the Tax Authority (Create EGS Code Usage API)."""
    from django.core.paginator import Paginator
    from django.db.models import Q
    from django.shortcuts import render
    from apps.tenant.inventory.models import Product
    from .egs import egs_code_for, request_egs_code
    settings_obj = TaxIntegrationSettings.objects.first()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'sync':
            from .egs import sync_egs_requests
            try:
                s = sync_egs_requests(settings_obj)
                messages.success(request, f'تم تحديث {s["checked"]} صنف من الضرائب: معتمد {s["approved"]}، '
                                          f'مرفوض {s["rejected"]}، قيد الموافقة {s["pending"]}.')
            except Exception as e:
                logger.warning('EGS requests sync failed', exc_info=True)
                messages.error(request, f'تعذر تحديث حالات الأكواد: {e}')
            return redirect(request.get_full_path())
        if action == 'request_categories':
            from apps.tenant.inventory.models import Category
            from .egs import request_category_egs_code
            ids = [i for i in request.POST.getlist('cat_selected') if i.isdigit()][:EGS_BATCH_LIMIT]
            ok = 0
            for c in Category.objects.filter(pk__in=ids):
                c.gpc_code = (request.POST.get(f'cat_gpc_{c.id}') or c.gpc_code or '').strip()
                c.name_en = (request.POST.get(f'cat_name_en_{c.id}') or c.name_en or '').strip()
                c.save(update_fields=['gpc_code', 'name_en'])
                success, msg = request_category_egs_code(c, settings_obj)
                if success:
                    ok += 1
                else:
                    messages.error(request, f'فئة {c.name}: {msg}')
            if ok:
                messages.success(request, f'تم إرسال {ok} طلب كود فئة — في انتظار موافقة الضرائب.')
            elif not ids:
                messages.warning(request, 'اختر فئة واحدة على الأقل.')
            return redirect(request.get_full_path())
        if action == 'approve_category':
            from apps.tenant.inventory.models import Category
            c = get_object_or_404(Category, pk=request.POST.get('category_id'))
            c.egs_status = 'APPROVED'
            c.egs_message = 'تم تأكيد اعتماد الكود من بوابة الضرائب.'
            c.save(update_fields=['egs_status', 'egs_message'])
            messages.success(request, f'تم اعتماد كود الفئة {c.name}.')
            return redirect(request.get_full_path())
        if action == 'approve':
            p = get_object_or_404(Product, pk=request.POST.get('product_id'))
            p.egs_status = Product.EGS_APPROVED
            p.egs_message = 'تم تأكيد اعتماد الكود من بوابة الضرائب.'
            p.save(update_fields=['egs_status', 'egs_message', 'updated_at'])
            messages.success(request, f'تم اعتماد كود {p.egs_code} للصنف {p.name}.')
        elif action == 'request':
            ids = [i for i in request.POST.getlist('selected') if i.isdigit()][:EGS_BATCH_LIMIT]
            if not ids:
                messages.warning(request, 'اختر صنفاً واحداً على الأقل.')
            ok = failed = 0
            for p in Product.objects.filter(pk__in=ids):
                p.gpc_code = (request.POST.get(f'gpc_{p.id}') or p.gpc_code or '').strip()
                p.name_en = (request.POST.get(f'name_en_{p.id}') or p.name_en or '').strip()
                p.save(update_fields=['gpc_code', 'name_en', 'updated_at'])
                success, msg = request_egs_code(p, settings_obj)
                if success:
                    ok += 1
                else:
                    failed += 1
                    messages.error(request, f'{p.name}: {msg}')
            if ok:
                messages.success(request, f'تم إرسال {ok} طلب تسجيل كود — في انتظار موافقة الضرائب.')
        return redirect(request.get_full_path())

    q = (request.GET.get('q') or '').strip()
    show = request.GET.get('show') or 'missing'
    qs = Product.objects.filter(is_active=True).order_by('name')
    if show == 'pending':
        qs = qs.filter(egs_status__in=[Product.EGS_REQUESTED, Product.EGS_FAILED])
    else:
        qs = qs.filter(
            Q(egs_code__isnull=True) | Q(egs_code='') | Q(egs_status__in=[Product.EGS_REQUESTED, Product.EGS_FAILED]))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(barcode__icontains=q) | Q(sku__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    from .services import category_code_holder
    from .egs import category_egs_code
    from apps.tenant.inventory.models import Category
    for p in page:
        p.proposed_egs = egs_code_for(p, settings_obj) if settings_obj and settings_obj.company_id else ''
        p.covered_by = category_code_holder(p)
    categories = list(Category.objects.filter(is_active=True).order_by('tree_id', 'lft'))
    for c in categories:
        c.proposed_egs = category_egs_code(c, settings_obj) if settings_obj and settings_obj.company_id else ''
    return render(request, 'einvoicing/egs_codes.html', {
        'page_obj': page, 'q': q, 'show': show, 'batch_limit': EGS_BATCH_LIMIT, 'categories': categories,
        'company_ready': bool(settings_obj and (settings_obj.company_id or '').strip()),
        'title': 'تسجيل أكواد الأصناف لدى الضرائب',
    })
