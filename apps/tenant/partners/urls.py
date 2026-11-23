from django.urls import path
from . import views
from . import dashboard_views

app_name = 'partners'
urlpatterns = [
    path('dashboard/', dashboard_views.partners_dashboard, name='dashboard'),
    path('suppliers/', views.supplier_list, name='supplier_list'),
    path('customers/', views.customer_list, name='customer_list'),
    path('create/', views.partner_create, name='partner_create'),
    path('<int:pk>/edit/', views.partner_edit, name='partner_edit'),
    path('ledger/', views.partner_ledger_index, name='partner_ledger_index'),
    path('<int:pk>/ledger/', views.partner_ledger, name='partner_ledger'),
    path('<int:pk>/ledger/export-excel/', views.partner_ledger_export_excel, name='partner_ledger_export_excel'),
    path('payment/post/', views.post_payment_view, name='post_payment'),
]
