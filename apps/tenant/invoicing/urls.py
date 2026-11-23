from django.urls import path
from . import views

app_name = 'invoicing'
urlpatterns = [
    path('pos/', views.pos_view, name='pos'),
    path('pos/open-shift/', views.pos_open_shift, name='pos_open_shift'),
    path('pos/close-shift/', views.pos_close_shift, name='pos_close_shift'),
    path('pos/api/shift-stats/', views.pos_shift_stats_api, name='pos_shift_stats_api'),
    path('purchase/create/', views.purchase_invoice_view, name='purchase_invoice'),
    path('sales/', views.sales_invoice_list, name='sales_invoice_list'),
    path('purchases/', views.purchase_invoice_list, name='purchase_invoice_list'),
    path('confirm/<int:invoice_id>/', views.confirm_invoice_view, name='confirm_invoice'),
    path('api/product/barcode/', views.get_product_by_barcode, name='product_by_barcode'),
    path('api/product/serials/', views.get_available_serials, name='get_available_serials'),
    path('api/product/last-prices/', views.get_product_last_prices, name='get_product_last_prices'),
    path('api/cart/add/', views.add_to_cart, name='add_to_cart'),
    path('api/sale/complete/', views.complete_sale, name='complete_sale'),
    path('api/recent-transactions/', views.recent_transactions_api, name='recent_transactions_api'),
    path('api/invoice/<int:invoice_id>/return/', views.process_invoice_return, name='process_invoice_return'),
    path('invoice/<int:invoice_id>/', views.invoice_detail, name='invoice_detail'),
    path('invoice/<int:invoice_id>/update-payment/', views.update_invoice_payment_method_view, name='update_invoice_payment_method'),
    path('invoice/<int:invoice_id>/cancel/', views.cancel_invoice_view, name='cancel_invoice'),
    path('receipt/<int:invoice_id>/', views.receipt_view, name='receipt'),
    path('shift-report/', views.shift_report, name='shift_report'),
    path('sales-returns/', views.sales_returns_dashboard, name='sales_returns_dashboard'),
    path('sales-returns/create-standalone/', views.create_standalone_sales_return, name='create_standalone_sales_return'),

    # Quotation URLs
    path('quotations/', views.quotation_list, name='quotation_list'),
    path('quotations/create/', views.quotation_create, name='quotation_create'),
    path('quotations/<int:quotation_id>/', views.quotation_detail, name='quotation_detail'),
    path('quotations/<int:quotation_id>/edit/', views.quotation_edit, name='quotation_edit'),
    path('quotations/<int:quotation_id>/delete/', views.quotation_delete, name='quotation_delete'),
    path('quotations/<int:quotation_id>/confirm/', views.quotation_confirm, name='quotation_confirm'),
    path('quotations/<int:quotation_id>/convert/', views.quotation_convert, name='quotation_convert'),
    path('quotations/<int:quotation_id>/print/', views.quotation_print, name='quotation_print'),
]
