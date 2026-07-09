"""
EasySupermarket - URL Configuration
Root URL dispatcher for the tenant-aware Django project.
"""
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views

urlpatterns = [
    # Core / Dashboard
    path('', include('apps.tenant.core.urls')),

    # Invoicing (POS + Purchases)
    path('invoicing/', include('apps.tenant.invoicing.urls')),

    # Inventory management
    path('inventory/', include('apps.tenant.inventory.urls')),

    # Partners (suppliers & customers)
    path('partners/', include('apps.tenant.partners.urls')),

    # Accounting (journal, CoA, reports)
    path('accounting/', include('apps.tenant.accounting.urls')),

    # Auth
    path('login/', auth_views.LoginView.as_view(template_name='auth/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/login/'), name='logout'),
    
    # i18n
    path('i18n/', include('django.conf.urls.i18n')),
]

# Serve media and static in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
