from django.contrib import admin
from .models import Invoice, InvoiceLine, Quotation, QuotationLine

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

class QuotationLineInline(admin.TabularInline):
    model = QuotationLine
    extra = 1

@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ('quotation_number', 'partner', 'date', 'valid_until', 'total_amount', 'status')
    list_filter = ('status', 'date')
    search_fields = ('quotation_number', 'partner__name')
    inlines = [QuotationLineInline]

@admin.register(QuotationLine)
class QuotationLineAdmin(admin.ModelAdmin):
    list_display = ('quotation', 'product', 'quantity', 'unit_price', 'total_amount')

