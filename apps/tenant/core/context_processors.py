from django.db import connection
from apps.tenant.core.models import Branch, SystemSetting
from apps.tenant.core.access import BRANCH_UNASSIGNED_MESSAGE
from apps.tenant.core.company import company_profile
from apps.tenant.core.nav import build_sidebar_nav


def global_context(request):
    if hasattr(request, 'tenant') and request.tenant.schema_name == 'public':
        return {}
        
    branches = Branch.objects.none()
    
    is_admin = False
    is_authenticated = hasattr(request, 'user') and request.user.is_authenticated
    if is_authenticated:
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
        'nav': build_sidebar_nav(request.user, system_setting, is_admin) if is_authenticated else {},
        # Organisation name / logo for printed documents (core.company)
        'company': company_profile(request, system_setting),
        'is_admin': is_admin,
        'db_engine_name': db_engine_name,
        # Non-admin account not linked to a branch: sees no branch data.
        'branch_unassigned': getattr(request, 'branch_unassigned', False),
        # Open POS shift: branch switching is locked until the shift is closed.
        'branch_locked_by_shift': getattr(request, 'branch_locked_by_shift', False),
        'branch_unassigned_message': BRANCH_UNASSIGNED_MESSAGE,
    }
