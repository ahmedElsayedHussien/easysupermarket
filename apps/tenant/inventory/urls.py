from django.urls import path
from . import views

app_name = 'inventory'
urlpatterns = [
    path('stock/', views.stock_list, name='stock_list'),
    path('products/', views.product_list, name='product_list'),
    path('products/create/', views.ProductCreateView.as_view(), name='product_create'),
    path('products/<int:pk>/update/', views.ProductUpdateView.as_view(), name='product_update'),
    path('transfer/', views.transfer_stock, name='transfer_stock'),
    path('api/stock-level/', views.api_stock_level, name='api_stock_level'),
    path('api/products/', views.api_products, name='api_products'),
    path('valuation-report/', views.valuation_report, name='valuation_report'),
    path('expiry-report/', views.expiry_report, name='expiry_report'),
    
    path('warehouses/', views.WarehouseListView.as_view(), name='warehouse_list'),
    path('warehouses/create/', views.WarehouseCreateView.as_view(), name='warehouse_create'),
    path('warehouses/<int:pk>/update/', views.WarehouseUpdateView.as_view(), name='warehouse_update'),
    path('warehouses/<int:pk>/delete/', views.warehouse_delete, name='warehouse_delete'),
    
    path('categories/', views.CategoryListView.as_view(), name='category_list'),
    path('categories/create/', views.CategoryCreateView.as_view(), name='category_create'),
    path('categories/<int:pk>/update/', views.CategoryUpdateView.as_view(), name='category_update'),
    
    path('uom/', views.UnitOfMeasureListView.as_view(), name='uom_list'),
    path('uom/create/', views.UnitOfMeasureCreateView.as_view(), name='uom_create'),
    path('uom/<int:pk>/update/', views.UnitOfMeasureUpdateView.as_view(), name='uom_update'),
    
    path('adjustments/', views.adjustment_list, name='adjustment_list'),
    path('adjustments/create/', views.adjustment_create, name='adjustment_create'),
    path('adjustments/<int:pk>/confirm/', views.adjustment_confirm, name='adjustment_confirm'),
]
