from django.urls import path
from . import views

app_name = 'invoicing'
urlpatterns = [
    path('pos/', views.pos_view, name='pos'),
    path('purchase/create/', views.purchase_invoice_view, name='purchase_invoice'),
    path('sales/', views.sales_invoice_list, name='sales_invoice_list'),
    path('purchases/', views.purchase_invoice_list, name='purchase_invoice_list'),
    path('confirm/<int:invoice_id>/', views.confirm_invoice_view, name='confirm_invoice'),
    path('api/product/barcode/', views.get_product_by_barcode, name='product_by_barcode'),
    path('api/cart/add/', views.add_to_cart, name='add_to_cart'),
    path('api/sale/complete/', views.complete_sale, name='complete_sale'),
    path('invoice/<int:invoice_id>/', views.invoice_detail, name='invoice_detail'),
    path('receipt/<int:invoice_id>/', views.receipt_view, name='receipt'),
    path('shift-report/', views.shift_report, name='shift_report'),
]
