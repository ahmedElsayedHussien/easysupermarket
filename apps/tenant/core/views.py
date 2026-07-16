"""
Core views: Main dashboard, Admin analytics dashboard, Branch management.
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import json
from django.urls import reverse_lazy
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin, AccessMixin
from django.views.generic import UpdateView, ListView, CreateView, View
from django.contrib.auth.models import User
from .models import SystemSetting
from .forms import SystemSettingForm, EmployeeUserCreationForm, EmployeeUserUpdateForm
from .decorators import custom_permission_required

class AdminRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return hasattr(self.request.user, 'employee_profile') and self.request.user.employee_profile.is_admin()

class SystemSettingUpdateView(LoginRequiredMixin, AdminRequiredMixin, UpdateView):
    model = SystemSetting
    form_class = SystemSettingForm
    template_name = 'core/settings.html'
    success_url = reverse_lazy('core:system_settings')

    def get_object(self, queryset=None):
        return SystemSetting.get_settings()

    def form_valid(self, form):
        messages.success(self.request, 'تم حفظ إعدادات النظام بنجاح.')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'يرجى مراجعة الأخطاء وتصحيحها.')
        return super().form_invalid(form)


@login_required
def main_screen(request):
    """
    Main command hub / cashier dashboard.
    Shows today's KPIs, low-stock alerts, and expiry warnings.
    """
    from apps.tenant.core.models import Branch
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.inventory.models import InventoryBatch, Product

    today = timezone.now().date()
    branches = Branch.objects.filter(is_active=True)

    # --- Today's total sales ---
    today_sales = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED',
        date=today
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    
    yesterday = today - timedelta(days=1)
    yesterday_sales = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED',
        date=yesterday
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    
    if yesterday_sales > 0:
        today_sales_change = round(((today_sales - yesterday_sales) / yesterday_sales) * 100, 1)
    else:
        today_sales_change = 100 if today_sales > 0 else 0

    # --- Today's sale count ---
    today_invoices = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED',
        date=today
    ).count()
    
    # --- Total Products ---
    total_products = Product.objects.filter(is_active=True).count()

    # --- Low stock alerts ---
    stock_alerts = []
    low_stock_count = 0
    for product in Product.objects.filter(is_active=True).select_related('category'):
        stock = product.get_stock()
        if stock <= product.min_stock_level and product.min_stock_level > 0:
            low_stock_count += 1
            if len(stock_alerts) < 5:
                stock_alerts.append({
                    'product': product,
                    'stock': stock,
                    'min_stock': product.min_stock_level,
                })

    # --- Expiry alerts (items expiring in next 7 days) ---
    expiry_alerts = InventoryBatch.objects.filter(
        quantity_remaining__gt=0,
        expiry_date__isnull=False,
        expiry_date__lte=today + timedelta(days=7)
    ).select_related('product', 'warehouse').order_by('expiry_date')[:10]

    # --- Recent invoices ---
    recent_invoices = Invoice.objects.filter(
        status='POSTED'
    ).select_related('partner', 'branch').order_by('-created_at')[:5]

    context = {
        'user_role': 'Manager',
        'title': 'لوحة التحكم الرئيسية',
        'today': today,
        'today_sales': today_sales,
        'today_sales_change': today_sales_change,
        'today_invoices': today_invoices,
        'total_products': total_products,
        'low_stock_count': low_stock_count,
        'stock_alerts': stock_alerts,
        'expiry_alerts': expiry_alerts,
        'recent_invoices': recent_invoices,
    }
    return render(request, 'dashboard/main.html', context)


@login_required
def admin_dashboard(request):
    """
    Admin analytics dashboard.
    Shows 7-day sales chart, top branches, FIFO age alerts.
    """
    from apps.tenant.core.models import Branch
    from apps.tenant.invoicing.models import Invoice, InvoiceLine
    from apps.tenant.inventory.models import InventoryBatch, Product
    from django.contrib.auth.models import User

    today = timezone.now().date()
    branches = Branch.objects.filter(is_active=True)

    # --- Sales and Profit for the last 7 days (for chart) ---
    sales_data = []
    profit_data = []
    labels = []
    
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        
        # Day Sales
        day_sales = Invoice.objects.filter(
            invoice_type='SALE',
            status='POSTED',
            date=day
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        sales_data.append(float(day_sales))
        labels.append(day.strftime('%d %b'))

        # Day Profit (Sales - COGS)
        day_lines = InvoiceLine.objects.filter(
            invoice__invoice_type='SALE',
            invoice__status='POSTED',
            invoice__date=day
        ).aggregate(
            revenue=Sum('subtotal'),
            cogs=Sum('cogs_amount')
        )
        day_rev = day_lines['revenue'] or Decimal('0')
        day_cogs = day_lines['cogs'] or Decimal('0')
        day_profit = day_rev - day_cogs
        
        day_profit_margin = 0
        if day_rev > 0:
            day_profit_margin = float(day_profit / day_rev * 100)
        profit_data.append(round(day_profit_margin, 1))

    # Calculate overall avg profit margin
    all_time_lines = InvoiceLine.objects.filter(
        invoice__invoice_type='SALE',
        invoice__status='POSTED'
    ).aggregate(
        revenue=Sum('subtotal'),
        cogs=Sum('cogs_amount')
    )
    all_time_rev = all_time_lines['revenue'] or Decimal('0')
    all_time_cogs = all_time_lines['cogs'] or Decimal('0')
    avg_profit_margin = 0
    if all_time_rev > 0:
        avg_profit_margin = round(float((all_time_rev - all_time_cogs) / all_time_rev * 100), 1)

    # --- Top branches by revenue (all time) ---
    total_sales_overall = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED'
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('1')
    
    top_branches_qs = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED'
    ).values('branch__name', 'branch__address').annotate(
        revenue=Sum('total_amount'),
        tx_count=Count('id')
    ).order_by('-revenue')[:5]

    top_branches = []
    for tb in top_branches_qs:
        pct = (tb['revenue'] / total_sales_overall) * 100 if total_sales_overall > 0 else 0
        top_branches.append({
            'name': tb['branch__name'] or 'فرع رئيسي',
            'city': tb['branch__address'] or '',
            'total_sales': tb['revenue'],
            'invoice_count': tb['tx_count'],
            'sales_pct': round(float(pct), 1)
        })

    # --- Low stock alerts (aggregated across batches) ---
    stock_alerts = []
    for product in Product.objects.filter(is_active=True).select_related('category'):
        stock = product.get_stock()
        if stock <= product.min_stock_level and product.min_stock_level > 0:
            stock_alerts.append({
                'product': product,
                'stock': stock,
                'min_stock': product.min_stock_level,
            })
    stock_alerts = sorted(stock_alerts, key=lambda x: (x['stock'] - x['min_stock']))[:10]

    # --- Summary KPIs ---
    total_revenue_month = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED',
        date__month=today.month,
        date__year=today.year
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

    total_purchase_month = Invoice.objects.filter(
        invoice_type='PURCHASE',
        status='POSTED',
        date__month=today.month,
        date__year=today.year
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

    total_sales = Invoice.objects.filter(invoice_type='SALE', status='POSTED').aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    total_invoices = Invoice.objects.filter(invoice_type='SALE', status='POSTED').count()

    active_branches = branches.count()
    total_branches = Branch.objects.count()
    inactive_branches = total_branches - active_branches

    total_products = Product.objects.filter(is_active=True).count()

    # --- Active Users ---
    active_users_list = User.objects.filter(is_active=True).select_related('employee_profile__branch')[:5]
    users_with_branches = []
    for u in active_users_list:
        branch_obj = None
        if hasattr(u, 'employee_profile') and u.employee_profile:
            branch_obj = u.employee_profile.branch
        users_with_branches.append({
            'username': u.username,
            'get_full_name': u.get_full_name() or u.username,
            'is_superuser': u.is_superuser,
            'is_staff': u.is_staff,
            'branch': branch_obj
        })

    # --- FIFO Stock Valuation ---
    fifo_valuation = []
    batches = InventoryBatch.objects.filter(quantity_remaining__gt=0).select_related('product', 'product__category')
    fifo_dict = {}
    for b in batches:
        pid = b.product.id
        if pid not in fifo_dict:
            fifo_dict[pid] = {
                'product': b.product,
                'total_quantity': Decimal('0'),
                'total_value': Decimal('0'),
                'oldest_date': b.created_at.date()
            }
        fifo_dict[pid]['total_quantity'] += b.quantity_remaining
        fifo_dict[pid]['total_value'] += (b.quantity_remaining * b.unit_cost)
        if b.created_at.date() < fifo_dict[pid]['oldest_date']:
            fifo_dict[pid]['oldest_date'] = b.created_at.date()

    for pid, data in fifo_dict.items():
        if data['total_quantity'] > 0:
            data['fifo_cost'] = data['total_value'] / data['total_quantity']
            days_in_stock = (today - data['oldest_date']).days
            data['days_in_stock'] = days_in_stock
            
            if days_in_stock <= 30: aging_class = 'ok'
            elif days_in_stock <= 90: aging_class = 'moderate'
            elif days_in_stock <= 180: aging_class = 'warning'
            else: aging_class = 'critical'
            data['aging_class'] = aging_class
            
            fifo_valuation.append(data)
            
    fifo_valuation = sorted(fifo_valuation, key=lambda x: x['total_value'], reverse=True)[:5]

    # --- Recent Invoices ---
    recent_invoices = Invoice.objects.filter(
        status='POSTED'
    ).select_related('partner', 'branch').order_by('-created_at')[:5]

    context = {
        'user_role': 'Admin',
        'title': 'لوحة التحكم الإدارية',
        'today': today,
        'sales_chart_labels': json.dumps(labels),
        'sales_chart_data': json.dumps(sales_data),
        'profit_chart_data': json.dumps(profit_data),
        'avg_profit_margin': avg_profit_margin,
        'total_sales': total_sales,
        'total_invoices': total_invoices,
        'top_branches': top_branches,
        'stock_alerts': stock_alerts,
        'alerts_count': len(stock_alerts),
        'active_branches': active_branches,
        'total_branches': total_branches,
        'inactive_branches': inactive_branches,
        'total_revenue_month': total_revenue_month,
        'total_purchase_month': total_purchase_month,
        'total_products': total_products,
        'active_users_list': users_with_branches,
        'active_users': len(users_with_branches),
        'fifo_valuation': fifo_valuation,
        'recent_invoices': recent_invoices,
    }
    return render(request, 'dashboard/admin_dashboard.html', context)


@login_required
@custom_permission_required('core.view_branch', redirect_url='core:main_screen')
def branch_list(request):
    """
    Lists all branches. Supports POST to create a new branch.
    """
    from apps.tenant.core.models import Branch

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        code = request.POST.get('code', '').strip()
        address = request.POST.get('address', '').strip()
        phone = request.POST.get('phone', '').strip()

        if name and code:
            if Branch.objects.filter(code=code).exists():
                messages.error(request, f'كود الفرع "{code}" موجود بالفعل.')
            else:
                Branch.objects.create(
                    name=name,
                    code=code,
                    address=address,
                    phone=phone
                )
                messages.success(request, f'تم إنشاء الفرع "{name}" بنجاح.')
                return redirect('core:branch_list')
        else:
            messages.error(request, 'اسم الفرع والكود مطلوبان.')

    branches = Branch.objects.all().annotate(
        warehouse_count=Count('warehouses'),
        invoice_count=Count('invoices')
    )

    context = {
        'branches': branches,
        'title': 'إدارة الفروع',
        'user_branches': Branch.objects.filter(is_active=True),
        'user_role': 'Manager',
    }
    return render(request, 'core/branch_list.html', context)

# ==========================================
# User Management Views
# ==========================================

class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = 'core/user_list.html'
    context_object_name = 'users'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'المستخدمون والصلاحيات'
        return context

class UserCreateView(AdminRequiredMixin, CreateView):
    model = User
    form_class = EmployeeUserCreationForm
    template_name = 'core/user_form.html'
    success_url = reverse_lazy('core:user_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة مستخدم جديد'
        context['cancel_url'] = self.success_url
        
        # Group permissions by app_label for the template
        from django.contrib.auth.models import Permission
        from collections import defaultdict
        
        excluded_models = ['logentry', 'contenttype', 'session', 'group', 'permission', 'employee']
        perms = Permission.objects.filter(content_type__app_label__in=['core', 'accounting', 'inventory', 'invoicing', 'partners'])\
                                  .exclude(content_type__model__in=excluded_models)\
                                  .select_related('content_type')
        
        APP_NAMES = {
            'core': 'إعدادات النظام والفروع',
            'accounting': 'الحسابات والمالية',
            'inventory': 'المخزون والأصناف',
            'invoicing': 'المبيعات والمشتريات',
            'partners': 'العملاء والموردين',
        }
        
        grouped = defaultdict(list)
        for p in perms:
            name = str(p.name)
            name = name.replace("Can add", "إضافة")
            name = name.replace("Can change", "تعديل")
            name = name.replace("Can delete", "حذف")
            name = name.replace("Can view", "عرض")
            p.translated_name = name
            app_name = APP_NAMES.get(p.content_type.app_label, p.content_type.app_label)
            grouped[app_name].append(p)
            
        context['grouped_permissions'] = dict(grouped)
        return context

class UserUpdateView(AdminRequiredMixin, UpdateView):
    model = User
    form_class = EmployeeUserUpdateForm
    template_name = 'core/user_form.html'
    success_url = reverse_lazy('core:user_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل بيانات وصلاحيات المستخدم'
        context['cancel_url'] = self.success_url
        
        from django.contrib.auth.models import Permission
        from collections import defaultdict
        
        excluded_models = ['logentry', 'contenttype', 'session', 'group', 'permission', 'employee']
        perms = Permission.objects.filter(content_type__app_label__in=['core', 'accounting', 'inventory', 'invoicing', 'partners'])\
                                  .exclude(content_type__model__in=excluded_models)\
                                  .select_related('content_type')
        
        APP_NAMES = {
            'core': 'إعدادات النظام والفروع',
            'accounting': 'الحسابات والمالية',
            'inventory': 'المخزون والأصناف',
            'invoicing': 'المبيعات والمشتريات',
            'partners': 'العملاء والموردين',
        }
        
        grouped = defaultdict(list)
        for p in perms:
            name = str(p.name)
            name = name.replace("Can add", "إضافة")
            name = name.replace("Can change", "تعديل")
            name = name.replace("Can delete", "حذف")
            name = name.replace("Can view", "عرض")
            p.translated_name = name
            app_name = APP_NAMES.get(p.content_type.app_label, p.content_type.app_label)
            grouped[app_name].append(p)
            
        context['grouped_permissions'] = dict(grouped)
        return context

class ReportsIndexView(LoginRequiredMixin, View):
    """
    Main reports dashboard view. Serves as a structural skeleton for tabs.
    """
    def get(self, request, *args, **kwargs):
        context = {
            'title': 'لوحة التقارير الشاملة',
        }
        return render(request, 'core/reports.html', context)

@login_required
@custom_permission_required('core.view_systemsetting', redirect_url='core:main_screen')
def settings_dashboard(request):
    """
    System settings dashboard.
    Shows links to Branch, Warehouse, Category, Product, Customers, Suppliers, Chart of Accounts, Users.
    """
    from apps.tenant.core.models import Branch
    branches = Branch.objects.filter(is_active=True)
    
    context = {
        'title': 'إعدادات النظام',
        'user_role': 'Admin',
    }
    return render(request, 'core/settings_dashboard.html', context)


from django.views.generic import ListView, CreateView, UpdateView
from django.urls import reverse_lazy
from apps.tenant.core.models import Branch
from django.contrib.auth.mixins import LoginRequiredMixin

class BranchListView(AdminRequiredMixin, LoginRequiredMixin, ListView):
    model = Branch
    template_name = 'core/generic_list.html'
    context_object_name = 'objects'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إدارة الفروع'
        context['create_url'] = reverse_lazy('core:branch_create')
        context['update_url_name'] = 'core:branch_update'
        context['delete_url_name'] = 'core:branch_delete'
        return context

class BranchCreateView(AdminRequiredMixin, LoginRequiredMixin, CreateView):
    model = Branch
    fields = ['name', 'code', 'address', 'phone', 'is_active']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('core:branch_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة فرع جديد'
        context['cancel_url'] = reverse_lazy('core:branch_list')
        return context

class BranchUpdateView(AdminRequiredMixin, LoginRequiredMixin, UpdateView):
    model = Branch
    fields = ['name', 'code', 'address', 'phone', 'is_active']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('core:branch_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل الفرع'
        context['cancel_url'] = reverse_lazy('core:branch_list')
        return context

@login_required
@custom_permission_required('core.delete_branch', redirect_url='core:main_screen')
def branch_delete(request, pk):
    from apps.tenant.core.models import Branch
    from django.db.models import ProtectedError
    from django.shortcuts import get_object_or_404, redirect
    from django.contrib import messages
    
    if request.method == 'POST':
        branch = get_object_or_404(Branch, pk=pk)
        try:
            name = branch.name
            branch.delete()
            messages.success(request, f'تم حذف الفرع "{name}" بنجاح.')
        except ProtectedError:
            messages.error(request, f'لا يمكن حذف الفرع "{branch.name}" لارتباطه ببيانات أخرى (مخازن، فواتير، موظفين...).')
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء الحذف: {str(e)}')
    return redirect('core:branch_list')


