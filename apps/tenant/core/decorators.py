from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.shortcuts import redirect
from functools import wraps

def is_admin_user(user):
    """
    Check if a user is an Administrator of the CURRENT tenant.
    An admin is:
    - a platform superuser (Django is_superuser), or
    - a user whose Employee profile in the current schema has role ADMIN.

    is_staff, group names and usernames are deliberately NOT considered:
    auth_user is shared by all tenants, so only the per-schema Employee row
    can express tenant-level rights.
    """
    from apps.tenant.core.access import is_tenant_admin
    return is_tenant_admin(user)


def custom_permission_required(perm, login_url='login', redirect_url='core:branch_list'):
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
