"""شاشات الإدارة لبيع المناديب: طلبات التحميل والتفريغ وتوريد النقدية (القواعد في van_services)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.tenant.core.access import filter_by_branch
from apps.tenant.core.decorators import custom_permission_required
from apps.tenant.inventory.models import Warehouse

from . import van_services as van
from .models import LoadingOrder, RepCashHandover, SalesRep, UnloadingOrder


def _scoped(qs, request):
    return filter_by_branch(qs, request, 'rep__employee__branch')


def _error(request, e):
    messages.error(request, ' '.join(e.messages) if isinstance(e, ValidationError) else str(e))


def _status_list(request, model, template, title, open_statuses):
    status = request.GET.get('status') or 'OPEN'
    qs = _scoped(model.objects.select_related('rep__employee__user', 'from_warehouse', 'to_warehouse'), request)
    rep_id = request.GET.get('rep') or ''
    if rep_id.isdigit():
        qs = qs.filter(rep_id=int(rep_id))
    if status == 'OPEN':
        qs = qs.filter(status__in=open_statuses)
    elif status != 'ALL':
        qs = qs.filter(status=status)
    qs = qs.annotate(lines_count=Count('lines')).order_by('-date', '-id')
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    return render(request, template, {
        'title': title, 'orders': page, 'page_obj': page, 'status': status, 'rep_id': rep_id,
        'status_choices': model.STATUS_CHOICES,
        'reps': filter_by_branch(SalesRep.objects.select_related('employee__user'), request, 'employee__branch').filter(is_active=True),
    })


@login_required
@custom_permission_required('invoicing.view_loadingorder', redirect_url='core:main_screen')
def loading_list(request):
    return _status_list(request, LoadingOrder, 'invoicing/loading_list.html', 'طلبات التحميل',
                        [LoadingOrder.PENDING, LoadingOrder.APPROVED])


@login_required
@custom_permission_required('invoicing.view_loadingorder', redirect_url='core:main_screen')
def loading_detail(request, pk):
    order = get_object_or_404(_scoped(LoadingOrder.objects.select_related(
        'rep__employee__user', 'from_warehouse', 'to_warehouse', 'requested_by', 'approved_by', 'issued_by',
        'transfer'), request), pk=pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'approve' and request.user.has_perm('invoicing.approve_loadingorder'):
                van.approve_loading(order, {l.id: request.POST.get(f'qty_{l.id}', l.requested_qty)
                                            for l in order.lines.all()}, request.user)
                messages.success(request, f'اتعمد طلب التحميل {order.number} — مستني الصرف من المخزن.')
            elif action == 'issue' and request.user.has_perm('invoicing.issue_loadingorder'):
                order = van.issue_loading(order, request.user)
                messages.success(request, f'اتصرف الطلب واترحل على عربية المندوب ({order.transfer.number}).')
            elif action == 'cancel' and (request.user.has_perm('invoicing.approve_loadingorder')
                                         or request.user.has_perm('invoicing.issue_loadingorder')):
                van.cancel_loading(order, request.user)
                messages.success(request, 'اتلغى الطلب.')
            else:
                messages.error(request, 'عفواً، لا تملك الصلاحية.')
        except ValidationError as e:
            _error(request, e)
        return redirect('invoicing:loading_detail', pk=order.pk)
    lines = list(order.lines.select_related('product', 'uom__uom'))
    available = van.van_stock(order.from_warehouse, [l.product_id for l in lines])
    van_has = van.van_stock(order.to_warehouse, [l.product_id for l in lines])
    for l in lines:
        l.available = available.get(l.product_id) or 0
        l.in_van = van_has.get(l.product_id) or 0
    return render(request, 'invoicing/loading_detail.html', {
        'title': f'طلب تحميل {order.number}', 'order': order, 'lines': lines,
        'can_approve': request.user.has_perm('invoicing.approve_loadingorder'),
        'can_issue': request.user.has_perm('invoicing.issue_loadingorder'),
    })


@login_required
@custom_permission_required('invoicing.view_unloadingorder', redirect_url='core:main_screen')
def unloading_list(request):
    return _status_list(request, UnloadingOrder, 'invoicing/unloading_list.html', 'طلبات التفريغ',
                        [UnloadingOrder.PENDING])


@login_required
@custom_permission_required('invoicing.view_unloadingorder', redirect_url='core:main_screen')
def unloading_detail(request, pk):
    order = get_object_or_404(_scoped(UnloadingOrder.objects.select_related(
        'rep__employee__user', 'from_warehouse', 'to_warehouse', 'damaged_warehouse', 'requested_by',
        'received_by', 'transfer', 'damaged_transfer'), request), pk=pk)
    warehouses = filter_by_branch(Warehouse.objects.filter(is_active=True), request).exclude(
        pk=order.from_warehouse_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if not request.user.has_perm('invoicing.receive_unloadingorder'):
                messages.error(request, 'عفواً، لا تملك الصلاحية.')
            elif action == 'receive':
                dmg = request.POST.get('damaged_warehouse') or ''
                order = van.receive_unloading(
                    order, {l.id: (request.POST.get(f'sound_{l.id}', l.sound_qty),
                                   request.POST.get(f'damaged_{l.id}', l.damaged_qty)) for l in order.lines.all()},
                    request.user, damaged_warehouse=warehouses.filter(pk=dmg).first() if dmg.isdigit() else None)
                messages.success(request, f'اتستلم طلب التفريغ {order.number} واترحل.')
            elif action == 'cancel':
                van.cancel_unloading(order, request.user)
                messages.success(request, 'اتلغى الطلب.')
        except ValidationError as e:
            _error(request, e)
        return redirect('invoicing:unloading_detail', pk=order.pk)
    lines = list(order.lines.select_related('product'))
    in_van = van.van_stock(order.from_warehouse, [l.product_id for l in lines])
    for l in lines:
        l.in_van = in_van.get(l.product_id) or 0
    return render(request, 'invoicing/unloading_detail.html', {
        'title': f'طلب تفريغ {order.number}', 'order': order, 'lines': lines, 'warehouses': warehouses,
        'can_receive': request.user.has_perm('invoicing.receive_unloadingorder'),
    })


@login_required
@custom_permission_required('invoicing.view_repcashhandover', redirect_url='core:main_screen')
def handover_list(request):
    from apps.tenant.accounting.models import BankAccount, Treasury
    reps = list(filter_by_branch(SalesRep.objects.select_related('employee__user', 'treasury__account'), request,
                                 'employee__branch')
                .filter(is_active=True).exclude(treasury=None))
    if request.method == 'POST':
        if not request.user.has_perm('invoicing.add_repcashhandover'):
            messages.error(request, 'عفواً، لا تملك صلاحية استلام التوريد.')
            return redirect('invoicing:handover_list')
        rep = next((r for r in reps if str(r.pk) == request.POST.get('rep')), None)
        dest = request.POST.get('destination') or ''
        treasury = bank = None
        if dest.startswith('t:') and dest[2:].isdigit():
            treasury = filter_by_branch(Treasury.objects.all(), request).filter(pk=int(dest[2:])).first()
        elif dest.startswith('b:') and dest[2:].isdigit():
            bank = BankAccount.objects.filter(pk=int(dest[2:])).first()
        try:
            if rep is None:
                raise ValidationError('اختار المندوب.')
            h = van.post_handover(rep, request.POST.get('amount'), request.user, treasury=treasury, bank_account=bank,
                                  charge_shortage=bool(request.POST.get('charge_shortage')),
                                  notes=request.POST.get('notes', ''))
            msg = f'اتسجل التوريد {h.number}: {h.amount:.2f}'
            if h.shortage_charged:
                msg += f' — وعجز {h.shortage_charged:.2f} اتحمّل على المندوب (هيتخصم من مرتبه)'
            messages.success(request, msg + '.')
        except ValidationError as e:
            _error(request, e)
        return redirect('invoicing:handover_list')
    cash = van.cash_balances(reps)
    balances = [{'rep': r, 'balance': cash.get(r.id, van.ZERO)} for r in reps]
    qs = _scoped(RepCashHandover.objects.select_related('rep__employee__user', 'treasury', 'bank_account',
                                                        'created_by', 'journal_entry'), request)
    rep_id = request.GET.get('rep') or ''
    if rep_id.isdigit():
        qs = qs.filter(rep_id=int(rep_id))
    page = Paginator(qs.order_by('-date', '-id'), 30).get_page(request.GET.get('page'))
    rep_treasuries = [r.treasury_id for r in reps]
    return render(request, 'invoicing/rep_handovers.html', {
        'title': 'توريدات المناديب', 'balances': balances, 'handovers': page, 'page_obj': page, 'rep_id': rep_id,
        'treasuries': filter_by_branch(Treasury.objects.all(), request).exclude(
            Q(pk__in=rep_treasuries) | Q(sales_reps__isnull=False)).distinct(),
        'banks': BankAccount.objects.all(),
        'can_add': request.user.has_perm('invoicing.add_repcashhandover'),
    })
