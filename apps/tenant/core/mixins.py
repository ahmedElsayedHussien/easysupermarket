from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib import messages
from django.shortcuts import redirect
from django.core.exceptions import ImproperlyConfigured
from apps.tenant.core.decorators import is_admin_user

class CustomPermissionRequiredMixin(PermissionRequiredMixin):
    """
    A custom mixin that checks for permissions and shows a friendly error message
    using Django's messaging framework before redirecting the user.
    Admins are always granted full access.
    """
    permission_denied_message = 'عفواً، لا تملك الصلاحية اللازمة للقيام بهذا الإجراء.'

    def has_permission(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if is_admin_user(user):
            return True
        return super().has_permission()
    
    def handle_no_permission(self):

        if self.request.user.is_authenticated:
            # User is logged in but doesn't have permission
            missing_perms = self.get_permission_required()
            
            # Try to get human-readable permission names if possible
            perm_names = []
            from django.contrib.auth.models import Permission
            for perm in missing_perms:
                try:
                    app_label, codename = perm.split('.')
                    p_obj = Permission.objects.get(content_type__app_label=app_label, codename=codename)
                    perm_names.append(f"[{p_obj.name}]")
                except Exception:
                    perm_names.append(f"[{perm}]")
                    
            perms_str = " / ".join(perm_names)
            msg = f"{self.permission_denied_message} المطلوب: {perms_str}"
            
            messages.error(self.request, msg)
            
            # Redirect to dashboard
            return redirect('core:main_screen')
            
        return super().handle_no_permission()
