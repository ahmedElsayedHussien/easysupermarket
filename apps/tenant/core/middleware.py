from apps.tenant.core.models import Branch

class BranchMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def _get_allowed_branches(self, request):
        if not hasattr(request, 'user') or not request.user.is_authenticated:
            return Branch.objects.none()
            
        if request.user.is_superuser:
            return Branch.objects.filter(is_active=True)
            
        if hasattr(request, 'employee_profile'):
            employee = request.user.employee_profile
            if employee.role == 'Admin':
                return Branch.objects.filter(is_active=True)
            elif employee.branch:
                return Branch.objects.filter(id=employee.branch.id, is_active=True)
                
        return Branch.objects.none()

    def __call__(self, request):
        if hasattr(request, 'tenant') and request.tenant.schema_name == 'public':
            return self.get_response(request)
            
        allowed_branches = self._get_allowed_branches(request)
        branch_id = request.GET.get('branch')
        
        if branch_id:
            if branch_id == 'all':
                request.session['current_branch_id'] = 'all'
            else:
                try:
                    branch_id = int(branch_id)
                    branch = allowed_branches.get(id=branch_id)
                    request.session['current_branch_id'] = branch.id
                except (Branch.DoesNotExist, ValueError):
                    pass
                    
        session_branch_id = request.session.get('current_branch_id')
        
        if session_branch_id == 'all':
            request.branch = None
        elif session_branch_id:
            try:
                request.branch = allowed_branches.get(id=session_branch_id)
            except Branch.DoesNotExist:
                self._set_default_branch(request, allowed_branches)
        else:
            self._set_default_branch(request, allowed_branches)

        response = self.get_response(request)
        return response

    def _set_default_branch(self, request, allowed_branches):
        branch = None
        if hasattr(request, 'user') and request.user.is_authenticated:
            if hasattr(request.user, 'employee_profile'):
                employee = request.user.employee_profile
                if employee.branch and allowed_branches.filter(id=employee.branch.id).exists():
                    branch = employee.branch
        
        if not branch:
            branch = allowed_branches.first()
            
        request.branch = branch
        if branch:
            request.session['current_branch_id'] = branch.id
