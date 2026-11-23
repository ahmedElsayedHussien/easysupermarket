"""
Multi-item stock transfer (تحويل مخزني متعدد الأصناف).

Branch rule (core.access): the SOURCE warehouse must belong to the current
branch (header branch for admins — any warehouse when the header is on "all
branches"; own branch for users). The destination may be any active warehouse,
including another branch's (inter-branch transfer).
"""
import datetime
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.tenant.core.access import filter_by_branch
from apps.tenant.core.decorators import custom_permission_required

from .models import Product, ProductUoM, StockMovement, StockTransfer, StockTransferLine, Warehouse

MAX_LINES = 300


class _TransferAbort(Exception):
    pass


def _source_warehouses(request):
    return filter_by_branch(Warehouse.objects.filter(is_active=True).select_related('branch'), request)


def _visible_transfers(request):
    """Transfers touching a warehouse of the current branch scope (source OR destination)."""
    allowed = _source_warehouses(request)
    return StockTransfer.objects.filter(Q(from_warehouse__in=allowed) | Q(to_warehouse__in=allowed)).distinct()


def _parse_lines(post):
    product_ids = post.getlist('product_id[]')
    uom_ids = post.getlist('uom_id[]')
    quantities = post.getlist('quantity[]')
    lines = []
    for i, pid in enumerate(product_ids):
        pid = (pid or '').strip()
        raw_qty = (quantities[i] if i < len(quantities) else '').strip().replace(',', '')
        if not pid and not raw_qty:
            continue  # empty row
        if not pid.isdigit():
            raise _TransferAbort(f'السطر {i + 1}: اختر الصنف.')
        try:
            qty = Decimal(raw_qty)
        except (InvalidOperation, ValueError):
            raise _TransferAbort(f'السطر {i + 1}: الكمية غير صالحة.')
        if not qty.is_finite() or qty <= 0:
            raise _TransferAbort(f'السطر {i + 1}: الكمية يجب أن تكون أكبر من صفر.')
        uom = (uom_ids[i] if i < len(uom_ids) else '').strip()
        lines.append({'row': i + 1, 'product_id': int(pid), 'uom_id': uom if uom.isdigit() else None, 'qty': qty})
    if not lines:
        raise _TransferAbort('أضف صنفاً واحداً على الأقل.')
    if len(lines) > MAX_LINES:
        raise _TransferAbort(f'الحد الأقصى {MAX_LINES} سطر في التحويل الواحد.')
    return lines


def _posted_rows(post):
    """Rebuild the entered rows after a failed submit (nothing is lost)."""
    ids = [p for p in post.getlist('product_id[]') if (p or '').isdigit()]
    products = {p.id: p for p in Product.objects.filter(id__in=ids).prefetch_related('uoms__uom')}
    rows = []
    for i, pid in enumerate(post.getlist('product_id[]')):
        if not (pid or '').isdigit() or int(pid) not in products:
            continue
        p = products[int(pid)]
        uoms = [{'id': 'base', 'name': p.pos_unit_name, 'factor': 1.0}] + [
            {'id': u.id, 'name': u.uom.name, 'factor': float(u.conversion_factor or 1)} for u in p.uoms.all() if not u.is_base]
        q = post.getlist('quantity[]')
        u = post.getlist('uom_id[]')
        rows.append({'id': p.id, 'name': p.name, 'barcode': p.barcode or '', 'uoms': uoms,
                     'quantity': q[i] if i < len(q) else '', 'uom_id': u[i] if i < len(u) else 'base'})
    return rows


