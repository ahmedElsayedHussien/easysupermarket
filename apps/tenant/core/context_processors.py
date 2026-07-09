from apps.tenant.core.models import Branch

def global_context(request):
    if hasattr(request, 'tenant') and request.tenant.schema_name == 'public':
        return {}
        
    branches = Branch.objects.none()
    
    if hasattr(request, 'user') and request.user.is_authenticated:
        if request.user.is_superuser:
            branches = Branch.objects.filter(is_active=True)
        elif hasattr(request.user, 'employee_profile'):
            employee = request.user.employee_profile
            if employee.role == 'Admin':
                branches = Branch.objects.filter(is_active=True)
            elif employee.branch:
                branches = Branch.objects.filter(id=employee.branch.id, is_active=True)
                
    return {
        'current_branch': getattr(request, 'branch', None),
        'nav_branches': branches
    }
