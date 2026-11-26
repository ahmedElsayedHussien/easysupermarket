"""
واجهة المندوب على الموبايل (منفصلة عن الواجهة الكاملة).

The rep sells from his van, collects from customers, and requests loading / unloading. Every
figure (prices, totals, stock) is computed on the server (van_services); the phone only sends
product ids and quantities. Access: permission invoicing.use_rep_app + an active SalesRep
linked to the user.
"""
import json
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from apps.tenant.inventory.models import Product

from . import van_services as van
from .models import Invoice, LoadingOrder, RepCashHandover, UnloadingOrder
from .rep_services import rep_for_user

ZERO = Decimal('0')


def rep_app_view(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.has_perm('invoicing.use_rep_app'):
            messages.error(request, 'عفواً، مش متاح لك واجهة المندوب.')
            return redirect('core:main_screen')
        rep = rep_for_user(request.user)
        if rep is None:
            return render(request, 'rep_app/no_rep.html', {'title': 'واجهة المندوب'})
        rep = type(rep).objects.select_related('employee__user', 'van_warehouse__branch', 'treasury',
                                               'source_warehouse').get(pk=rep.pk)
        request.rep = rep
        return view(request, *args, **kwargs)
    return wrapper


def _ctx(request, tab, **kw):
    return {'rep': request.rep, 'tab': tab, 'rep_name': van.rep_name(request.rep), **kw}


def _err(e):
    return ' '.join(e.messages) if isinstance(e, ValidationError) else str(e)


def _van_products(rep, product_ids=None):
    """Products in the van with quantity, units and the base sale price (display only)."""
    stock = van.van_stock(rep.van_warehouse, product_ids)
    products = Product.objects.filter(id__in=list(stock), is_active=True).prefetch_related('uoms__uom').order_by('name')
    out = []
    for p in products:
        uoms = [{'id': '', 'name': p.pos_unit_name, 'factor': '1'}] + [
            {'id': u.id, 'name': u.uom.name, 'factor': str(u.conversion_factor or 1)}
            for u in p.uoms.all() if not u.is_base]
        out.append({'id': p.id, 'name': p.name, 'barcode': p.barcode or '', 'qty': str(stock[p.id].normalize()),
                    'price': str(p.sale_price or ZERO), 'uoms': uoms})
    return out


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

@rep_app_view
def home(request):
    rep = request.rep
    today = timezone.localdate()
    day = Invoice.objects.filter(sales_rep=rep, status=Invoice.POSTED, date=today, invoice_type=Invoice.SALE).aggregate(
        n=Count('id'), total=Sum('total_amount'), credit=Sum('paid_credit'))
    stock = van.van_stock(rep.van_warehouse)
    return render(request, 'rep_app/home.html', _ctx(
        request, 'home', title='الرئيسية', day=day, cash=van.cash_balance(rep), items_in_van=len(stock),
        pending_loading=LoadingOrder.objects.filter(rep=rep, status__in=[LoadingOrder.PENDING, LoadingOrder.APPROVED]).count(),
        pending_unloading=UnloadingOrder.objects.filter(rep=rep, status=UnloadingOrder.PENDING).count(),
        recent=Invoice.objects.filter(sales_rep=rep, status=Invoice.POSTED).select_related('partner')
        .order_by('-id')[:6],
        handovers=RepCashHandover.objects.filter(rep=rep).order_by('-id')[:3],
        ready=bool(rep.van_warehouse_id and rep.treasury_id)))


# ---------------------------------------------------------------------------
# Selling
# ---------------------------------------------------------------------------

def _partner(request, pid):
    from apps.tenant.partners.models import Partner
    if not str(pid or '').isdigit():
        return None
    return Partner.objects.filter(pk=int(pid), is_active=True, partner_type__in=['CUSTOMER', 'BOTH']).first()


@rep_app_view
def sell(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body or b'{}')
            rows = [{'product_id': r.get('product_id'), 'uom_id': r.get('uom_id'), 'qty': r.get('qty')}
                    for r in (data.get('cart') or []) if isinstance(r, dict)]
            partner = _partner(request, data.get('partner_id'))
            if data.get('partner_id') and partner is None:
                raise ValidationError('العميل مش موجود.')
            if data.get('quote'):
                return JsonResponse({'success': True, **_quote(request.rep, request.user, rows, partner,
                                                               data.get('discount_pct') or 0)})
            inv = van.rep_sale(request.rep, request.user, rows, partner=partner, payment=data.get('payment') or 'CASH',
                               paid_amount=data.get('paid_amount'), discount_pct=data.get('discount_pct') or 0,
                               notes=str(data.get('notes') or '')[:300])
            return JsonResponse({'success': True, 'invoice_id': inv.id, 'number': inv.invoice_number,
                                 'url': reverse('invoicing:rep_app_invoice', args=[inv.id])})
        except ValidationError as e:
            return JsonResponse({'success': False, 'error': _err(e)}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'error': 'بيانات غير صالحة.'}, status=400)
    partner = _partner(request, request.GET.get('customer'))
    return render(request, 'rep_app/sell.html', _ctx(
        request, 'sell', title='بيع', products_json=_van_products(request.rep),
        partner=partner))


