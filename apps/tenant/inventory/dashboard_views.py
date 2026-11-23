"""
Inventory dashboard (لوحة المخزون).

One page that replaces the crowded inventory menu: stock value and health
(low / out / negative / expiring / stagnant), value by category and warehouse,
30-day stock in vs out, top items by value, alerts, recent adjustments, and
every inventory action (FIFO tracking, stocktake, adjustments, transfers,
products, Excel import, opening balances, warehouses, categories, units).

Branch rule (core.access): warehouses follow the header branch for admins
("all branches" = every warehouse) and the own branch for users.
"""
import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.shortcuts import redirect, render

from apps.tenant.core.access import filter_by_branch, get_branch_scope
from apps.tenant.core.settings_helpers import setting

from .models import Category, InventoryBatch, Product, SerialItem, StockAdjustment, StockMovement, Warehouse

ZERO = Decimal('0')
FLOW_DAYS = 30
TOP_CATEGORIES = 8
INVENTORY_VIEW_PERMS = ('inventory.view_product', 'inventory.view_inventorybatch', 'inventory.view_stockadjustment',
                        'inventory.view_warehouse')
IN_TYPES = ('IN', 'ADJUSTMENT_IN', 'OPENING_BALANCE', 'RETURN_IN')
OUT_TYPES = ('OUT', 'ADJUSTMENT_OUT', 'RETURN_OUT')   # transfers are internal moves: excluded

MONEY = DecimalField(max_digits=20, decimal_places=4)


def _can_view(user):
    from apps.tenant.core.decorators import is_admin_user
    return is_admin_user(user) or any(user.has_perm(p) for p in INVENTORY_VIEW_PERMS)


def _value_expr(qty_field='quantity_remaining', cost_field='unit_cost'):
    return ExpressionWrapper(F(qty_field) * Coalesce(F(cost_field), Value(ZERO)), output_field=MONEY)


