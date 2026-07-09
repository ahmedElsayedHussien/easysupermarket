from django.contrib import admin
from .models import *

@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('id', 'schema_name', 'name', 'owner_name', 'owner_email',)

@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ('id', 'domain', 'tenant', 'is_primary',)