def _quote(rep, user, rows, partner, discount_pct):
    """Server prices and totals for the cart (nothing is saved)."""
    from .views import _pos_unit_price
    discount = van._dec(discount_pct, 'الخصم')
    items = van._product_rows(rows, allow_service=True) if rows else {}
    branch = rep.van_warehouse.branch if rep.van_warehouse_id else None
    lines, subtotal, tax = [], ZERO, ZERO
    for i in items.values():
        price, _ = _pos_unit_price(i['product'], i['uom'], partner or van._cash_customer(), branch, i['qty'])
        net = (price * i['qty'] * (1 - discount / 100)).quantize(van.CENT)
        t = (net * (i['product'].tax_rate or ZERO) / 100).quantize(van.CENT)
        subtotal += net
        tax += t
        lines.append({'product_id': i['product'].id, 'uom_id': i['uom'].id if i['uom'] else '',
                      'price': str(price.quantize(van.CENT)), 'total': str(net + t)})
    return {'lines': lines, 'subtotal': str(subtotal), 'tax': str(tax), 'total': str(subtotal + tax)}


@rep_app_view
def invoice(request, pk):
    inv = get_object_or_404(Invoice.objects.select_related('partner').prefetch_related('lines__product', 'lines__uom__uom'),
                            pk=pk, sales_rep=request.rep)
    lines = list(inv.lines.all())
    share = f'فاتورة {inv.invoice_number}\n' + '\n'.join(
        f'{l.product.name} × {l.quantity.normalize():f} {l.display_unit_name} = {l.total_amount:.2f}' for l in lines
    ) + f'\nالإجمالي: {inv.total_amount:.2f}'
    if inv.paid_credit:
        share += f'\nآجل: {inv.paid_credit:.2f}'
    return render(request, 'rep_app/invoice.html', _ctx(request, 'sell', title=inv.invoice_number, inv=inv,
                                                         lines=lines, share=share))


# ---------------------------------------------------------------------------
# Customers & collection
# ---------------------------------------------------------------------------

@rep_app_view
def customers(request):
    from apps.tenant.partners.models import Partner
    from apps.tenant.partners.services import batch_get_partners_balances
    if request.method == 'POST':
        if not request.user.has_perm('partners.add_partner'):
            messages.error(request, 'عفواً، مش مسموح لك تضيف عملاء.')
            return redirect('invoicing:rep_app_customers')
        name = (request.POST.get('name') or '').strip()
        if not name:
            messages.error(request, 'اكتب اسم العميل.')
            return redirect('invoicing:rep_app_customers')
        if Partner.objects.filter(name=name).exists():
            messages.error(request, 'فيه عميل بنفس الاسم.')
            return redirect('invoicing:rep_app_customers')
        p = Partner.objects.create(name=name[:300], partner_type='CUSTOMER', is_active=True,
                                   mobile=(request.POST.get('mobile') or '').strip()[:30],
                                   address=(request.POST.get('address') or '').strip())
        messages.success(request, f'اتضاف العميل {p.name}.')
        return redirect('invoicing:rep_app_customer', pk=p.pk)
    q = (request.GET.get('q') or '').strip()
    qs = Partner.objects.filter(is_active=True, partner_type__in=['CUSTOMER', 'BOTH']).exclude(name='عميل نقدي')
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(mobile__icontains=q) | Q(phone__icontains=q))
    else:
        # my customers first: the ones I sold to
        mine = Invoice.objects.filter(sales_rep=request.rep, status=Invoice.POSTED).values('partner')
        qs = qs.filter(pk__in=mine)
    qs = qs.order_by('name')[:40]
    qs = list(qs)
    balances = batch_get_partners_balances(partner_qs=Partner.objects.filter(pk__in=[p.pk for p in qs]))
    rows = [{'p': p, 'balance': (balances.get(p.id) or {}).get('final_balance', ZERO)} for p in qs]
    if request.GET.get('json'):
        return JsonResponse({'items': [{'id': r['p'].id, 'name': r['p'].name, 'mobile': r['p'].mobile or r['p'].phone,
                                        'balance': str(r['balance'])} for r in rows]})
    return render(request, 'rep_app/customers.html', _ctx(
        request, 'customers', title='العملاء', rows=rows, q=q,
        can_add=request.user.has_perm('partners.add_partner')))


