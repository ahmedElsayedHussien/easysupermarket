from django.contrib.auth import logout

from apps.tenant.core.models import Branch
from apps.tenant.core.access import (
    is_public_request, is_tenant_member, is_tenant_admin, get_employee,
    is_branch_unassigned, BRANCH_UNASSIGNED_MESSAGE,
)


def _wants_html_page(request):
    if request.method != 'GET':
        return False
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return False
    path = request.path or ''
    if path.startswith(('/static/', '/media/', '/i18n/', '/api/')) or '/api/' in path:
        return False
    accept = request.headers.get('accept', '')
    return 'text/html' in accept


def _add_unassigned_notice(request):
    """Queue the 'no branch' warning once (no duplicates across redirects)."""
    from django.contrib import messages
    storage = getattr(request, '_messages', None)
    if storage is None:
        return
    try:
        pending = list(getattr(storage, '_queued_messages', [])) + list(getattr(storage, '_loaded_messages', []))
        if any(str(m.message) == BRANCH_UNASSIGNED_MESSAGE for m in pending):
            return
    except Exception:
        pass
    messages.warning(request, BRANCH_UNASSIGNED_MESSAGE)


class BranchMiddleware:
    """
    1. Tenant membership guard: auth_user is shared by all tenants, so a session
       for a user who has no Employee profile in the current tenant schema (and
       is not a platform superuser) is logged out.
    2. Branch selection: admins may switch branches (or pick "all"); everybody
       else is pinned to their own employee branch, regardless of GET/session.
    3. Sets ``request.branch_restricted`` (True for every non-admin) and
       ``request.allowed_branch_ids`` (None = all branches for admins, [] for a
       non-admin whose account is not linked to an active branch). Non-admins
       without a branch see no branch-scoped data and get a warning banner.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.branch_restricted = False
        request.allowed_branch_ids = None
        request.branch_unassigned = False
        if is_public_request(request):
            return self.get_response(request)

        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and not is_tenant_member(user):
            logout(request)

        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            request.branch = None
            return self.get_response(request)

        if is_tenant_admin(user):
            self._select_admin_branch(request)
        else:
            # Non-admins: always their own branch. Employees without a branch
            # (central administration) get request.branch = None.
            emp = get_employee(user)
            branch = None
            if emp is not None and emp.branch_id:
                branch = Branch.objects.filter(id=emp.branch_id, is_active=True).first()
            request.branch = branch
            if request.session.get('current_branch_id') != (branch.id if branch else None):
                request.session['current_branch_id'] = branch.id if branch else None
            request.branch_restricted = True
            if branch is not None:
                request.allowed_branch_ids = [branch.id]
            elif is_branch_unassigned(request):
                # No branch: nothing branch-scoped is visible.
                request.allowed_branch_ids = []
                request.branch_unassigned = True
                if _wants_html_page(request):
                    _add_unassigned_notice(request)

        return self.get_response(request)

    def _select_admin_branch(self, request):
        allowed_branches = Branch.objects.filter(is_active=True)
        branch_id = request.GET.get('branch')

        if branch_id:
            if branch_id == 'all':
                request.session['current_branch_id'] = 'all'
            else:
                try:
                    branch = allowed_branches.get(id=int(branch_id))
                    request.session['current_branch_id'] = branch.id
                except (Branch.DoesNotExist, ValueError, TypeError):
                    pass

        session_branch_id = request.session.get('current_branch_id')

        if session_branch_id == 'all':
            request.branch = None
        elif session_branch_id:
            try:
                request.branch = allowed_branches.get(id=session_branch_id)
            except (Branch.DoesNotExist, ValueError, TypeError):
                self._set_default_branch(request, allowed_branches)
        else:
            self._set_default_branch(request, allowed_branches)

    def _set_default_branch(self, request, allowed_branches):
        branch = None
        emp = get_employee(request.user)
        if emp is not None and emp.branch_id:
            branch = allowed_branches.filter(id=emp.branch_id).first()
        if not branch:
            branch = allowed_branches.first()
        request.branch = branch
        if branch:
            request.session['current_branch_id'] = branch.id
