from django.urls import path
from . import views

app_name = 'pricing'

urlpatterns = [
    # Sectors (قطاعات العملاء)
    path('sectors/', views.sector_list, name='sector_list'),
    path('sectors/create/', views.sector_create, name='sector_create'),
    path('sectors/<int:pk>/edit/', views.sector_edit, name='sector_edit'),
    path('sectors/<int:pk>/delete/', views.sector_delete, name='sector_delete'),

    # Price Lists (قوائم الأسعار)
    path('pricelists/', views.pricelist_list, name='pricelist_list'),
    path('pricelists/create/', views.pricelist_create, name='pricelist_create'),
    path('pricelists/<int:pk>/', views.pricelist_detail, name='pricelist_detail'),
    path('pricelists/<int:pk>/edit/', views.pricelist_edit, name='pricelist_edit'),
    path('pricelists/<int:pk>/delete/', views.pricelist_delete, name='pricelist_delete'),

    # Price List Items
    path('pricelists/<int:pk>/items/add/', views.pricelist_item_add, name='pricelist_item_add'),
    path('pricelists/<int:pk>/items/<int:item_id>/delete/', views.pricelist_item_delete, name='pricelist_item_delete'),

    # Dynamic Pricing API (for Invoices & POS)
    path('api/resolve-price/', views.api_resolve_price, name='api_resolve_price'),
    path('api/customer-prices/', views.api_customer_prices, name='api_customer_prices'),
]
