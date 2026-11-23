from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.urls import path


@staff_member_required(login_url='/admin/login/')
def public_home(request):
    """
    Platform landing page listing tenants. Restricted to platform staff
    (redirects to /admin/login/?next=/). All values are autoescaped by the
    template (templates/public/home.html).
    """
    from apps.public.tenants.models import Tenant
    from django_tenants.utils import get_public_schema_name

    tenants = (Tenant.objects.exclude(schema_name=get_public_schema_name())
               .prefetch_related('domains').order_by('schema_name'))

    icons = ['🛒', '🛍️', '🏬', '🏪', '✨']
    colors = [
        ('rgba(59,130,246,0.15)', '#3b82f6'),
        ('rgba(168,85,247,0.15)', '#a855f7'),
        ('rgba(16,185,129,0.15)', '#10b981'),
        ('rgba(245,158,11,0.15)', '#f59e0b'),
        ('rgba(239,68,68,0.15)',  '#ef4444'),
    ]

    tenant_cards = []
    for idx, tenant in enumerate(tenants):
        bg_color, accent = colors[idx % len(colors)]
        domains = list(tenant.domains.all())
        domain = next((d for d in domains if getattr(d, 'is_primary', False)), domains[0] if domains else None)
        domain_str = domain.domain if domain else 'localhost'
        port = request.get_port()
        port_part = '' if port in ('80', '443') else f':{port}'
        tenant_cards.append({
            'name': tenant.name,
            'schema_name': tenant.schema_name,
            'url': f'{request.scheme}://{domain_str}{port_part}/',
            'icon': icons[idx % len(icons)],
            'bg': bg_color,
            'accent': accent,
        })

    return render(request, 'public/home.html', {
        'tenant_cards': tenant_cards,
        'tenant_count': len(tenant_cards),
    })


urlpatterns = [
    path('', public_home, name='public_home'),
    path('admin/', admin.site.urls),
]
