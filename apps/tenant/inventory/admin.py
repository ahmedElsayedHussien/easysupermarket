from django.contrib import admin
from mptt.admin import MPTTModelAdmin
from .models import Warehouse, Category, Product, InventoryBatch, StockMovement

@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'branch', 'is_cold_storage', 'is_active')

@admin.register(Category)
class CategoryAdmin(MPTTModelAdmin):
    list_display = ('name', 'is_active')

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku', 'barcode', 'category', 'sale_price', 'is_active')
    search_fields = ('name', 'sku', 'barcode')
    list_filter = ('category', 'is_active')

@admin.register(InventoryBatch)
class InventoryBatchAdmin(admin.ModelAdmin):
    list_display = ('product', 'warehouse', 'quantity_remaining', 'unit_cost', 'expiry_date')
    search_fields = ('product__name',)

@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ('product', 'warehouse', 'movement_type', 'quantity', 'created_at')
