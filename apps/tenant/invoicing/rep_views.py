"""مناديب المبيعات (invoicing.rep_services)."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.tenant.core.access import filter_by_branch
from apps.tenant.core.decorators import custom_permission_required
from apps.tenant.core.models import Employee
from apps.tenant.inventory.models import InventoryBatch, Warehouse

from apps.tenant.accounting.models import Treasury

from . import rep_services as svc
from . import van_services as van
from .models import Invoice, SalesRep

AR_MONTHS = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']


def _period(request):
    today = timezone.localdate()
    try:
        y, m = int(request.GET.get('year') or today.year), int(request.GET.get('month') or today.month)
        if 1 <= m <= 12:
            return y, m
    except ValueError:
        pass
    return today.year, today.month


def _pick(model, request, field):
    v = request.POST.get(field) or ''
    return model.objects.filter(pk=int(v)).first() if v.isdigit() else None


def _reps(request):
    return filter_by_branch(SalesRep.objects.select_related('employee__user', 'employee__branch', 'van_warehouse'),
                            request, 'employee__branch')


@login_required
@custom_permission_required('invoicing.view_salesrep', redirect_url='core:main_screen')
def rep_list(request):
    if request.method == 'POST':
        if not request.user.has_perm('invoicing.add_salesrep'):
            messages.error(request, 'عفواً، لا تملك صلاحية إضافة مناديب.')
            return redirect('invoicing:rep_list')
        emp = filter_by_branch(Employee.objects.all(), request).filter(pk=request.POST.get('employee') or 0).first()
        if emp is None or SalesRep.objects.filter(employee=emp).exists():
            messages.error(request, 'اختار موظف مش مندوب بالفعل.')
        else:
            try:
                rep = SalesRep.objects.create(
                    employee=emp, van_warehouse=_pick(Warehouse, request, 'van_warehouse'),
                    treasury=_pick(Treasury, request, 'treasury'),
                    source_warehouse=_pick(Warehouse, request, 'source_warehouse'),
                    commission_rate=Decimal(request.POST.get('commission_rate') or '0'),
                    monthly_target=Decimal(request.POST.get('monthly_target') or '0'))
                van.ensure_rep_setup(rep, create_van=request.POST.get('van_warehouse') == 'new',
                                     create_treasury=request.POST.get('treasury') == 'new')
                messages.success(request, 'اتضاف المندوب. خلّي دوره «مندوب مبيعات» من شاشة المستخدمين عشان يدخل على واجهة الموبايل.')
            except InvalidOperation:
                messages.error(request, 'نسبة أو تارجت غير صالح.')
        return redirect('invoicing:rep_list')
    year, month = _period(request)
    reps = list(_reps(request))
    perf = svc.month_performance(reps, year, month)
    rows = sorted(perf.values(), key=lambda r: -r['net'])
    totals = {k: sum((r[k] for r in rows), Decimal('0')) for k in ('net', 'target', 'commission')}
    used = SalesRep.objects.values_list('employee_id', flat=True)
    return render(request, 'invoicing/rep_list.html', {
        'title': 'مناديب المبيعات', 'rows': rows, 'totals': totals, 'year': year, 'month': month,
        'month_name': AR_MONTHS[month - 1], 'months': list(enumerate(AR_MONTHS, start=1)),
        'employees': filter_by_branch(Employee.objects.filter(user__is_active=True).exclude(id__in=used)
                                      .select_related('user'), request),
        'warehouses': filter_by_branch(Warehouse.objects.filter(is_active=True), request),
        'treasuries': filter_by_branch(Treasury.objects.all(), request),
        'can_add': request.user.has_perm('invoicing.add_salesrep'),
    })


@login_required
@custom_permission_required('invoicing.view_salesrep', redirect_url='core:main_screen')
def rep_detail(request, pk):
    rep = get_object_or_404(_reps(request), pk=pk)
    year, month = _period(request)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'settings' and request.user.has_perm('invoicing.change_salesrep'):
                rep.van_warehouse = _pick(Warehouse, request, 'van_warehouse')
                rep.treasury = _pick(Treasury, request, 'treasury')
                rep.source_warehouse = _pick(Warehouse, request, 'source_warehouse')
                if rep.source_warehouse_id and rep.source_warehouse_id == rep.van_warehouse_id:
                    raise ValidationError('مخزن التحميل لازم يكون غير مخزن العربية.')
                rep.commission_rate = Decimal(request.POST.get('commission_rate') or '0')
                rep.monthly_target = Decimal(request.POST.get('monthly_target') or '0')
                rep.is_active = bool(request.POST.get('is_active'))
                rep.save()
                van.ensure_rep_setup(rep, create_van=request.POST.get('van_warehouse') == 'new',
                                     create_treasury=request.POST.get('treasury') == 'new')
                messages.success(request, 'اتحفظت إعدادات المندوب.')
            elif action == 'approve' and request.user.has_perm('invoicing.add_repcommission'):
                c = svc.approve_commission(rep, int(request.POST.get('year')), int(request.POST.get('month')),
                                           user=request.user)
                messages.success(request, f'اتعمدت عمولة {c.month}/{c.year}: {c.amount} — هتتضاف لمرتبه في المسير.')
            else:
                messages.error(request, 'عفواً، لا تملك الصلاحية.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (InvalidOperation, ValueError, TypeError):
            messages.error(request, 'بيانات غير صالحة.')
        return redirect(f'{request.path}?year={year}&month={month}')
    perf = svc.month_performance([rep], year, month)[rep.id]
    van_stock = []
    if rep.van_warehouse_id:
        van_stock = list(InventoryBatch.objects.filter(warehouse=rep.van_warehouse, quantity_remaining__gt=0)
                         .values('product__name').annotate(q=Sum('quantity_remaining')).order_by('product__name'))
    main_wh = filter_by_branch(Warehouse.objects.filter(is_active=True), request).exclude(
        pk=rep.van_warehouse_id).first()
    invoices = (Invoice.objects.filter(sales_rep=rep, status=Invoice.POSTED, date__year=year, date__month=month)
                .select_related('partner').order_by('-date', '-id')[:30])
    from .models import LoadingOrder, RepCashHandover, UnloadingOrder
    return render(request, 'invoicing/rep_detail.html', {
        'title': f'المندوب {rep}', 'rep': rep, 'perf': perf, 'year': year, 'month': month,
        'cash': van.cash_balance(rep), 'source_wh': van.default_source_warehouse(rep),
        'treasuries': filter_by_branch(Treasury.objects.all(), request),
        'loadings': LoadingOrder.objects.filter(rep=rep).order_by('-id')[:8],
        'unloadings': UnloadingOrder.objects.filter(rep=rep).order_by('-id')[:5],
        'handovers': RepCashHandover.objects.filter(rep=rep).order_by('-id')[:5],
        'month_name': AR_MONTHS[month - 1], 'months': list(enumerate(AR_MONTHS, start=1)),
        'van_stock': van_stock, 'main_wh': main_wh, 'invoices': invoices,
        'warehouses': filter_by_branch(Warehouse.objects.filter(is_active=True), request),
        'can_change': request.user.has_perm('invoicing.change_salesrep'),
        'can_approve': request.user.has_perm('invoicing.add_repcommission'),
    })
