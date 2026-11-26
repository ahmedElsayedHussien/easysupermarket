"""أوامر الشراء (invoicing.purchase_orders)."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.tenant.core.access import filter_by_branch, get_working_branch
from apps.tenant.core.decorators import custom_permission_required
from apps.tenant.inventory.models import Product, ProductUoM, Warehouse
from apps.tenant.partners.models import Partner

from . import purchase_orders as svc
from .models import PurchaseOrder

MONEY = DecimalField(max_digits=18, decimal_places=2)


def _orders(request):
    return filter_by_branch(PurchaseOrder.objects.select_related('supplier', 'warehouse', 'branch'), request)


def _warehouses(request):
    return filter_by_branch(Warehouse.objects.filter(is_active=True).select_related('branch'), request)


def _suppliers():
    return Partner.objects.filter(partner_type__in=[Partner.SUPPLIER, Partner.BOTH], is_active=True).order_by('name')


@login_required
@custom_permission_required('invoicing.view_purchaseorder', redirect_url='core:main_screen')
def purchase_order_list(request):
    status = request.GET.get('status', 'OPEN')
    qs = _orders(request).annotate(
        lines_count=Count('lines'),
        total=Coalesce(Sum(F('lines__quantity') * F('lines__unit_price'), output_field=MONEY), Value(Decimal('0')),
                       output_field=MONEY),
    ).order_by('-date', '-id')
    if status == 'OPEN':
        qs = qs.filter(status__in=PurchaseOrder.OPEN_STATUSES)
    elif status in dict(PurchaseOrder.STATUS_CHOICES):
        qs = qs.filter(status=status)
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    return render(request, 'invoicing/purchase_order_list.html', {
        'title': 'أوامر الشراء', 'orders': page, 'page_obj': page, 'status': status,
        'status_choices': PurchaseOrder.STATUS_CHOICES,
        'can_add': request.user.has_perm('invoicing.add_purchaseorder'),
    })


@login_required
@custom_permission_required('invoicing.add_purchaseorder', redirect_url='core:main_screen')
def purchase_order_create(request):
    warehouses = list(_warehouses(request))
    products = list(Product.objects.filter(is_active=True).exclude(product_type='SERVICE')
                    .prefetch_related('uoms__uom').order_by('name'))
    if request.method == 'POST':
        try:
            supplier = _suppliers().filter(pk=request.POST.get('supplier') or 0).first()
            warehouse = next((w for w in warehouses if str(w.id) == request.POST.get('warehouse')), None)
            if warehouse is None:
                raise ValidationError('اختار المخزن.')
            by_id = {str(p.id): p for p in products}
            uoms = {str(u.id): u for u in ProductUoM.objects.filter(product__in=products)}
            lines = [(by_id.get(pid), q, pr, uoms.get(u)) for pid, q, pr, u in zip(
                request.POST.getlist('product[]'), request.POST.getlist('qty[]'), request.POST.getlist('price[]'),
                request.POST.getlist('uom[]')) if pid and (q or '').strip()]
            expected = request.POST.get('expected_date') or None
            order = svc.create_order(supplier, warehouse, lines, user=request.user, expected_date=expected,
                                     notes=request.POST.get('notes', ''))
            messages.success(request, f'اتعمل أمر الشراء {order.number}.')
            return redirect('invoicing:purchase_order_detail', order.pk)
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (InvalidOperation, ValueError):
            messages.error(request, 'كمية أو سعر غير صالح.')
    branch = get_working_branch(request)
    return render(request, 'invoicing/purchase_order_form.html', {
        'title': 'أمر شراء جديد', 'suppliers': _suppliers(), 'warehouses': warehouses, 'products': products,
        'default_warehouse': next((w for w in warehouses if branch and w.branch_id == branch.id), None),
    })


@login_required
@custom_permission_required('invoicing.add_purchaseorder', redirect_url='core:main_screen')
def purchase_order_suggest(request):
    """النواقص: create one draft order per supplier from the ticked suggestions."""
    warehouses = list(_warehouses(request))
    wh_id = request.POST.get('warehouse') or request.GET.get('warehouse') or ''
    warehouse = next((w for w in warehouses if str(w.id) == wh_id), None)
    if warehouse is None:
        branch = get_working_branch(request)
        warehouse = next((w for w in warehouses if branch and w.branch_id == branch.id), None) or (
            warehouses[0] if warehouses else None)
    rows = svc.reorder_suggestions(warehouse) if warehouse else []
    if request.method == 'POST':
        ticked = set(request.POST.getlist('pick'))
        suppliers = {str(s.id): s for s in _suppliers()}
        groups = {}
        for r in rows:
            pid = str(r['product'].id)
            if pid not in ticked:
                continue
            sup = suppliers.get(request.POST.get(f'supplier_{pid}') or '')
            try:
                qty = Decimal(request.POST.get(f'qty_{pid}') or '0')
            except InvalidOperation:
                qty = Decimal('0')
            if sup is None or qty <= 0:
                continue
            groups.setdefault(sup, []).append((r['product'], qty, r['price'], None))
        made = []
        for sup, lines in groups.items():
            made.append(svc.create_order(sup, warehouse, lines, user=request.user,
                                         notes='من اقتراح النواقص'))
        if made:
            messages.success(request, 'اتعملت أوامر شراء: ' + '، '.join(o.number for o in made))
            return redirect(f"{reverse('invoicing:purchase_order_list')}?status=DRAFT")
        messages.error(request, 'اختار أصناف وحدد لكل واحد مورد وكمية.')
    return render(request, 'invoicing/purchase_order_suggest.html', {
        'title': 'اقتراح أوامر شراء من النواقص', 'rows': rows, 'warehouses': warehouses, 'warehouse': warehouse,
        'suppliers': _suppliers(),
    })


@login_required
@custom_permission_required('invoicing.view_purchaseorder', redirect_url='core:main_screen')
def purchase_order_detail(request, pk):
    order = get_object_or_404(_orders(request).prefetch_related('invoices'), pk=pk)
    if request.method == 'POST':
        if not request.user.has_perm('invoicing.change_purchaseorder'):
            messages.error(request, 'عفواً، لا تملك صلاحية تعديل أوامر الشراء.')
            return redirect('invoicing:purchase_order_detail', pk)
        action = request.POST.get('action')
        if action == 'sent' and order.status == PurchaseOrder.DRAFT:
            order.status = PurchaseOrder.SENT
            order.save(update_fields=['status'])
            messages.success(request, 'اتعلّم إن الأمر اتبعت للمورد.')
        elif action == 'cancel' and order.status in PurchaseOrder.OPEN_STATUSES:
            order.status = PurchaseOrder.CANCELLED
            order.save(update_fields=['status'])
            messages.success(request, 'اتلغى أمر الشراء.')
        elif action == 'close' and order.status == PurchaseOrder.PARTIAL:
            order.status = PurchaseOrder.RECEIVED
            order.save(update_fields=['status'])
            messages.success(request, 'اتقفل الأمر (الباقي مش هيوصل).')
        return redirect('invoicing:purchase_order_detail', pk)
    amount = F('quantity') * F('unit_price')
    lines = list(order.lines.select_related('product', 'uom__uom').annotate(amount=amount))
    total = order.lines.aggregate(t=Coalesce(Sum(amount, output_field=MONEY), Value(Decimal('0')), output_field=MONEY))['t']
    from apps.tenant.core.company import company_profile
    return render(request, 'invoicing/purchase_order_detail.html', {
        'title': f'أمر الشراء {order.number}', 'order': order, 'lines': lines, 'total': total,
        'company': company_profile(request),
        'can_change': request.user.has_perm('invoicing.change_purchaseorder'),
        'can_receive': request.user.has_perm('invoicing.add_purchase_invoice') and order.status in PurchaseOrder.OPEN_STATUSES,
    })
