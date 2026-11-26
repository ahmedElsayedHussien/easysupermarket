import secrets
from decimal import Decimal

from django.db import models
from apps.tenant.core.crypto import EncryptedCharField
from apps.tenant.invoicing.models import Invoice

class TaxIntegrationSettings(models.Model):
    client_id = models.CharField(max_length=255, verbose_name="معرف العميل (Client ID)")
    # encrypted at rest (core.crypto): a database backup alone never reveals it
    client_secret = EncryptedCharField(max_length=500, verbose_name="كلمة السر (Client Secret)")
    taxpayer_activity_code = models.CharField(
        max_length=10, blank=True, default="", verbose_name="كود النشاط (Activity Code)",
        help_text="كود النشاط المسجل لك في مصلحة الضرائب (4 أرقام) — مثال: 4711 لمحلات السوبر ماركت.")
    enable_einvoicing = models.BooleanField(default=True, verbose_name="تفعيل المنظومة (إرسال الفواتير)")
    is_production = models.BooleanField(default=False, verbose_name="العملية الفعلية (Production Mode)")
    company_id = models.CharField(max_length=50, blank=True, null=True, verbose_name="رقم التسجيل الضريبي (Issuer ID)")
    # Issuer name comes from Settings > store name (core.company); this is only a fallback.
    company_name = models.CharField(max_length=255, blank=True, null=True, verbose_name="اسم الشركة (Issuer Name)")

    # Intermediary login only: registration number of the taxpayer represented ("onbehalfof")
    on_behalf_of = models.CharField(max_length=20, blank=True, default="",
                                    verbose_name="الدخول كوسيط نيابة عن (رقم التسجيل)",
                                    help_text="اتركه فارغاً إلا إذا كنت تدخل كوسيط (Intermediary) نيابة عن ممول آخر.")

    # --- Issuer address exactly as registered at the Tax Authority ---
    branch_code = models.CharField(max_length=20, default="0", verbose_name="كود الفرع لدى الضرائب (Branch ID)",
                                   help_text="0 للمركز الرئيسي، أو كود الفرع المسجل في بوابة الضرائب.")
    governate = models.CharField(max_length=100, blank=True, default="", verbose_name="المحافظة")
    region_city = models.CharField(max_length=100, blank=True, default="", verbose_name="المدينة / المنطقة")
    street = models.CharField(max_length=200, blank=True, default="", verbose_name="الشارع")
    building_number = models.CharField(max_length=50, blank=True, default="", verbose_name="رقم المبنى")
    postal_code = models.CharField(max_length=20, blank=True, default="", verbose_name="الرقم البريدي (اختياري)")
    floor = models.CharField(max_length=20, blank=True, default="", verbose_name="الدور (اختياري)")
    room = models.CharField(max_length=20, blank=True, default="", verbose_name="رقم المحل / الشقة (اختياري)")
    landmark = models.CharField(max_length=200, blank=True, default="", verbose_name="علامة مميزة (اختياري)")

    # --- Tax codes ---
    wht_subtype = models.CharField(
        max_length=10, blank=True, default="W010", verbose_name="كود نوع الخصم والإضافة (T4)",
        help_text="كود نوع ضريبة الخصم والإضافة من دليل أكواد الضرائب حسب طبيعة التعامل (يُستخدم فقط إذا كانت الفاتورة عليها خصم وإضافة).")
    receiver_id_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('50000'), verbose_name="حد إلزام الرقم القومي للعميل الفرد",
        help_text="فاتورة لعميل فرد بقيمة تساوي أو تزيد عن هذا المبلغ لا تُرسل إلا ومعها الرقم القومي للعميل.")
    # Shared secret used by the Local Signer: "Authorization: Bearer <api_token>".
    api_token = EncryptedCharField(max_length=300, blank=True, default='', editable=False,
                                 verbose_name="مفتاح ربط برنامج التوقيع (API Token)")

    class Meta:
        verbose_name = "إعدادات الفاتورة الإلكترونية"
        verbose_name_plural = "إعدادات الفاتورة الإلكترونية"

    @staticmethod
    def generate_api_token():
        return secrets.token_urlsafe(32)

    def regenerate_api_token(self):
        self.api_token = self.generate_api_token()
        self.save(update_fields=['api_token'])
        return self.api_token

    def save(self, *args, **kwargs):
        # one settings row: a second one would make "which credentials?" ambiguous
        if self._state.adding and TaxIntegrationSettings.objects.exists():
            raise ValueError('إعدادات الفاتورة الإلكترونية صف واحد بس — عدّل الموجود.')
        if not self.api_token:
            self.api_token = self.generate_api_token()
            update_fields = kwargs.get('update_fields')
            if update_fields is not None and 'api_token' not in update_fields:
                kwargs['update_fields'] = list(update_fields) + ['api_token']
        super().save(*args, **kwargs)

    def __str__(self):
        return f"إعدادات الضرائب - {'البيئة الفعلية' if self.is_production else 'البيئة التجريبية'}"


