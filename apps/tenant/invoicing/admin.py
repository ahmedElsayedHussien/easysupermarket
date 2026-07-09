from django.contrib import admin
from .models import Invoice, InvoiceLine

class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 1

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'invoice_type', 'date', 'partner', 'total_amount', 'status')
    list_filter = ('invoice_type', 'status', 'date')
    search_fields = ('invoice_number', 'partner__name')
    inlines = [InvoiceLineInline]

@admin.register(InvoiceLine)
class InvoiceLineAdmin(admin.ModelAdmin):
    list_display = ('invoice', 'product', 'quantity', 'unit_price', 'subtotal')
