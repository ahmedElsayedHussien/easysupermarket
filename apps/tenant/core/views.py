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

    # --- Today's sale count ---
    today_sale_count = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED',
        date=today
    ).count()

    # --- Today's purchase total ---
    today_purchases = Invoice.objects.filter(
        invoice_type='PURCHASE',
        status='POSTED',
        date=today
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

    # --- Low stock alerts ---
    stock_alerts = []
    for product in Product.objects.filter(is_active=True).select_related('category'):
        stock = product.get_stock()
        if stock <= product.min_stock_level and product.min_stock_level > 0:
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
        'today_sale_count': today_sale_count,
        'today_purchases': today_purchases,
        'stock_alerts': stock_alerts[:5],
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
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.inventory.models import InventoryBatch, Product

    today = timezone.now().date()
    branches = Branch.objects.filter(is_active=True)

    # --- Sales for the last 7 days (for chart) ---
    sales_data = []
    labels = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_sales = Invoice.objects.filter(
            invoice_type='SALE',
            status='POSTED',
            date=day
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        sales_data.append(float(day_sales))
        labels.append(day.strftime('%Y-%m-%d'))

    # --- Top branches by revenue (all time) ---
    top_branches = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED'
    ).values('branch__name').annotate(
        revenue=Sum('total_amount'),
        tx_count=Count('id')
    ).order_by('-revenue')[:5]

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

    active_branches = branches.count()
    total_branches = Branch.objects.count()

    total_products = Product.objects.filter(is_active=True).count()

    context = {
        'user_role': 'Admin',
        'title': 'لوحة التحكم الإدارية',
        'today': today,
        'sales_chart_labels': json.dumps(labels),
        'sales_chart_data': json.dumps(sales_data),
        'top_branches': list(top_branches),
        'stock_alerts': stock_alerts,
        'alerts_count': len(stock_alerts),
        'active_branches': active_branches,
        'total_branches': total_branches,
        'total_revenue_month': total_revenue_month,
        'total_purchase_month': total_purchase_month,
        'total_products': total_products,
    }
    return render(request, 'dashboard/admin_dashboard.html', context)


@login_required
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
        perms = Permission.objects.filter(content_type__app_label__in=['core', 'accounting', 'inventory', 'invoicing', 'partners'])
        grouped = defaultdict(list)
        for p in perms:
            grouped[p.content_type.app_label].append(p)
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
        perms = Permission.objects.filter(content_type__app_label__in=['core', 'accounting', 'inventory', 'invoicing', 'partners'])
        grouped = defaultdict(list)
        for p in perms:
            grouped[p.content_type.app_label].append(p)
        context['grouped_permissions'] = dict(grouped)
        return context

class ReportsIndexView(AdminRequiredMixin, View):
    """
    Main reports dashboard view. Serves as a structural skeleton for tabs.
    """
    def get(self, request, *args, **kwargs):
        context = {
            'title': 'التقارير المجمعة',
        }
        return render(request, 'core/reports.html', context)

@login_required
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

class BranchListView(LoginRequiredMixin, ListView):
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

class BranchCreateView(LoginRequiredMixin, CreateView):
    model = Branch
    fields = ['name', 'code', 'address', 'phone', 'is_active']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('core:branch_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة فرع جديد'
        context['cancel_url'] = reverse_lazy('core:branch_list')
        return context

class BranchUpdateView(LoginRequiredMixin, UpdateView):
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

from django.contrib.auth.models import User
from .forms import EmployeeUserCreationForm, EmployeeUserUpdateForm

class UserListView(LoginRequiredMixin, ListView):
    model = User
    template_name = 'core/generic_list.html'
    context_object_name = 'objects'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'المستخدمين'
        context['create_url'] = reverse_lazy('core:user_create')
        context['update_url_name'] = 'core:user_update'
        return context

class UserCreateView(LoginRequiredMixin, CreateView):
    model = User
    form_class = EmployeeUserCreationForm
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('core:user_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة مستخدم'
        context['cancel_url'] = self.success_url
        return context

class UserUpdateView(LoginRequiredMixin, UpdateView):
    model = User
    form_class = EmployeeUserUpdateForm
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('core:user_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل مستخدم'
        context['cancel_url'] = self.success_url
        return context
