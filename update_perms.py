import re

with open('E:/easysupermarket/apps/tenant/core/reports_views.py', 'r', encoding='utf-8') as f:
    c = f.read()

c = c.replace("'invoicing.view_invoice'", "'core.view_sales_reports'")
c = c.replace("'inventory.view_product'", "'core.view_inventory_reports'")
c = c.replace("'inventory.view_inventorybatch'", "'core.view_inventory_reports'")
c = c.replace("'inventory.view_stockmovement'", "'core.view_inventory_reports'")
c = c.replace("'accounting.view_expense'", "'core.view_accounting_reports'")
c = c.replace("'accounting.view_account'", "'core.view_accounting_reports'")
c = c.replace("'accounting.view_journalentry'", "'core.view_accounting_reports'")
c = c.replace("'partners.view_partner'", "'core.view_sales_reports'")
c = c.replace("'invoicing.view_possession'", "'core.view_sales_reports'")

with open('E:/easysupermarket/apps/tenant/core/reports_views.py', 'w', encoding='utf-8') as f:
    f.write(c)

print("Permissions updated successfully!")
