from django.urls import path
from . import views

app_name = 'partners'
urlpatterns = [
    path('suppliers/', views.supplier_list, name='supplier_list'),
    path('customers/', views.customer_list, name='customer_list'),
    path('create/', views.partner_create, name='partner_create'),
    path('<int:pk>/edit/', views.partner_edit, name='partner_edit'),
    path('<int:pk>/ledger/', views.partner_ledger, name='partner_ledger'),
    path('payment/post/', views.post_payment_view, name='post_payment'),
]
