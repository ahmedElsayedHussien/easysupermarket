from django.contrib import admin
from mptt.admin import MPTTModelAdmin
from .models import Account, JournalEntry, JournalItem

class JournalItemInline(admin.TabularInline):
    model = JournalItem
    extra = 1

@admin.register(Account)
class AccountAdmin(MPTTModelAdmin):
    list_display = ('code', 'name', 'account_type', 'is_active')
    search_fields = ('code', 'name')
    list_filter = ('account_type', 'is_active')

@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ('reference', 'date', 'status', 'description')
    list_filter = ('status', 'date')
    search_fields = ('reference', 'description')
    inlines = [JournalItemInline]

@admin.register(JournalItem)
class JournalItemAdmin(admin.ModelAdmin):
    list_display = ('entry', 'account', 'debit', 'credit')
