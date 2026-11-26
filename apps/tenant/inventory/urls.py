from django.urls import path
from . import views
from . import dashboard_views
from . import stock_transfer_views

app_name = 'inventory'
urlpatterns = [
    path('dashboard/', dashboard_views.inventory_dashboard, name='dashboard'),
    path('stock/', views.stock_list, name='stock_list'),
    path('products/', views.product_list, name='product_list'),
    path('products/bulk-import/', views.product_bulk_import, name='product_bulk_import'),
    path('products/bulk-import/template/', views.product_template_download, name='product_template_download'),
    path('products/create/', views.ProductCreateView.as_view(), name='product_create'),
    path('products/<int:pk>/update/', views.ProductUpdateView.as_view(), name='product_update'),
    path('products/<int:pk>/opening-stock/', views.product_opening_stock_view, name='product_opening_stock'),
    path('api/products/<int:pk>/opening-stock/', views.api_product_opening_stock, name='api_product_opening_stock'),
    path('opening-stock/', views.opening_stock_list_view, name='opening_stock_list'),
    path('transfer/', stock_transfer_views.stock_transfer_create, name='transfer_stock'),
    path('transfers/', stock_transfer_views.stock_transfer_list, name='stock_transfer_list'),
    path('transfers/<int:pk>/', stock_transfer_views.stock_transfer_detail, name='stock_transfer_detail'),
    path('api/stock-level/', views.api_stock_level, name='api_stock_level'),
    path('api/warehouse-stock/', stock_transfer_views.warehouse_stock_api, name='api_warehouse_stock'),
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
    
    path('stocktake/', views.stocktake_create, name='stocktake'),
    path('stocktake/sheet/', views.stocktake_sheet, name='stocktake_sheet'),
    path('adjustments/', views.adjustment_list, name='adjustment_list'),
    path('adjustments/create/', views.adjustment_create, name='adjustment_create'),
    path('adjustments/<int:pk>/', views.adjustment_detail, name='adjustment_detail'),
    path('adjustments/<int:pk>/confirm/', views.adjustment_confirm, name='adjustment_confirm'),
    path('adjustments/<int:pk>/delete/', views.adjustment_delete, name='adjustment_delete'),
]
