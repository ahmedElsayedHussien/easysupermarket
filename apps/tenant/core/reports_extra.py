"""
تقارير مخزون إضافية: دوران المخزون، الهالك والعجز، وتتبع التشغيلة.
All sums are done by the database (grouped queries, no per-row queries).
"""
import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Case, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.utils import timezone

from .decorators import custom_permission_required
from apps.tenant.core.money import money

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=20, decimal_places=2)
QTY = DecimalField(max_digits=20, decimal_places=4)


def _dates(request, default_days=30):
    today = timezone.localdate()

    def parse(name, default):
        try:
            return datetime.date.fromisoformat(request.GET.get(name) or '')
        except ValueError:
            return default
    return parse('date_from', today - datetime.timedelta(days=default_days - 1)), parse('date_to', today)


def _warehouses(request):
    from apps.tenant.core.access import filter_by_branch
    from apps.tenant.inventory.models import Warehouse
    return filter_by_branch(Warehouse.objects.filter(is_active=True), request)


@login_required
@custom_permission_required('core.view_inventory_reports', redirect_url='core:main_screen')
def inventory_turnover(request):
    from apps.tenant.inventory.models import InventoryBatch, Product
    from apps.tenant.invoicing.models import Invoice, InvoiceLine
    date_from, date_to = _dates(request, 90)
    days = max((date_to - date_from).days + 1, 1)
    warehouses = _warehouses(request)
    sold = {r['product_id']: r for r in InvoiceLine.objects.filter(
        invoice__status=Invoice.POSTED, invoice__date__gte=date_from, invoice__date__lte=date_to,
        invoice__warehouse__in=warehouses, invoice__invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
    ).order_by().values('product_id').annotate(
        qty=Coalesce(Sum(Case(When(invoice__invoice_type=Invoice.SALE, then=F('quantity')),
                              default=-F('quantity'), output_field=QTY)), Value(ZERO), output_field=QTY),
        cogs=Coalesce(Sum(Case(When(invoice__invoice_type=Invoice.SALE, then=F('cogs_amount')),
                               default=-F('cogs_amount'), output_field=MONEY)), Value(ZERO), output_field=MONEY),
    )}
    stock = {r['product_id']: r for r in InventoryBatch.objects.filter(warehouse__in=warehouses, quantity_remaining__gt=0)
             .order_by().values('product_id').annotate(
                 qty=Sum('quantity_remaining'),
                 value=Sum(F('quantity_remaining') * F('unit_cost'), output_field=MONEY))}
    ids = set(sold) | set(stock)
    names = dict(Product.objects.filter(id__in=ids).values_list('id', 'name'))
    rows = []
    for pid in ids:
        s, st = sold.get(pid, {}), stock.get(pid, {})
        cogs, value = s.get('cogs') or ZERO, st.get('value') or ZERO
        qty_sold, qty_now = s.get('qty') or ZERO, st.get('qty') or ZERO
        turnover = (cogs / value) if value > 0 else None
        cover = (qty_now / (qty_sold / days)).quantize(Decimal('1')) if qty_sold > 0 else None
        if qty_sold <= 0:
            klass = ('dead', 'راكد')
        elif cover is not None and cover <= 15:
            klass = ('fast', 'سريع')
        elif cover is not None and cover <= 60:
            klass = ('mid', 'متوسط')
        else:
            klass = ('slow', 'بطيء')
        rows.append({'name': names.get(pid, ''), 'qty_sold': qty_sold, 'cogs': cogs, 'qty_now': qty_now,
                     'value': value, 'turnover': money(turnover) if turnover is not None else None,
                     'cover': cover, 'klass': klass[0], 'klass_label': klass[1]})
    order = {'dead': 0, 'slow': 1, 'mid': 2, 'fast': 3}
    sort = request.GET.get('sort', 'slow')
    if sort == 'fast':
        rows.sort(key=lambda r: (-order[r['klass']], -(r['cogs'])))
    else:
        rows.sort(key=lambda r: (order[r['klass']], -(r['value'])))
    totals = {'cogs': sum((r['cogs'] for r in rows), ZERO), 'value': sum((r['value'] for r in rows), ZERO)}
    totals['turnover'] = money(totals['cogs'] / totals['value']) if totals['value'] > 0 else None
    dead_value = sum((r['value'] for r in rows if r['klass'] == 'dead'), ZERO)
    return render(request, 'reports/inventory_turnover.html', {
        'title': 'دوران المخزون', 'rows': rows, 'totals': totals, 'dead_value': dead_value, 'days': days,
        'date_from': date_from, 'date_to': date_to, 'sort': sort,
    })


@login_required
@custom_permission_required('core.view_inventory_reports', redirect_url='core:main_screen')
def wastage_report(request):
    from apps.tenant.inventory.models import StockMovement
    date_from, date_to = _dates(request, 30)
    moves = StockMovement.objects.filter(
        warehouse__in=_warehouses(request), movement_type__in=['ADJUSTMENT_OUT', 'ADJUSTMENT_IN'],
        created_at__date__gte=date_from, created_at__date__lte=date_to)
    value = F('quantity') * F('unit_cost')
    by_product = list(moves.order_by().values('product__name').annotate(
        lost_qty=Coalesce(Sum('quantity', filter=Q(movement_type='ADJUSTMENT_OUT')), Value(ZERO), output_field=QTY),
        lost=Coalesce(Sum(value, filter=Q(movement_type='ADJUSTMENT_OUT'), output_field=MONEY), Value(ZERO), output_field=MONEY),
        gained_qty=Coalesce(Sum('quantity', filter=Q(movement_type='ADJUSTMENT_IN')), Value(ZERO), output_field=QTY),
        gained=Coalesce(Sum(value, filter=Q(movement_type='ADJUSTMENT_IN'), output_field=MONEY), Value(ZERO), output_field=MONEY),
    ).order_by('-lost'))
    totals = moves.aggregate(
        lost=Coalesce(Sum(value, filter=Q(movement_type='ADJUSTMENT_OUT'), output_field=MONEY), Value(ZERO), output_field=MONEY),
        gained=Coalesce(Sum(value, filter=Q(movement_type='ADJUSTMENT_IN'), output_field=MONEY), Value(ZERO), output_field=MONEY))
    details = list(moves.select_related('product', 'warehouse').annotate(value=value).order_by('-created_at')[:200])
    return render(request, 'reports/wastage.html', {
        'title': 'الهالك والعجز', 'by_product': by_product, 'totals': totals, 'details': details,
        'date_from': date_from, 'date_to': date_to, 'net': totals['gained'] - totals['lost'],
    })


@login_required
@custom_permission_required('core.view_inventory_reports', redirect_url='core:main_screen')
def batch_traceability(request):
    from apps.tenant.inventory.models import InventoryBatch, StockMovement
    q = (request.GET.get('q') or '').strip()
    batches = []
    if q:
        batches = list(InventoryBatch.objects.filter(warehouse__in=_warehouses(request)).filter(
            Q(batch_number__icontains=q) | Q(product__name__icontains=q) | Q(product__barcode=q))
            .select_related('product', 'warehouse', 'source_invoice_line__invoice__partner')
            .order_by('product__name', 'expiry_date', 'id')[:50])
        moves = {}
        for m in (StockMovement.objects.filter(batch__in=batches).select_related('warehouse')
                  .order_by('created_at', 'id')):
            moves.setdefault(m.batch_id, []).append(m)
        for b in batches:
            b.moves = moves.get(b.id, [])
    return render(request, 'reports/batch_traceability.html', {
        'title': 'تتبع التشغيلة', 'q': q, 'batches': batches, 'today': timezone.localdate(),
    })
