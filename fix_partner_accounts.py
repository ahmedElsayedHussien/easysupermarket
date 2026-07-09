import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from apps.tenant.partners.models import Partner
from apps.tenant.accounting.models import Account, JournalItem
from apps.tenant.services.journal_service import _get_system_account

parent_payable = _get_system_account('2110')
parent_receivable = _get_system_account('1210')

for partner in Partner.objects.all():
    # Fix supplier account
    if partner.is_supplier and getattr(partner.payable_account, 'code', None) == '2110':
        acc, created = Account.objects.get_or_create(
            code=f"{parent_payable.code}-{partner.id}",
            defaults={
                'name': partner.name,
                'account_type': parent_payable.account_type,
                'parent': parent_payable
            }
        )
        partner.payable_account = acc
        if getattr(partner.account, 'code', None) == '2110':
            partner.account = acc
        partner.save(update_fields=['payable_account', 'account'])
        
        # Migrate journal items that hit 2110 and have this partner's name in the description
        # (This is a heuristic since we don't have partner_id on JournalItem)
        items = JournalItem.objects.filter(account=parent_payable, entry__description__icontains=partner.name)
        for item in items:
            item.account = acc
            item.save(update_fields=['account'])
            print(f"Migrated JournalItem {item.id} for Supplier {partner.name}")

    # Fix customer account
    if partner.is_customer and getattr(partner.receivable_account, 'code', None) == '1210':
        acc, created = Account.objects.get_or_create(
            code=f"{parent_receivable.code}-{partner.id}",
            defaults={
                'name': partner.name,
                'account_type': parent_receivable.account_type,
                'parent': parent_receivable
            }
        )
        partner.receivable_account = acc
        if getattr(partner.account, 'code', None) == '1210':
            partner.account = acc
        partner.save(update_fields=['receivable_account', 'account'])
        
        items = JournalItem.objects.filter(account=parent_receivable, entry__description__icontains=partner.name)
        for item in items:
            item.account = acc
            item.save(update_fields=['account'])
            print(f"Migrated JournalItem {item.id} for Customer {partner.name}")

print("Migration completed.")
