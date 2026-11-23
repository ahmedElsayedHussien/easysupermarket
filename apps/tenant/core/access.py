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


BRANCH_UNASSIGNED_MESSAGE = 'حسابك غير مرتبط بفرع — راجع مدير النظام'


def is_branch_unassigned(request):
    """
    True for a non-admin employee whose account is not linked to any (active)
    branch. Such users must see NO branch-scoped data at all (previously they
    were treated as "central administration" and saw every branch).

    Not applied when the Employee table does not exist yet (fresh SQLite DB
    being bootstrapped) or for anonymous users.
    """
    cached = getattr(request, '_branch_unassigned_cache', None)
    if cached is not None:
        return cached
    user = getattr(request, 'user', None)
    result = False
    if (user is not None and getattr(user, 'is_authenticated', False)
            and not is_tenant_admin(user) and employee_table_exists()):
        emp = get_employee(user)
        if emp is None or not emp.branch_id:
            result = True
        else:
            from apps.tenant.core.models import Branch
            result = not Branch.objects.filter(id=emp.branch_id, is_active=True).exists()
    try:
        request._branch_unassigned_cache = result
    except Exception:
        pass
    return result


def get_branch_scope(request):
    """
    The Branch that data must be restricted to for this request, or None for
    "no restriction".

    - Admins: the branch selected in the branch switcher (request.branch);
      None means "all branches".
    - Non-admins: always their own employee branch, regardless of any GET /
      session override.
    - Non-admins WITHOUT a branch also get None here (backward compatible
      return type), but they are NOT unrestricted: always check
      ``is_branch_unassigned(request)`` / ``request.branch_restricted`` or,
      better, use ``filter_by_branch()`` which returns an empty queryset.
    """
    user = getattr(request, 'user', None)
    if is_tenant_admin(user):
        return getattr(request, 'branch', None)
    emp = get_employee(user)
    if emp is not None and emp.branch_id:
        return emp.branch
    return None


def is_branch_restricted(request):
    """
    True if the user may NOT see company-wide data: any non-admin employee,
    whether pinned to a branch or not linked to a branch at all.
    """
    user = getattr(request, 'user', None)
    if is_tenant_admin(user):
        return False
    emp = get_employee(user)
    if emp is not None and emp.branch_id:
        return True
    return is_branch_unassigned(request)


def get_allowed_branch_ids(request):
    """
    List of branch ids the user may see, or None for "all branches"
    (admins). Non-admins without a branch get ``[]``.
    """
    user = getattr(request, 'user', None)
    if is_tenant_admin(user):
        return None
    if is_branch_unassigned(request):
        return []
    return list(allowed_branches(request).values_list('id', flat=True))


def filter_by_branch(queryset, request, field='branch'):
    """
    Filter ``queryset`` by ``field`` (a lookup path to a Branch FK).
    Single source of truth for branch scoping: non-admins without a branch
    get an empty queryset.
    """
    if is_branch_unassigned(request):
        return queryset.none()
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
    if not employee_table_exists():
        # Fresh DB being bootstrapped: no employees yet.
        return Branch.objects.filter(is_active=True)
    # Non-admin without a branch: no branch access at all.
    return Branch.objects.none()
