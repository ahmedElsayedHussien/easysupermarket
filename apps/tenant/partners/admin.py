from django.contrib import admin
from .models import Partner, Payment

@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    list_display = ('name', 'partner_type', 'phone', 'is_active', 'outstanding_balance')
    list_filter = ('partner_type', 'is_active')
    search_fields = ('name', 'phone')

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('reference', 'partner', 'payment_type', 'amount', 'date', 'status')
    list_filter = ('payment_type', 'status', 'date')
    search_fields = ('reference', 'partner__name')
