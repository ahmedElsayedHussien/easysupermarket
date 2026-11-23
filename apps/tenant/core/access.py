"""
Tenant access helpers.

``django.contrib.auth`` lives in the shared (public) schema, so ``auth_user`` is
global across all tenants. Membership of a tenant is therefore expressed by the
existence of an ``Employee`` row in the *current* schema. Admin rights inside a
tenant come from ``Employee.role == ADMIN`` (never from the global
``is_superuser`` / ``is_staff`` flags, which are reserved for platform
operators).

All helpers work both with django-tenants (PostgreSQL, one schema per tenant)
and in single-instance SQLite mode (django_tenants stripped).
"""
from django.core.exceptions import ObjectDoesNotExist
from django.db import connection

# (alias, schema) pairs where the Employee table is known to exist.
_EMPLOYEE_TABLE_PRESENT = set()


def _schema_key():
    return (connection.alias, getattr(connection, 'schema_name', None) or '')


def employee_table_exists():
    """True when the core Employee table exists in the current schema/DB."""
    key = _schema_key()
    if key in _EMPLOYEE_TABLE_PRESENT:
        return True
    from apps.tenant.core.models import Employee
    try:
        with connection.cursor() as cursor:
            names = connection.introspection.table_names(cursor)
    except Exception:
        return False
    if Employee._meta.db_table in names:
        _EMPLOYEE_TABLE_PRESENT.add(key)
        return True
    return False


def is_public_request(request):
    tenant = getattr(request, 'tenant', None)
    if tenant is None:
        return False
    try:
        from django_tenants.utils import get_public_schema_name
        public = get_public_schema_name()
    except Exception:
        public = 'public'
    return getattr(tenant, 'schema_name', None) == public


def get_employee(user):
    """Return the user's Employee profile in the current schema, or None."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    if not employee_table_exists():
        return None
    try:
        return user.employee_profile
    except ObjectDoesNotExist:
        return None


def is_tenant_member(user):
    """
    May this user use the current tenant?
    - platform superusers: yes
    - otherwise only if they have an Employee profile in the current schema.
    If the Employee table does not exist (fresh SQLite DB), the check is skipped.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    if not employee_table_exists():
        return True
    return get_employee(user) is not None


def is_tenant_admin(user):
    """Platform superuser, or Employee with role ADMIN in the current schema."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    emp = get_employee(user)
    if emp is None:
        return False
    from apps.tenant.core.models import Role
    return emp.role == Role.ADMIN


def get_branch_scope(request):
    """
    The Branch that data must be restricted to for this request, or None for
    "no restriction".

    - Admins: the branch selected in the branch switcher (request.branch);
      None means "all branches".
    - Non-admins: always their own employee branch, regardless of any GET /
      session override. Employees without a branch (central administration)
      are not branch-restricted.
    """
    user = getattr(request, 'user', None)
    if is_tenant_admin(user):
        return getattr(request, 'branch', None)
    emp = get_employee(user)
    if emp is not None and emp.branch_id:
        return emp.branch
    return None


def is_branch_restricted(request):
    """True if a non-admin is pinned to a branch."""
    user = getattr(request, 'user', None)
    if is_tenant_admin(user):
        return False
    emp = get_employee(user)
    return bool(emp is not None and emp.branch_id)


def filter_by_branch(queryset, request, field='branch'):
    """Filter ``queryset`` by ``field`` (a lookup path to a Branch FK)."""
    branch = get_branch_scope(request)
    if branch is None:
        return queryset
    return queryset.filter(**{field: branch})


def allowed_branches(request):
    """Queryset of branches the user may see."""
    from apps.tenant.core.models import Branch
    user = getattr(request, 'user', None)
    if not user or not getattr(user, 'is_authenticated', False):
        return Branch.objects.none()
    if is_tenant_admin(user):
        return Branch.objects.filter(is_active=True)
    emp = get_employee(user)
    if emp is not None and emp.branch_id:
        return Branch.objects.filter(id=emp.branch_id, is_active=True)
    # Central administration employees (no branch): not branch-restricted.
    if emp is not None:
        return Branch.objects.filter(is_active=True)
    return Branch.objects.none()