@rep_app_view
def customer(request, pk):
    p = _partner(request, pk)
    if p is None:
        return redirect('invoicing:rep_app_customers')
    if request.method == 'POST':
        try:
            v = van.rep_collect(request.rep, request.user, p, request.POST.get('amount'), request.POST.get('notes', ''))
            messages.success(request, f'اتسجل تحصيل {v.amount:.2f} من {p.name} (سند {v.voucher_number}).')
        except ValidationError as e:
            messages.error(request, _err(e))
        return redirect(request.path)
    from apps.tenant.partners.services import batch_get_partners_balances
    from apps.tenant.partners.models import Partner
    bal = (batch_get_partners_balances(partner_qs=Partner.objects.filter(pk=p.pk)).get(p.pk) or {}).get(
        'final_balance', ZERO)
    from apps.tenant.accounting.models import Voucher
    return render(request, 'rep_app/customer.html', _ctx(
        request, 'customers', title=p.name, p=p, balance=bal,
        invoices=Invoice.objects.filter(partner=p, status=Invoice.POSTED, sales_rep=request.rep).order_by('-id')[:10],
        receipts=Voucher.objects.filter(partner=p, voucher_type=Voucher.RECEIPT, status=Voucher.POSTED,
                                        treasury=request.rep.treasury).order_by('-id')[:10] if request.rep.treasury_id else []))


# ---------------------------------------------------------------------------
# Van stock, loading & unloading
# ---------------------------------------------------------------------------

@rep_app_view
def stock(request):
    return render(request, 'rep_app/stock.html', _ctx(request, 'van', title='العربية',
                                                       products=_van_products(request.rep)))


@rep_app_view
def loading(request):
    rep = request.rep
    if request.method == 'POST':
        rows = [{'product_id': pid, 'uom_id': u, 'qty': q} for pid, u, q in zip(
            request.POST.getlist('product_id'), request.POST.getlist('uom_id'), request.POST.getlist('qty'))]
        try:
            o = van.create_loading_request(rep, rows, request.user, request.POST.get('notes', ''))
            messages.success(request, f'اتبعت طلب التحميل {o.number} — مستني الاعتماد.')
        except ValidationError as e:
            messages.error(request, _err(e))
        return redirect('invoicing:rep_app_loading')
    source = van.default_source_warehouse(rep)
    available = van.van_stock(source) if source else {}
    products = Product.objects.filter(id__in=list(available), is_active=True).exclude(
        product_type__in=['SERVICE', 'SERIALIZED']).prefetch_related('uoms__uom').order_by('name')
    catalog = [{'id': p.id, 'name': p.name, 'barcode': p.barcode or '', 'qty': str(available[p.id].normalize()),
                'uoms': [{'id': '', 'name': p.pos_unit_name, 'factor': '1'}] + [
                    {'id': u.id, 'name': u.uom.name, 'factor': str(u.conversion_factor or 1)}
                    for u in p.uoms.all() if not u.is_base]} for p in products]
    orders = (LoadingOrder.objects.filter(rep=rep).annotate(n=Count('lines')).order_by('-id')[:15])
    return render(request, 'rep_app/loading.html', _ctx(
        request, 'orders', title='طلب تحميل', source=source, catalog_json=catalog,
        orders=orders))


@rep_app_view
def loading_order(request, pk):
    o = get_object_or_404(LoadingOrder.objects.select_related('from_warehouse'), pk=pk, rep=request.rep)
    return render(request, 'rep_app/loading_order.html', _ctx(
        request, 'orders', title=o.number, o=o, lines=o.lines.select_related('product', 'uom__uom')))


@rep_app_view
def unloading(request):
    rep = request.rep
    if request.method == 'POST':
        rows = [{'product_id': pid, 'sound': s, 'damaged': d} for pid, s, d in zip(
            request.POST.getlist('product_id'), request.POST.getlist('sound'), request.POST.getlist('damaged'))]
        try:
            o = van.create_unloading_request(rep, rows, request.user, request.POST.get('notes', ''))
            messages.success(request, f'اتبعت طلب التفريغ {o.number} — سلّم البضاعة لأمين المخزن.')
        except ValidationError as e:
            messages.error(request, _err(e))
        return redirect('invoicing:rep_app_unloading')
    orders = UnloadingOrder.objects.filter(rep=rep).annotate(n=Count('lines')).order_by('-id')[:15]
    return render(request, 'rep_app/unloading.html', _ctx(
        request, 'orders', title='طلب تفريغ', products=_van_products(rep), orders=orders,
        target=van.default_source_warehouse(rep)))
