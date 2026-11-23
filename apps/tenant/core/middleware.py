from django.contrib.auth import logout

from apps.tenant.core.models import Branch
from apps.tenant.core.access import (
    is_public_request, is_tenant_member, is_tenant_admin, get_employee,
)


class BranchMiddleware:
    """
    1. Tenant membership guard: auth_user is shared by all tenants, so a session
       for a user who has no Employee profile in the current tenant schema (and
       is not a platform superuser) is logged out.
    2. Branch selection: admins may switch branches (or pick "all"); everybody
       else is pinned to their own employee branch, regardless of GET/session.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
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
