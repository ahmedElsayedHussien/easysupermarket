"""
The organisation's identity on printed documents (invoices, quotations, statements,
reports): ONE place reads it from the settings, every template gets it as ``company``.

    name  : Settings > "اسم المتجر للإيصال" (store_name), else the tenant (company) name
    logo  : Settings > company logo (URL) or ''
    tax_number / commercial_register : Settings
"""


def company_profile(request=None, settings_obj=None):
    if settings_obj is None:
        try:
            from apps.tenant.core.models import SystemSetting
            settings_obj = SystemSetting.get_settings()
        except Exception:  # noqa: BLE001 - public schema / DB not ready
            settings_obj = None

    name = (getattr(settings_obj, 'store_name', '') or '').strip()
    if not name and request is not None:
        tenant = getattr(request, 'tenant', None)
        if tenant is not None and getattr(tenant, 'schema_name', 'public') != 'public':
            name = (getattr(tenant, 'name', '') or '').strip()

    logo_url = ''
    logo = getattr(settings_obj, 'company_logo', None)
    if logo:
        try:
            logo_url = logo.url
        except Exception:  # noqa: BLE001 - file missing
            logo_url = ''

    return {
        'name': name,
        'logo_url': logo_url,
        'phone': (getattr(settings_obj, 'store_phone', '') or '').strip(),
        'address': (getattr(settings_obj, 'store_address', '') or '').strip(),
        'tax_number': (getattr(settings_obj, 'tax_number', '') or '').strip(),
        'commercial_register': (getattr(settings_obj, 'commercial_register', '') or '').strip(),
    }
