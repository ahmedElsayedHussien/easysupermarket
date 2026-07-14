from django.db import models
from apps.tenant.invoicing.models import Invoice

class TaxIntegrationSettings(models.Model):
    client_id = models.CharField(max_length=255, verbose_name="معرف العميل (Client ID)")
    client_secret = models.CharField(max_length=255, verbose_name="كلمة السر (Client Secret)")
    taxpayer_activity_code = models.CharField(max_length=10, default="4741", verbose_name="كود النشاط (Activity Code)")
    is_production = models.BooleanField(default=False, verbose_name="العملية الفعلية (Production Mode)")
    company_id = models.CharField(max_length=50, blank=True, null=True, verbose_name="رقم التسجيل الضريبي (Issuer ID)")
    company_name = models.CharField(max_length=255, blank=True, null=True, verbose_name="اسم الشركة (Issuer Name)")
    
    class Meta:
        verbose_name = "إعدادات الفاتورة الإلكترونية"
        verbose_name_plural = "إعدادات الفاتورة الإلكترونية"

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
    ]
    
    invoice = models.OneToOneField(Invoice, on_delete=models.CASCADE, related_name='einvoice_log', verbose_name="الفاتورة")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='WAITING_APPROVAL', verbose_name="الحالة")
    uuid = models.CharField(max_length=255, blank=True, null=True, verbose_name="UUID الفاتورة بالضرائب")
    submission_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="رقم الإرسال (Submission ID)")
    signed_payload = models.JSONField(blank=True, null=True, verbose_name="ملف التوقيع (JSON)")
    eta_response = models.JSONField(blank=True, null=True, verbose_name="رد مصلحة الضرائب")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ الإنشاء")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="تاريخ التحديث")
    
    class Meta:
        verbose_name = "سجل الفاتورة الإلكترونية"
        verbose_name_plural = "سجل الفواتير الإلكترونية"

    def __str__(self):
        return f"سجل فاتورة {self.invoice.id} - {self.get_status_display()}"
