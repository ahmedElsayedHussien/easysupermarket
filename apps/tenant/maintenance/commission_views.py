from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.utils import timezone
import datetime

from .commission_models import CommissionRule, CommissionRecord
from .commission_service import calculate_commissions
from apps.tenant.inventory.models import Category


# ---------------------------------------------------------------------------
# Commission Rules Management
# ---------------------------------------------------------------------------
@login_required
def commission_rules(request):
    categories = Category.objects.filter(is_active=True)
    rules = CommissionRule.objects.select_related('category').order_by('category__name')

    if request.method == 'POST':
        category_id    = request.POST.get('category_id')
        sales_milestone = request.POST.get('sales_milestone')
        commission_amount = request.POST.get('commission_amount')
        is_active = request.POST.get('is_active') == 'on'
        try:
            category = Category.objects.get(id=category_id)
            CommissionRule.objects.update_or_create(
                category=category,
                defaults={
                    'sales_milestone': sales_milestone,
                    'commission_amount': commission_amount,
                    'is_active': is_active,
                }
            )
            messages.success(request, f'تم حفظ قاعدة عمولة فئة "{category.name}" بنجاح.')
        except Exception as e:
            messages.error(request, str(e))
        return redirect('maintenance:commission_rules')

    context = {
        'categories': categories,
        'rules': rules,
        'title': 'إدارة قواعد العمولات',
    }
    return render(request, 'maintenance/commission_rules.html', context)


@login_required
def commission_rule_delete(request, pk):
    rule = get_object_or_404(CommissionRule, pk=pk)
    rule.delete()
    messages.success(request, 'تم حذف القاعدة.')
    return redirect('maintenance:commission_rules')


# ---------------------------------------------------------------------------
# Commission Report
# ---------------------------------------------------------------------------
@login_required
def commission_report(request):
    period      = request.GET.get('period', 'MONTHLY')
    date_str    = request.GET.get('date', '')

    try:
        target_date = datetime.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        target_date = timezone.now().date()

    branch = getattr(request, 'branch', None)
    report, start_date, end_date = calculate_commissions(period, target_date, branch=branch)

    context = {
        'report': report,
        'period': period,
        'target_date': target_date,
        'start_date': start_date,
        'end_date': end_date,
        'periods': [('DAILY', 'يومي'), ('MONTHLY', 'شهري'), ('YEARLY', 'سنوي')],
        'title': 'تقرير العمولات',
    }
    return render(request, 'maintenance/commission_report.html', context)
