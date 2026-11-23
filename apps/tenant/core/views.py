"""
Core views: Main dashboard, Admin analytics dashboard, Branch management.
"""
import os
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, Q, F, Min, Case, When, DecimalField
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import json
from django.urls import reverse_lazy
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin, AccessMixin
from django.views.generic import UpdateView, ListView, CreateView, View
from django.contrib.auth.models import User
from django.http import Http404
from .models import SystemSetting
from .forms import SystemSettingForm, EmployeeUserCreationForm, EmployeeUserUpdateForm
from .decorators import custom_permission_required, is_admin_user
from .access import allowed_branches as _allowed_branches
from .usernames import display_username

_is_admin = is_admin_user


def tenant_users_queryset(request):
    """
    Users of the CURRENT tenant only (auth_user is shared by all tenants):
    users with an Employee profile in this schema. Platform superusers are
    hidden from tenant admins.
    """
    qs = User.objects.filter(employee_profile__isnull=False).select_related('employee_profile__branch')
    if not request.user.is_superuser:
        qs = qs.filter(is_superuser=False)
    return qs


class AdminRequiredMixin(UserPassesTestMixin):
    """
    Mixin that ensures the user is an administrator or has specific model permissions.
    Supports superusers, staff, and employees with ADMIN role.
    """
    def test_func(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return False
        if is_admin_user(user):
            return True
        # Check specific branch/user/setting permissions if applicable
        view_name = self.__class__.__name__
        if 'Branch' in view_name:
            if 'Create' in view_name and user.has_perm('core.add_branch'):
                return True
            if 'Update' in view_name and user.has_perm('core.change_branch'):
                return True
            if 'List' in view_name and (user.has_perm('core.view_branch') or user.has_perm('core.add_branch')):
                return True
        return False

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            messages.error(self.request, 'عفواً، هذه الصفحة مخصصة لمديري النظام فقط أو لمن يمتلكون الصلاحية اللازمة.')
            return redirect('core:main_screen')
        return super().handle_no_permission()

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
    Shows today's KPIs, quick access tiles, and system summary.
    Optimized with zero in-memory loops and direct database-level aggregation.
    """
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.inventory.models import InventoryBatch, Product
    from django.db.models import Subquery, OuterRef, F, Case, When, Count
    from django.db.models.functions import Coalesce

    today = timezone.now().date()
    yesterday = today - timedelta(days=1)

    # Determine if user is admin/superuser to load quick stats
    is_admin_user = _is_admin(request.user)

    today_sales = Decimal('0')
    today_sales_change = 0
    today_invoices = 0
    total_products = 0
    low_stock_count = 0

    if is_admin_user:
        # Today's gross sales, returns, and invoice count in one single database aggregation
        today_stats = Invoice.objects.filter(
            status='POSTED',
            date=today,
            invoice_type__in=['SALE', 'RETURN_SALE']
        ).aggregate(
            sales=Sum(Case(When(invoice_type='SALE', then='total_amount'), default=Decimal('0'))),
            returns=Sum(Case(When(invoice_type='RETURN_SALE', then='total_amount'), default=Decimal('0'))),
            invoices_count=Count(Case(When(invoice_type='SALE', then='id')))
        )
        today_gross = today_stats['sales'] or Decimal('0')
        today_returns = today_stats['returns'] or Decimal('0')
        today_sales = today_gross - today_returns
        today_invoices = today_stats['invoices_count'] or 0

        # Yesterday's net sales in one query
        yesterday_stats = Invoice.objects.filter(
            status='POSTED',
            date=yesterday,
            invoice_type__in=['SALE', 'RETURN_SALE']
        ).aggregate(
            sales=Sum(Case(When(invoice_type='SALE', then='total_amount'), default=Decimal('0'))),
            returns=Sum(Case(When(invoice_type='RETURN_SALE', then='total_amount'), default=Decimal('0')))
        )
        yesterday_sales = (yesterday_stats['sales'] or Decimal('0')) - (yesterday_stats['returns'] or Decimal('0'))

        if yesterday_sales > 0:
            today_sales_change = round(((today_sales - yesterday_sales) / yesterday_sales) * 100, 1)
        else:
            today_sales_change = 100 if today_sales > 0 else 0

        # Total active products
        total_products = Product.objects.filter(is_active=True).count()

        # Database-level calculation for low stock count (zero in-memory loops)
        batch_stock_subquery = InventoryBatch.objects.filter(
            product=OuterRef('pk'),
            quantity_remaining__gt=0
        ).values('product').annotate(
            total=Sum('quantity_remaining')
        ).values('total')

        low_stock_count = Product.objects.filter(
            is_active=True,
            min_stock_level__gt=0
        ).annotate(
            current_stock=Coalesce(Subquery(batch_stock_subquery), Decimal('0'))
        ).filter(
            current_stock__lte=F('min_stock_level')
        ).count()

    context = {
        'user_role': 'Manager',
        'title': 'لوحة التحكم الرئيسية',
        'today': today,
        'today_sales': today_sales,
        'today_sales_change': today_sales_change,
        'today_invoices': today_invoices,
        'total_products': total_products,
        'low_stock_count': low_stock_count,
    }
    return render(request, 'dashboard/main.html', context)


@login_required
def admin_dashboard(request):
    """
    Admin analytics dashboard.
    Shows 7-day sales chart, top branches, FIFO age alerts.
    """
    if not is_admin_user(request.user):
        messages.error(request, 'عفواً، لوحة التحكم الإدارية مقتصرة على مديري النظام.')
        return redirect('core:main_screen')
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
    
    from django.db.models import Case, When, DecimalField
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        
        # Day Sales (Net: SALE - RETURN_SALE)
        sale_sum = Invoice.objects.filter(
            invoice_type='SALE',
            status='POSTED',
            date=day
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        return_sum = Invoice.objects.filter(
            invoice_type='RETURN_SALE',
            status='POSTED',
            date=day
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        day_sales = sale_sum - return_sum
        sales_data.append(float(day_sales))
        labels.append(day.strftime('%d %b'))

        # Day Profit (Net Revenue - Net COGS)
        day_lines = InvoiceLine.objects.filter(
            invoice__invoice_type__in=['SALE', 'RETURN_SALE'],
            invoice__status='POSTED',
            invoice__date=day
        ).aggregate(
            gross_rev=Sum(Case(When(invoice__invoice_type='SALE', then='subtotal'), default=0, output_field=DecimalField())),
            ret_rev=Sum(Case(When(invoice__invoice_type='RETURN_SALE', then='subtotal'), default=0, output_field=DecimalField())),
            gross_cogs=Sum(Case(When(invoice__invoice_type='SALE', then='cogs_amount'), default=0, output_field=DecimalField())),
            ret_cogs=Sum(Case(When(invoice__invoice_type='RETURN_SALE', then='cogs_amount'), default=0, output_field=DecimalField())),
        )
        day_rev = (day_lines['gross_rev'] or Decimal('0')) - (day_lines['ret_rev'] or Decimal('0'))
        day_cogs = (day_lines['gross_cogs'] or Decimal('0')) - (day_lines['ret_cogs'] or Decimal('0'))
        day_profit = day_rev - day_cogs
        
        day_profit_margin = 0
        if day_rev > 0:
            day_profit_margin = float(day_profit / day_rev * 100)
        profit_data.append(round(day_profit_margin, 1))

    # Calculate overall avg profit margin (Net Revenue - Net COGS)
    all_time_lines = InvoiceLine.objects.filter(
        invoice__invoice_type__in=['SALE', 'RETURN_SALE'],
        invoice__status='POSTED'
    ).aggregate(
        gross_rev=Sum(Case(When(invoice__invoice_type='SALE', then='subtotal'), default=0, output_field=DecimalField())),
        ret_rev=Sum(Case(When(invoice__invoice_type='RETURN_SALE', then='subtotal'), default=0, output_field=DecimalField())),
        gross_cogs=Sum(Case(When(invoice__invoice_type='SALE', then='cogs_amount'), default=0, output_field=DecimalField())),
        ret_cogs=Sum(Case(When(invoice__invoice_type='RETURN_SALE', then='cogs_amount'), default=0, output_field=DecimalField())),
    )
    all_time_rev = (all_time_lines['gross_rev'] or Decimal('0')) - (all_time_lines['ret_rev'] or Decimal('0'))
    all_time_cogs = (all_time_lines['gross_cogs'] or Decimal('0')) - (all_time_lines['ret_cogs'] or Decimal('0'))
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

    # --- Low stock alerts (database-level aggregation, zero in-memory loop) ---
    from django.db.models import Subquery, OuterRef
    from django.db.models.functions import Coalesce

    batch_stock_subquery = InventoryBatch.objects.filter(
        product=OuterRef('pk'),
        quantity_remaining__gt=0
    ).values('product').annotate(
        total=Sum('quantity_remaining')
    ).values('total')

    stock_alerts_qs = Product.objects.filter(
        is_active=True,
        min_stock_level__gt=0
    ).annotate(
        current_stock=Coalesce(Subquery(batch_stock_subquery), Decimal('0'))
    ).filter(
        current_stock__lte=F('min_stock_level')
    ).select_related('category').order_by(
        F('current_stock') - F('min_stock_level')
    )[:10]

    stock_alerts = [
        {
            'product': p,
            'stock': p.current_stock,
            'min_stock': p.min_stock_level,
        }
        for p in stock_alerts_qs
    ]

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
    active_users_list = tenant_users_queryset(request).filter(is_active=True)[:5]
    users_with_branches = []
    for u in active_users_list:
        branch_obj = None
        if hasattr(u, 'employee_profile') and u.employee_profile:
            branch_obj = u.employee_profile.branch
        users_with_branches.append({
            'username': display_username(u),
            'get_full_name': u.get_full_name() or display_username(u),
            # Template shows "مدير" for is_superuser: reflect the tenant role.
            'is_superuser': u.is_superuser or getattr(getattr(u, 'employee_profile', None), 'role', '') == 'ADMIN',
            'is_staff': False,
            'branch': branch_obj
        })

    # --- FIFO Stock Valuation (Ultra-fast SQL aggregation for top 5) ---
    top_valued = list(
        InventoryBatch.objects.filter(quantity_remaining__gt=0).values('product_id').annotate(
            total_quantity=Sum('quantity_remaining'),
            total_value=Sum(F('quantity_remaining') * F('unit_cost')),
            oldest_date=Min('created_at')
        ).order_by('-total_value')[:5]
    )
    top_pids = [item['product_id'] for item in top_valued]
    prods_by_id = {p.id: p for p in Product.objects.filter(id__in=top_pids).select_related('category')}

    fifo_valuation = []
    for item in top_valued:
        p = prods_by_id.get(item['product_id'])
        if not p:
            continue
        tot_qty = item['total_quantity'] or Decimal('0')
        tot_val = item['total_value'] or Decimal('0')
        fifo_cost = (tot_val / tot_qty) if tot_qty > 0 else Decimal('0')
        oldest = item['oldest_date'].date() if item['oldest_date'] else today
        days_in_stock = (today - oldest).days
        if days_in_stock <= 30: aging_class = 'ok'
        elif days_in_stock <= 90: aging_class = 'moderate'
        elif days_in_stock <= 180: aging_class = 'warning'
        else: aging_class = 'critical'
        fifo_valuation.append({
            'product': p,
            'total_quantity': tot_qty,
            'fifo_cost': fifo_cost,
            'total_value': tot_val,
            'days_in_stock': days_in_stock,
            'aging_class': aging_class,
        })

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

class TenantUserQuerysetMixin:
    """Restrict user management to the current tenant's employees."""
    def get_queryset(self):
        return tenant_users_queryset(self.request)


class UserListView(LoginRequiredMixin, AdminRequiredMixin, TenantUserQuerysetMixin, ListView):
    model = User
    template_name = 'core/user_list.html'
    context_object_name = 'users'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'المستخدمون والصلاحيات'
        return context

class UserCreateView(LoginRequiredMixin, AdminRequiredMixin, CreateView):
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
        perms = Permission.objects.filter(content_type__app_label__in=['core', 'accounting', 'inventory', 'invoicing', 'partners', 'maintenance', 'einvoicing', 'hr', 'rentals', 'pricing'])\
                                  .exclude(content_type__model__in=excluded_models)\
                                  .select_related('content_type')
        
        APP_NAMES = {
            'core': 'إعدادات النظام والفروع',
            'accounting': 'الحسابات والمالية',
            'inventory': 'المخزون والأصناف',
            'invoicing': 'المبيعات والمشتريات',
            'partners': 'العملاء والموردين',
            'pricing': 'قوائم الأسعار والقطاعات',
            'maintenance': 'المعدات وتذاكر الصيانة',
            'einvoicing': 'الفاتورة والضرائب الإلكترونية',
            'hr': 'الموارد البشرية والرواتب',
            'rentals': 'تأجير المعدات والمساحات',
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

    def form_valid(self, form):
        messages.success(self.request, f'تم إنشاء المستخدم "{display_username(form.instance)}" بنجاح.')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'تعذر الحفظ، يرجى مراجعة الأخطاء الموضحة بالاستمارة.')
        return super().form_invalid(form)

class UserUpdateView(LoginRequiredMixin, AdminRequiredMixin, TenantUserQuerysetMixin, UpdateView):
    model = User
    form_class = EmployeeUserUpdateForm
    template_name = 'core/user_form.html'
    success_url = reverse_lazy('core:user_list')

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        # Defence in depth: only platform superusers may edit superusers.
        if obj.is_superuser and not self.request.user.is_superuser:
            raise Http404
        return obj

    def form_valid(self, form):
        if form.instance.pk == self.request.user.pk and not form.cleaned_data.get('is_active', True):
            form.add_error('is_active', 'لا يمكنك تعطيل حسابك الحالي.')
            return self.form_invalid(form)
        messages.success(self.request, f'تم تعديل بيانات المستخدم "{display_username(form.instance)}" بنجاح.')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'تعذر الحفظ، يرجى مراجعة الأخطاء الموضحة بالاستمارة.')
        return super().form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل بيانات وصلاحيات المستخدم'
        context['cancel_url'] = self.success_url
        
        from django.contrib.auth.models import Permission
        from collections import defaultdict
        
        excluded_models = ['logentry', 'contenttype', 'session', 'group', 'permission', 'employee']
        perms = Permission.objects.filter(content_type__app_label__in=['core', 'accounting', 'inventory', 'invoicing', 'partners', 'maintenance', 'einvoicing', 'hr', 'rentals', 'pricing'])\
                                  .exclude(content_type__model__in=excluded_models)\
                                  .select_related('content_type')
        
        APP_NAMES = {
            'core': 'إعدادات النظام والفروع',
            'accounting': 'الحسابات والمالية',
            'inventory': 'المخزون والأصناف',
            'invoicing': 'المبيعات والمشتريات',
            'partners': 'العملاء والموردين',
            'pricing': 'قوائم الأسعار والقطاعات',
            'maintenance': 'المعدات وتذاكر الصيانة',
            'einvoicing': 'الفاتورة والضرائب الإلكترونية',
            'hr': 'الموارد البشرية والرواتب',
            'rentals': 'تأجير المعدات والمساحات',
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
    if not is_admin_user(request.user):
        messages.error(request, 'عفواً، الدخول لإعدادات النظام مقتصر على الأدمن والمديرين فقط.')
        return redirect('core:main_screen')

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

class BranchListView(LoginRequiredMixin, AdminRequiredMixin, ListView):
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

class BranchCreateView(LoginRequiredMixin, AdminRequiredMixin, CreateView):
    model = Branch
    fields = ['name', 'code', 'address', 'phone', 'is_active']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('core:branch_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة فرع جديد'
        context['cancel_url'] = reverse_lazy('core:branch_list')
        return context

    def form_valid(self, form):
        messages.success(self.request, f'تم إنشاء الفرع "{form.instance.name}" بنجاح.')
        return super().form_valid(form)

class BranchUpdateView(LoginRequiredMixin, AdminRequiredMixin, UpdateView):
    model = Branch
    fields = ['name', 'code', 'address', 'phone', 'is_active']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('core:branch_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل الفرع'
        context['cancel_url'] = reverse_lazy('core:branch_list')
        return context

    def form_valid(self, form):
        messages.success(self.request, f'تم تعديل بيانات الفرع "{form.instance.name}" بنجاح.')
        return super().form_valid(form)

@login_required
@custom_permission_required('core.delete_branch', redirect_url='core:branch_list')
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


from django.http import JsonResponse

@login_required
def branch_status_api(request):
    """API returning branch counts (active and total) for dashboard."""
    from apps.tenant.core.models import Branch
    total = Branch.objects.count()
    active = Branch.objects.filter(is_active=True).count()
    return JsonResponse({'active': active, 'total': total})


@login_required
def warehouses_by_branch_api(request):
    """API returning warehouses for a specific branch."""
    from apps.tenant.inventory.models import Warehouse
    branch_id = request.GET.get('branch_id')
    warehouses = Warehouse.objects.filter(is_active=True, branch__in=_allowed_branches(request))
    if branch_id:
        try:
            warehouses = warehouses.filter(branch_id=int(branch_id))
        except (TypeError, ValueError):
            warehouses = warehouses.none()
    data = [{'id': w.id, 'name': w.name} for w in warehouses]
    return JsonResponse({'warehouses': data})


# =============================================================================
# DATABASE BACKUP & RESTORE VIEWS
# =============================================================================
# Backups are per tenant (see backup_service). Access: platform superuser or
# tenant ADMIN only. All mutating actions require POST.
from django.http import FileResponse, Http404
from django.views.decorators.http import require_POST, require_GET
from . import backup_service


def _backup_access_denied(request):
    if not is_admin_user(request.user):
        messages.error(request, 'عفواً، الوصول للنسخ الاحتياطي مقتصر على مديري النظام فقط.')
        return redirect('core:main_screen')
    return None


@login_required
@require_GET
def database_backup_view(request):
    """لوحة إدارة النسخ الاحتياطي لمساحة العمل الحالية"""
    denied = _backup_access_denied(request)
    if denied:
        return denied

    try:
        db_info = backup_service.get_current_db_info()
        backups = backup_service.list_backups()
    except backup_service.BackupError as e:
        messages.error(request, str(e))
        return redirect('core:main_screen')

    context = {
        'title': 'النسخ الاحتياطي لقاعدة البيانات',
        'db_info': db_info,
        'backups': backups,
        # Server details are only shown to platform superusers (template).
        'pg_dump_path': backup_service.find_pg_tool('pg_dump') if request.user.is_superuser else None,
        'backup_dir': db_info.get('schema'),
        'max_upload_mb': backup_service.MAX_UPLOAD_BYTES // (1024 * 1024),
    }
    return render(request, 'core/database_backup.html', context)


@login_required
@require_POST
def database_backup_create(request):
    """أخذ نسخة احتياطية فورية لمساحة العمل الحالية"""
    denied = _backup_access_denied(request)
    if denied:
        return denied

    success, msg_or_filename, filepath = backup_service.create_backup()
    if not success:
        messages.error(request, f'❌ فشل أخذ النسخة الاحتياطية: {msg_or_filename}')
        return redirect('core:database_backup')

    messages.success(request, f'✅ تم أخذ النسخة الاحتياطية بنجاح: {msg_or_filename}')
    if request.POST.get('download') == '1':
        return _bundle_response(request, msg_or_filename)
    return redirect('core:database_backup')


def _bundle_response(request, filename):
    try:
        tmp_path, download_name = backup_service.build_download_bundle(filename)
    except backup_service.BackupError as e:
        messages.error(request, str(e))
        return redirect('core:database_backup')

    class _TempFile:
        """File wrapper that deletes the temp zip once the response is closed."""
        def __init__(self, path):
            self._fh = open(path, 'rb')
            self._path = path

        def read(self, *args):
            return self._fh.read(*args)

        def close(self):
            try:
                self._fh.close()
            finally:
                try:
                    os.remove(self._path)
                except OSError:
                    pass

    return FileResponse(_TempFile(tmp_path), as_attachment=True, filename=download_name,
                        content_type='application/zip')


@login_required
@require_GET
def database_backup_download(request, filename):
    """تنزيل نسخة احتياطية كحزمة zip موقّعة (الملف + التوقيع)"""
    denied = _backup_access_denied(request)
    if denied:
        return denied
    try:
        backup_service.resolve_backup_path(filename)
    except backup_service.BackupError:
        raise Http404('ملف النسخة الاحتياطية غير موجود.')
    return _bundle_response(request, filename)


@login_required
@require_POST
def database_backup_delete(request, filename):
    """حذف ملف نسخة احتياطية من مجلد مساحة العمل الحالية"""
    denied = _backup_access_denied(request)
    if denied:
        return denied
    try:
        name = backup_service.delete_backup(filename)
        messages.success(request, f'🗑️ تم حذف ملف النسخة الاحتياطية: {name}')
    except backup_service.BackupError as e:
        messages.warning(request, str(e))
    except Exception:
        backup_service.logger.exception('Backup delete failed')
        messages.error(request, backup_service.GENERIC_ERROR)
    return redirect('core:database_backup')


@login_required
@require_POST
def database_backup_restore(request, filename=None):
    """استعادة بيانات مساحة العمل من نسخة موقّعة (محفوظة أو حزمة zip مرفوعة)"""
    denied = _backup_access_denied(request)
    if denied:
        return denied

    try:
        uploaded_file = request.FILES.get('backup_file')
        if uploaded_file:
            target_path = backup_service.import_uploaded_bundle(uploaded_file)
        elif filename:
            target_path = backup_service.resolve_backup_path(filename)
        else:
            messages.warning(request, 'يرجى اختيار ملف لاستعادة قاعدة البيانات منه.')
            return redirect('core:database_backup')
    except backup_service.BackupError as e:
        messages.error(request, f'❌ {e}')
        return redirect('core:database_backup')
    except Exception:
        backup_service.logger.exception('Backup upload/lookup failed')
        messages.error(request, f'❌ {backup_service.GENERIC_ERROR}')
        return redirect('core:database_backup')

    ok, msg = backup_service.restore_backup(target_path)
    if ok:
        messages.success(request, f'✅ {msg}')
    else:
        messages.error(request, f'❌ {msg}')
    return redirect('core:database_backup')
