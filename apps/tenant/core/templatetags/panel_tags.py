from django import template
from django.template.loader import render_to_string
from django.utils import timezone
from django.db.models import Sum
from django.contrib.auth import get_user_model
from decimal import Decimal

from apps.tenant.inventory.models import Product, Warehouse
from apps.tenant.invoicing.models import Invoice
from apps.tenant.core.models import Branch

register = template.Library()

@register.simple_tag(takes_context=True)
def render_right_panel(context):
    request = context.get('request')
    if not request:
        return ''
    
    app_name = request.resolver_match.app_name if request.resolver_match else ''
    
    # Do not render panel for POS screen as requested
    if app_name == 'invoicing' and request.resolver_match.url_name == 'pos':
        return ''
        
    # Hide panel for Cashier users
    if hasattr(request, 'user') and request.user.is_authenticated:
        if hasattr(request.user, 'employee_profile'):
            if request.user.employee_profile.role == 'Cashier':
                return ''
    
    new_context = {
        'request': request,
        'user': request.user,
        'branches': context.get('branches', Branch.objects.filter(is_active=True)),
        'current_branch': context.get('current_branch'),
        'warehouses': context.get('warehouses', Warehouse.objects.filter(is_active=True)),
        'current_warehouse': context.get('current_warehouse'),
    }

    if app_name == 'inventory':
        template_name = 'panels/inventory_panel.html'
        # Logic for low stock count
        new_context['low_stock_count'] = Product.objects.filter(min_stock_level__gte=1).count()
        
    elif app_name == 'invoicing':
        template_name = 'panels/invoicing_panel.html'
        today = timezone.now().date()
        today_invoices_qs = Invoice.objects.filter(date=today, invoice_type=Invoice.SALE)
        new_context['today_invoices'] = today_invoices_qs.count()
        new_context['today_revenue'] = today_invoices_qs.aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        
        # Payment types revenue (cash, card, credit usually, but using CASH, CARD)
        new_context['cash_revenue'] = today_invoices_qs.filter(payment_type=Invoice.CASH).aggregate(t=Sum('total_amount'))['t'] or Decimal('0')
        new_context['card_revenue'] = today_invoices_qs.filter(payment_type=Invoice.CARD).aggregate(t=Sum('total_amount'))['t'] or Decimal('0')
        # Wallet isn't in models, but UI has it, we'll keep it 0 or we can use CREDIT if needed
        new_context['wallet_revenue'] = Decimal('0')

    else:
        template_name = 'panels/default_panel.html'
        User = get_user_model()
        new_context['active_users'] = User.objects.filter(is_active=True).count()
        today = timezone.now().date()
        new_context['today_invoices'] = Invoice.objects.filter(date=today, invoice_type=Invoice.SALE).count()
        new_context['low_stock_count'] = Product.objects.filter(min_stock_level__gte=1).count()

    return render_to_string(template_name, new_context)