class EInvoiceLog(models.Model):
    STATUS_CHOICES = [
        ('WAITING_APPROVAL', 'في انتظار التأكيد'),
        ('PENDING', 'قيد الانتظار'),
        ('SIGNED', 'تم التوقيع'),
        ('SUBMITTED', 'تم الإرسال للضرائب'),
        ('VALID', 'مقبولة (Valid)'),
        ('INVALID', 'مرفوضة (Invalid)'),
        ('ERROR', 'خطأ في الإرسال'),
        ('REJECTED', 'مرفوضة من المستلم'),
        ('CANCEL_PENDING', 'ملغاة بالبرنامج — مطلوب إلغاؤها لدى الضرائب'),
        ('CANCEL_REQUESTED', 'تم طلب الإلغاء من الضرائب'),
        ('CANCELLED', 'ملغاة لدى الضرائب'),
        ('CANCEL_DECLINED', 'العميل رفض الإلغاء — اعمل مرتجع (إشعار دائن)'),
        ('REJECT_REQUESTED', 'العميل طلب رفض الفاتورة'),
    ]
    
    invoice = models.OneToOneField(Invoice, on_delete=models.PROTECT, related_name='einvoice_log', verbose_name="الفاتورة")
    # 4) the Local Signer / status sync look these up all the time
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='WAITING_APPROVAL', db_index=True,
                              verbose_name="الحالة")
    uuid = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name="UUID الفاتورة بالضرائب")
    submission_id = models.CharField(max_length=255, blank=True, null=True, db_index=True,
                                     verbose_name="رقم الإرسال (Submission ID)")
    # ETA "longId": the long unique id meant for document printouts (anonymous lookup).
    long_id = models.CharField(max_length=100, blank=True, default='', verbose_name="الرقم الطويل للطباعة (Long ID)")
    # Public read-only link to the document at the ETA (from Search Documents: publicUrl)
    public_url = models.URLField(max_length=500, blank=True, default='', verbose_name="رابط الفاتورة لدى الضرائب")
    signed_payload = models.JSONField(blank=True, null=True, verbose_name="ملف التوقيع (JSON)")
    eta_response = models.JSONField(blank=True, null=True, verbose_name="رد مصلحة الضرائب")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ الإنشاء")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="تاريخ التحديث")
    
    class Meta:
        verbose_name = "سجل الفاتورة الإلكترونية"
        verbose_name_plural = "سجل الفواتير الإلكترونية"
        permissions = [
            ('cancel_einvoice', 'إلغاء فاتورة مرسلة لدى الضرائب'),
            ('reject_received_einvoice', 'رفض فاتورة واردة من مورد'),
            ('manage_egs_codes', 'تسجيل أكواد الأصناف EGS'),
        ]

    def __str__(self):
        return f"سجل فاتورة {self.invoice.id} - {self.get_status_display()}"
