from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.shortcuts import redirect
from functools import wraps

def is_admin_user(user):
    """
    Check if a user is considered an Administrator with full permissions.
    An admin can be:
    - Superuser (Django is_superuser)
    - Staff (Django is_staff)
    - Has an Employee profile with Role.ADMIN or is_admin() returning True
    - Belonging to an admin group or username contains admin
    """
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    if hasattr(user, 'employee_profile') and user.employee_profile:
        if getattr(user.employee_profile, 'role', '') == 'ADMIN' or user.employee_profile.is_admin():
            return True
    try:
        if user.groups.filter(name__iregex=r'(admin|مدير)').exists():
            return True
    except Exception:
        pass
    if 'admin' in user.username.lower():
        return True
    return False


def custom_permission_required(perm, login_url='core:login', redirect_url='core:branch_list'):
    """
    Decorator for views that checks whether a user has a particular permission
    enabled, redirecting to the log-in page if necessary.
    If the user is logged in but doesn't have the permission, it shows a friendly
    error message and redirects to the dashboard (or another URL).
    Admins are always granted full access.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(login_url)
            
            # Admins have full access to all protected actions
            if is_admin_user(request.user):
                return view_func(request, *args, **kwargs)

            if isinstance(perm, str):
                perms = (perm,)
            else:
                perms = perm
                
            if request.user.has_perms(perms):
                return view_func(request, *args, **kwargs)

                
            # Try to get human-readable permission names if possible
            perm_names = []
            from django.contrib.auth.models import Permission
            for p in perms:
                try:
                    app_label, codename = p.split('.')
                    p_obj = Permission.objects.get(content_type__app_label=app_label, codename=codename)
                    perm_names.append(f"[{p_obj.name}]")
                except:
                    perm_names.append(f"[{p}]")
            
            perms_str = " أو ".join(perm_names)
            msg = f"عفواً، لا تملك الصلاحية اللازمة للقيام بهذا الإجراء. الصلاحية المطلوبة: {perms_str}"
            messages.error(request, msg)
            return redirect(redirect_url)
            
        return _wrapped_view
    return decorator
