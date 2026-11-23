from django.db import connection
from apps.tenant.core.models import Branch, SystemSetting

def global_context(request):
    if hasattr(request, 'tenant') and request.tenant.schema_name == 'public':
        return {}
        
    branches = Branch.objects.none()
    
    is_admin = False
    if hasattr(request, 'user') and request.user.is_authenticated:
        from apps.tenant.core.decorators import is_admin_user
        is_admin = is_admin_user(request.user)
        if is_admin:
            branches = Branch.objects.filter(is_active=True)
        elif hasattr(request.user, 'employee_profile') and request.user.employee_profile:
            employee = request.user.employee_profile
            if employee.branch:
                branches = Branch.objects.filter(id=employee.branch.id, is_active=True)

    try:
        system_setting = SystemSetting.get_settings()
    except Exception:
        system_setting = None

    db_engine_name = 'PostgreSQL' if 'postgresql' in connection.settings_dict.get('ENGINE', '') else 'SQLite'

    return {
        'current_branch': getattr(request, 'branch', None),
        'nav_branches': branches,
        'system_setting': system_setting,
        'is_admin': is_admin,
        'db_engine_name': db_engine_name,
    }