@login_required
def inventory_dashboard(request):
    if not _can_view(request.user):
        from django.contrib import messages
        messages.error(request, 'عفواً، لا تملك صلاحية عرض لوحة المخزون.')
        return redirect('core:main_screen')

    today = datetime.date.today()
    stagnant_days = int(setting('stagnant_stock_days', 60) or 60)
    expiry_days = int(setting('expiry_warning_days', 30) or 30)
    expiry_tracking = bool(setting('enable_expiry_tracking', False))
    try:
        default_min = Decimal(str(setting('low_stock_threshold', 0) or 0))
    except Exception:
        default_min = ZERO
    warehouses = filter_by_branch(Warehouse.objects.filter(is_active=True).select_related('branch'), request)
    batches = InventoryBatch.objects.filter(warehouse__in=warehouses)
    on_hand = batches.filter(quantity_remaining__gt=0)

    # --- stock per product (tangible from batches, serialized from unsold serials) ---
    qty_by_product = dict(batches.values('product_id').annotate(q=Sum('quantity_remaining')).values_list('product_id', 'q'))
    serial_counts = dict(SerialItem.objects.filter(is_sold=False, warehouse__in=warehouses)
                         .values('product_id').annotate(n=Count('id')).values_list('product_id', 'n'))
    products = list(Product.objects.filter(is_active=True).exclude(product_type='SERVICE')
                    .select_related('category')
                    .only('id', 'name', 'product_type', 'min_stock_level', 'category__name', 'last_purchase_price'))

    def stock_of(p):
        if p.product_type == 'SERIALIZED':
            return Decimal(serial_counts.get(p.id, 0))
        return qty_by_product.get(p.id) or ZERO

    low_stock, out_of_stock, negative = [], [], []
    in_stock_ids = set()
    for p in products:
        q = stock_of(p)
        if q > 0:
            in_stock_ids.add(p.id)
        if q < 0:
            negative.append((p, q))
        elif q == 0:
            out_of_stock.append((p, q))
        # product's own reorder level, else the global SystemSetting.low_stock_threshold
        level = p.min_stock_level if p.min_stock_level and p.min_stock_level > 0 else default_min
        if level > 0 and q <= level:
            low_stock.append((p, q, level))
    low_stock.sort(key=lambda t: (t[1] - t[2]))

    # --- value ---
    stock_value = on_hand.aggregate(v=Coalesce(Sum(_value_expr()), Value(ZERO), output_field=MONEY))['v']
    serial_value = (SerialItem.objects.filter(is_sold=False, warehouse__in=warehouses)
                    .aggregate(v=Coalesce(Sum('actual_cost'), Value(ZERO), output_field=MONEY))['v']
                    if hasattr(SerialItem, 'actual_cost') else ZERO)
    total_value = stock_value + (serial_value or ZERO)

    # value by category (top N + other)
    cat_rows = list(on_hand.values('product__category__name')
                    .annotate(v=Sum(_value_expr())).order_by('-v'))
    categories = [{'label': r['product__category__name'] or 'بدون فئة', 'value': r['v'] or ZERO} for r in cat_rows]
    if len(categories) > TOP_CATEGORIES:
        other = sum((c['value'] for c in categories[TOP_CATEGORIES - 1:]), ZERO)
        categories = categories[:TOP_CATEGORIES - 1] + [{'label': 'أخرى', 'value': other, 'other': True}]
    max_cat = max([c['value'] for c in categories] + [ZERO])
    for c in categories:
        c['pct_of_max'] = float(c['value'] / max_cat * 100) if max_cat > 0 else 0
        c['share'] = float(c['value'] / stock_value * 100) if stock_value > 0 else 0

    # value by warehouse
    wh_value = dict(on_hand.values('warehouse_id').annotate(v=Sum(_value_expr())).values_list('warehouse_id', 'v'))
    wh_items = dict(on_hand.values('warehouse_id').annotate(n=Count('product_id', distinct=True)).values_list('warehouse_id', 'n'))
    wh_rows = [{'obj': w, 'value': wh_value.get(w.id) or ZERO, 'items': wh_items.get(w.id, 0)} for w in warehouses]
    wh_rows.sort(key=lambda r: r['value'], reverse=True)
    max_wh = max([r['value'] for r in wh_rows] + [ZERO])
    for r in wh_rows:
        r['pct'] = float(r['value'] / max_wh * 100) if max_wh > 0 else 0

    # top items by value
    top_items = list(on_hand.values('product_id', 'product__name')
                     .annotate(q=Sum('quantity_remaining'), v=Sum(_value_expr())).order_by('-v')[:10])

    # --- expiry ---
    if expiry_tracking:
        soon = today + datetime.timedelta(days=expiry_days)
        expiring = on_hand.filter(expiry_date__isnull=False, expiry_date__lte=soon)
        expired_stats = expiring.filter(expiry_date__lt=today).aggregate(n=Count('id'), v=Sum(_value_expr()))
        soon_stats = expiring.filter(expiry_date__gte=today).aggregate(n=Count('id'), v=Sum(_value_expr()))
        expiring_list = list(expiring.select_related('product', 'warehouse').order_by('expiry_date')[:8])
    else:
        expired_stats = soon_stats = {'n': 0, 'v': ZERO}
        expiring_list = []

    # --- stagnant stock: on hand but no sale/out in stagnant_days ---
    moved_since = today - datetime.timedelta(days=stagnant_days)
    recently_out = set(StockMovement.objects.filter(
        warehouse__in=warehouses, movement_type__in=OUT_TYPES, created_at__date__gte=moved_since,
    ).values_list('product_id', flat=True).distinct())
    stagnant_ids = in_stock_ids - recently_out
    stagnant_value = (on_hand.filter(product_id__in=stagnant_ids)
                      .aggregate(v=Coalesce(Sum(_value_expr()), Value(ZERO), output_field=MONEY))['v'])

    # --- 30-day stock in / out (value at cost) ---
    start = today - datetime.timedelta(days=FLOW_DAYS - 1)
    moves = (StockMovement.objects.filter(warehouse__in=warehouses, created_at__date__gte=start)
             .annotate(day=TruncDate('created_at')))
    flow = {}
    for row in (moves.filter(movement_type__in=IN_TYPES + OUT_TYPES)
                .values('day', 'movement_type').annotate(v=Sum(_value_expr('quantity', 'unit_cost')))):
        d = flow.setdefault(row['day'], {'in': 0.0, 'out': 0.0})
        d['in' if row['movement_type'] in IN_TYPES else 'out'] += float(row['v'] or 0)
    labels, flow_in, flow_out = [], [], []
    for i in range(FLOW_DAYS):
        day = start + datetime.timedelta(days=i)
        labels.append(day.strftime('%m-%d'))
        flow_in.append(round(flow.get(day, {}).get('in', 0.0), 2))
        flow_out.append(round(flow.get(day, {}).get('out', 0.0), 2))
    total_in, total_out = sum(flow_in), sum(flow_out)

    # sales COGS in 30 days -> rough turnover / days of cover
    cogs_30 = float(sum(flow_out))
    days_cover = round(float(total_value) / (cogs_30 / FLOW_DAYS), 0) if cogs_30 > 0 else None

    # --- adjustments ---
    adjustments = filter_by_branch(StockAdjustment.objects.all(), request, field='warehouse__branch')
    recent_adjustments = list(adjustments.select_related('warehouse', 'created_by').order_by('-date', '-id')[:6])
    draft_adjustments = adjustments.filter(status='DRAFT').count()

    scope_branch = get_branch_scope(request)
    context = {
        'title': 'لوحة المخزون',
        'today': today,
        'scope_label': scope_branch.name if scope_branch else 'كل الفروع',
        'total_value': total_value,
        'products_count': len(products),
        'in_stock_count': len(in_stock_ids),
        'warehouses_count': len(wh_rows),
        'low_stock': low_stock[:10],
        'low_stock_count': len(low_stock),
        'out_of_stock_count': len(out_of_stock),
        'negative': negative[:10],
        'negative_count': len(negative),
        'expired_count': expired_stats['n'] or 0,
        'expired_value': expired_stats['v'] or ZERO,
        'expiring_soon_count': soon_stats['n'] or 0,
        'expiring_soon_value': soon_stats['v'] or ZERO,
        'expiring_list': expiring_list,
        'expiry_days': expiry_days,
        'expiry_tracking': expiry_tracking,
        'stagnant_count': len(stagnant_ids),
        'stagnant_value': stagnant_value or ZERO,
        'stagnant_days': stagnant_days,
        'categories': categories,
        'wh_rows': wh_rows,
        'top_items': top_items,
        'flow_days': FLOW_DAYS,
        'flow_data': {'labels': labels, 'inflow': flow_in, 'outflow': flow_out},
        'total_in': total_in,
        'total_out': total_out,
        'days_cover': days_cover,
        'recent_adjustments': recent_adjustments,
        'draft_adjustments': draft_adjustments,
        'categories_count': Category.objects.filter(is_active=True).count(),
    }
    return render(request, 'inventory/dashboard.html', context)
