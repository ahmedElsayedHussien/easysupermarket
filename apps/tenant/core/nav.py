"""Centralized sidebar visibility computation combining SystemSetting toggles and user permissions."""
from django.core.exceptions import ObjectDoesNotExist

# key: (SystemSetting boolean field or None, permission string or None)
NAV_ITEMS = {
    # Quick Navigation
    'dashboard': ('show_nav_dashboard', None),
    'pos': ('show_nav_pos', None),

    # Sales
    'quotations': ('show_nav_quotations', None),
    'sales_invoices': ('show_nav_sales_invoices', None),
    'pricelists': ('show_nav_pricelists', None),
    'sales_reps': ('show_nav_sales_reps', 'invoicing.view_salesrep'),
    'loading_orders': ('show_nav_loading_orders', 'invoicing.view_loadingorder'),
    'unloading_orders': ('show_nav_unloading_orders', 'invoicing.view_unloadingorder'),
    'rep_handovers': ('show_nav_rep_handovers', 'invoicing.view_repcashhandover'),
    'rep_app': ('show_nav_rep_app', 'invoicing.use_rep_app'),

    # Purchases
    'purchase_invoices': ('show_nav_purchase_invoices', None),
    'purchase_orders': ('show_nav_purchase_orders', 'invoicing.view_purchaseorder'),
    'create_purchase': ('show_nav_create_purchase', None),

    # Inventory
    'inventory_dashboard': ('show_nav_inventory_dashboard', None),

    # Rentals
    'rentals_equipment': ('show_nav_rentals_equipment', None),
    'rentals_contracts': ('show_nav_rentals_contracts', None),
    'rentals_reports': ('show_nav_rentals_reports', None),

    # Maintenance
    'maintenance_tickets': ('show_nav_maintenance_tickets', None),

    # Commissions
    'commission_rules': ('show_nav_commission_rules', None),
    'commission_report': ('show_nav_commission_report', None),

    # HR
    'hr_attendance': ('show_nav_hr_attendance', None),
    'hr_attendance_manage': ('show_nav_hr_attendance_manage', 'hr.change_attendance'),
    'hr_attendance_report': ('show_nav_hr_attendance_report', 'hr.view_attendance'),
    'hr_payroll': ('show_nav_hr_payroll', None),
    'hr_employees': ('show_nav_hr_employees', 'hr.view_employeeprofile'),
    'hr_my_portal': ('show_nav_hr_my_portal', None),
    'hr_leaves': ('show_nav_hr_leaves', 'hr.view_leaverequest'),
    'hr_loans': ('show_nav_hr_loans', 'hr.view_employeeloan'),

    # Partners
    'partners_dashboard': ('show_nav_partners_dashboard', None),

    # Accounting
    'accounting_dashboard': ('show_nav_accounting_dashboard', None),
    'liquidity_dashboard': ('show_nav_liquidity_dashboard', None),
    'fiscal_years': ('show_nav_fiscal_years', 'accounting.view_fiscalyearclose'),
    'account_mapping': ('show_nav_account_mapping', 'accounting.change_accountmapping'),
    'intermediaries': ('show_nav_intermediaries', 'accounting.view_intermediarycompany'),
    'tax_settlement': ('show_nav_tax_settlement', 'accounting.view_taxsettlement'),
    'bank_reconciliation': ('show_nav_bank_reconciliation', 'accounting.view_bankreconciliation'),
    'custody': ('show_nav_custody', 'accounting.view_custody'),
    'eservices': ('show_nav_eservices', None),

    # Administration (admin-only)
    'admin_dashboard': ('show_nav_admin_dashboard', None),
    'branches': ('show_nav_branches', None),
    'users': ('show_nav_users', None),
    'audit_log': ('show_nav_audit_log', 'core.view_auditlog'),
    'reports': ('show_nav_reports', None),

    # Tax & E-Invoicing (admin-only)
    'einvoice_approval': ('show_nav_einvoice_approval', None),
    'einvoice_history': ('show_nav_einvoice_history', None),
    'egs_codes': ('show_nav_tax_settings', None),
    'received_documents': ('show_nav_received_documents', None),
    'tax_settings': ('show_nav_tax_settings', None),
}

NAV_SECTIONS = {
    'quick': ('dashboard', 'pos'),
    'sales': (
        'quotations', 'sales_invoices', 'pricelists',
        'sales_reps', 'loading_orders', 'unloading_orders', 'rep_handovers', 'rep_app',
    ),
    'purchases': ('purchase_invoices', 'purchase_orders', 'create_purchase'),
    'inventory': ('inventory_dashboard',),
    'rentals': ('rentals_equipment', 'rentals_contracts', 'rentals_reports'),
    'maintenance': ('maintenance_tickets',),
    'commissions': ('commission_rules', 'commission_report'),
    'hr': (
        'hr_attendance', 'hr_attendance_manage', 'hr_attendance_report',
        'hr_payroll', 'hr_employees', 'hr_my_portal', 'hr_leaves', 'hr_loans',
    ),
    'partners': ('partners_dashboard',),
    'accounting': (
        'accounting_dashboard', 'liquidity_dashboard', 'fiscal_years',
        'account_mapping', 'intermediaries', 'tax_settlement',
        'bank_reconciliation', 'custody', 'eservices',
    ),
    'admin': ('admin_dashboard', 'branches', 'users', 'audit_log', 'reports'),
    'tax': ('einvoice_approval', 'einvoice_history', 'egs_codes', 'received_documents', 'tax_settings'),
}

_ADMIN_ONLY_SECTIONS = ('admin', 'tax')
_ADMIN_ONLY_ITEMS = {item for sec in _ADMIN_ONLY_SECTIONS for item in NAV_SECTIONS[sec]}


def _has_employee_profile(user):
    try:
        return bool(getattr(user, 'employee_profile', None))
    except ObjectDoesNotExist:
        return False


def build_sidebar_nav(user, setting, is_admin):
    """Compute a flat dict of item_<name> and sec_<section> visibility booleans."""
    nav = {}
    has_profile = None
    for key, (field, perm) in NAV_ITEMS.items():
        visible = (setting is None or (field is None or getattr(setting, field, True)))
        if visible and perm is not None:
            visible = user.has_perm(perm)
        if visible and key in _ADMIN_ONLY_ITEMS:
            visible = bool(is_admin)
        if visible and key == 'hr_my_portal':
            if has_profile is None:
                has_profile = _has_employee_profile(user)
            visible = has_profile
        nav[f'item_{key}'] = bool(visible)

    for sec_key, items in NAV_SECTIONS.items():
        nav[f'sec_{sec_key}'] = any(nav[f'item_{item}'] for item in items)

    return nav
