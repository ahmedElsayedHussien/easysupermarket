"""
تثبيت البرنامج كتطبيق (PWA) + جرس التنبيهات.

The service worker is NETWORK-ONLY on purpose: nothing is cached, so figures, stock and
balances are always live (an installable shell only, like the old system).
"""
import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.templatetags.static import static
from django.views.decorators.cache import never_cache

SERVICE_WORKER = """// Masar ERP — network only: never serve cached business data.
self.addEventListener('install', function (e) { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function (e) { /* default network handling */ });
"""


def manifest(request):
    from .company import company_profile
    name = company_profile(request).get('name') or 'مسار'
    icon = static('img/app-icon.png')
    data = {
        'name': f'{name} — Masar ERP', 'short_name': 'مسار', 'lang': 'ar', 'dir': 'rtl',
        'start_url': '/', 'scope': '/', 'display': 'standalone',
        'background_color': '#0a0e1a', 'theme_color': '#ff7300',
        'icons': [{'src': icon, 'sizes': '192x192', 'type': 'image/png'},
                  {'src': icon, 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any maskable'}],
    }
    return HttpResponse(json.dumps(data, ensure_ascii=False), content_type='application/manifest+json')


@never_cache
def service_worker(request):
    response = HttpResponse(SERVICE_WORKER, content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    return response


@login_required
@never_cache
def alerts_api(request):
    """Things to act on (same list as the home page) for the header bell."""
    from django.utils import timezone
    from . import home_dashboard as home
    from .views import low_stock_count_for
    today = timezone.localdate()
    try:
        _, low_stock = low_stock_count_for(request.user)
        items = home.alerts(request, today, low_stock)
    except Exception:
        items = []
    return JsonResponse({'count': sum(1 for _ in items),
                         'items': [{'icon': a['icon'], 'text': a['text'], 'count': a['count'], 'url': a['url'],
                                    'level': a['level']} for a in items]})