@login_required
@custom_permission_required('inventory.change_inventorybatch', redirect_url='core:main_screen')
def stock_transfer_create(request):
    from apps.tenant.services.fifo_engine import transfer_stock

    sources = _source_warehouses(request)
    destinations = Warehouse.objects.filter(is_active=True).select_related('branch').order_by('branch__name', 'name')

    if request.method == 'POST':
        try:
            from_wh = sources.filter(pk=request.POST.get('from_warehouse') or 0).first()
            to_wh = destinations.filter(pk=request.POST.get('to_warehouse') or 0).first()
            if from_wh is None:
                raise _TransferAbort('اختر مستودع المصدر (من مستودعات الفرع الحالي).')
            if to_wh is None:
                raise _TransferAbort('اختر مستودع الوجهة.')
            if from_wh.pk == to_wh.pk:
                raise _TransferAbort('لا يمكن التحويل من وإلى نفس المستودع.')
            lines = _parse_lines(request.POST)

            products = {p.id: p for p in Product.objects.filter(id__in=[l['product_id'] for l in lines], is_active=True)}
            uoms = {str(u.id): u for u in ProductUoM.objects.select_related('uom')
                    .filter(id__in=[l['uom_id'] for l in lines if l['uom_id']])}

            # merge duplicate rows of the same product (in base units)
            merged = OrderedDict()
            for l in lines:
                product = products.get(l['product_id'])
                if product is None:
                    raise _TransferAbort(f'السطر {l["row"]}: الصنف غير موجود أو غير نشط.')
                if product.product_type == 'SERVICE':
                    raise _TransferAbort(f'السطر {l["row"]}: "{product.name}" خدمة وليس لها مخزون.')
                uom = uoms.get(l['uom_id']) if l['uom_id'] else None
                if uom is not None and uom.product_id != product.id:
                    raise _TransferAbort(f'السطر {l["row"]}: الوحدة لا تخص الصنف "{product.name}".')
                factor = (uom.conversion_factor if uom is not None else Decimal('1')) or Decimal('1')
                base_qty = l['qty'] * factor
                if product.product_type == 'SERIALIZED' and base_qty != base_qty.to_integral_value():
                    raise _TransferAbort(f'السطر {l["row"]}: "{product.name}" بالسيريال — الكمية يجب أن تكون عدداً صحيحاً.')
                key = product.id
                if key in merged:
                    merged[key]['base_qty'] += base_qty
                    merged[key]['qty'] += base_qty
                    merged[key]['uom'], merged[key]['factor'] = None, Decimal('1')  # shown in base unit
                else:
                    merged[key] = {'product': product, 'uom': uom, 'factor': factor, 'qty': l['qty'], 'base_qty': base_qty}

            with transaction.atomic():
                doc = StockTransfer.objects.create(
                    date=parse_date(request.POST.get('date') or '') or datetime.date.today(),
                    from_warehouse=from_wh, to_warehouse=to_wh,
                    notes=(request.POST.get('notes') or '').strip(), created_by=request.user,
                )
                total = Decimal('0')
                for item in merged.values():
                    product = item['product']
                    try:
                        transfer_stock(product, from_wh, to_wh, item['base_qty'], reference=doc.number)
                    except ValueError as e:
                        raise _TransferAbort(f'"{product.name}": {e}')
                    moved = StockMovement.objects.filter(reference=doc.number, product=product,
                                                         movement_type='TRANSFER_OUT')
                    cost = sum(((m.quantity or 0) * (m.unit_cost or 0) for m in moved), Decimal('0'))
                    unit_cost = (cost / item['base_qty']) if item['base_qty'] else Decimal('0')
                    StockTransferLine.objects.create(
                        transfer=doc, product=product, uom=item['uom'], quantity=item['qty'],
                        conversion_factor=item['factor'], base_quantity=item['base_qty'], unit_cost=unit_cost,
                    )
                    total += cost
                doc.total_cost = total
                doc.save(update_fields=['total_cost'])
            messages.success(request, f'تم تنفيذ التحويل {doc.number}: {len(merged)} صنف من {from_wh.name} إلى {to_wh.name}.')
            return redirect('inventory:stock_transfer_detail', pk=doc.pk)
        except _TransferAbort as e:
            messages.error(request, f'لم يتم التحويل (لم يتحرك أي صنف): {e}')

    context = {
        'title': 'تحويل مخزني',
        'sources': sources,
        'destinations': destinations,
        'posted': request.POST if request.method == 'POST' else None,
        'posted_rows': _posted_rows(request.POST) if request.method == 'POST' else [],
        'max_lines': MAX_LINES,
    }
    return render(request, 'inventory/transfer.html', context)


@login_required
@custom_permission_required('inventory.view_inventorybatch', redirect_url='core:main_screen')
def stock_transfer_list(request):
    qs = (_visible_transfers(request).select_related('from_warehouse__branch', 'to_warehouse__branch', 'created_by')
          .annotate(n_lines=Count('lines')))
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(lines__product__name__icontains=q) | Q(notes__icontains=q)).distinct()
    page_obj = Paginator(qs.order_by('-date', '-id'), 25).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    context = {
        'title': 'سجل التحويلات المخزنية',
        'transfers': page_obj,
        'page_obj': page_obj,
        'page_range': page_obj.paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1),
        'page_query': params.urlencode(),
        'total_count': page_obj.paginator.count,
        'q': q,
    }
    return render(request, 'inventory/stock_transfer_list.html', context)


@login_required
@custom_permission_required('inventory.view_inventorybatch', redirect_url='core:main_screen')
def stock_transfer_detail(request, pk):
    doc = get_object_or_404(
        _visible_transfers(request).select_related('from_warehouse__branch', 'to_warehouse__branch', 'created_by'), pk=pk)
    lines = doc.lines.select_related('product', 'uom__uom')
    return render(request, 'inventory/stock_transfer_detail.html', {
        'title': f'تحويل مخزني {doc.number}', 'doc': doc, 'lines': lines,
    })
