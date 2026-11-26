"""Permission backend: the tenant's system admins (role ADMIN, or platform superusers)
hold EVERY permission — also permissions added after the admin account was created —
so ``user.has_perm()`` and ``{% if perms.app.codename %}`` in templates agree with the
views, which already let admins through (core.decorators.is_admin_user)."""


class TenantAdminPermissionBackend:
    def authenticate(self, request, **credentials):
        return None   # permissions only; logging in is handled by the other backends

    def _is_admin(self, user_obj):
        if not getattr(user_obj, 'is_active', False):
            return False
        from .access import is_tenant_admin
        return is_tenant_admin(user_obj)

    def has_perm(self, user_obj, perm, obj=None):
        return self._is_admin(user_obj)

    def has_module_perms(self, user_obj, app_label):
        return self._is_admin(user_obj)
