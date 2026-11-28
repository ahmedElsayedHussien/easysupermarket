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
from .models import Invoice, InvoiceLine, LoadingOrder, RepCashHandover, UnloadingOrder
from .rep_services import rep_for_user
from apps.tenant.core.money import money

ZERO = Decimal('0')


def rep_app_view(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        emp = getattr(request.user, 'employee_profile', None)
        is_rep_role = emp is not None and emp.role == 'SALES_REP'     # the role alone opens his screen
        if not is_rep_role and not request.user.has_perm('invoicing.use_rep_app'):
            messages.error(request, 'عفواً، مش متاح لك واجهة المندوب.')
            return redirect('core:main_screen')
        rep = rep_for_user(request.user)
        if rep is None:
            return render(request, 'rep_app/no_rep.html', {'title': 'واجهة المندوب'})
        rep = type(rep).objects.select_related('employee__user', 'employee__branch', 'van_warehouse__branch', 'treasury',
                                               'source_warehouse').get(pk=rep.pk)
        request.rep = rep
        # switch to the rep's branch automatically (his van's branch) — header + every screen
        from apps.tenant.core.access import is_tenant_admin, set_header_branch
        request.rep_branch = van.sync_rep_branch(rep)
        if request.rep_branch is not None and is_tenant_admin(request.user):
            set_header_branch(request, request.rep_branch)
        return view(request, *args, **kwargs)
    return wrapper


def _ctx(request, tab, **kw):
    return {'rep': request.rep, 'tab': tab, 'rep_name': van.rep_name(request.rep),
            'rep_branch': getattr(request, 'rep_branch', None), **kw}


def _err(e):
    return ' '.join(e.messages) if isinstance(e, ValidationError) else str(e)


def _product_list(ids, qty):
    """[{id, name, barcode, category, qty, price, uoms}] for products ``ids`` with quantities ``qty`` (base unit)."""
    products = Product.objects.filter(id__in=list(ids), is_active=True).select_related('category').prefetch_related(
        'uoms__uom').order_by('category__name', 'name')
    out = []
    for p in products:
        uoms = [{'id': '', 'name': p.pos_unit_name, 'factor': '1'}] + [
            {'id': u.id, 'name': u.uom.name, 'factor': f'{Decimal(u.conversion_factor or 1).normalize():f}'}
            for u in p.uoms.all() if not u.is_base]
        out.append({'id': p.id, 'name': p.name, 'barcode': p.barcode or '', 'category': p.category.name if p.category_id else '',
                    'qty': f'{Decimal(qty.get(p.id) or 0).normalize():f}', 'price': str(p.sale_price or ZERO), 'uoms': uoms})
    return out


def _van_products(rep, product_ids=None):
    """Products in the van with quantity, units and the base sale price (display only)."""
    stock = van.van_stock(rep.van_warehouse, product_ids)
    return _product_list(list(stock), stock)


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
        recent=_decorate(list(Invoice.objects.filter(sales_rep=rep, status__in=[Invoice.POSTED, Invoice.CANCELLED])
                              .select_related('partner')
                              .order_by('-id')[:6])),
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


def _customer_info(p):
    """Card data for the selected customer (balance, credit limit, what is left of it)."""
    from apps.tenant.partners.models import Partner
    from apps.tenant.partners.services import batch_get_partners_balances
    if p is None:
        return None
    bal = (batch_get_partners_balances(partner_qs=Partner.objects.filter(pk=p.pk)).get(p.pk) or {}).get(
        'final_balance', ZERO)
    limit = p.credit_limit or ZERO
    return {'id': p.id, 'name': p.name, 'mobile': p.mobile or p.phone or '', 'address': p.address or '',
            'tax_id': (p.tax_id or '').strip(), 'wht': bool(p.subject_to_withholding_tax),
            'vat_rate': f'{p.vat_rate.normalize():f}' if p.subject_to_vat else '',
            'wht_rate': f'{p.wht_rate.normalize():f}' if p.subject_to_withholding_tax else '',
            'balance': str(bal), 'credit_limit': str(limit),
            'credit_left': str(max(limit - bal, ZERO)) if limit > 0 else ''}


def _cart_rows(data):
    return [{'product_id': r.get('product_id'), 'uom_id': r.get('uom_id'), 'qty': r.get('qty'),
             'disc': r.get('disc') or 0, 'disc_type': r.get('disc_type') or 'pct', 'price': r.get('price')}
            for r in (data.get('cart') or []) if isinstance(r, dict)]


@rep_app_view
def sell(request):
    """One screen for selling and for taking goods back (mode = sale | return)."""
    rep = request.rep
    if request.method == 'GET' and request.GET.get('customer_info'):
        return JsonResponse({'customer': _customer_info(_partner(request, request.GET.get('customer_info')))})
    if request.method == 'GET' and request.GET.get('returnable'):
        partner = _partner(request, request.GET.get('returnable'))
        can = van.returnable_for(partner, rep=rep) if partner else {}
        return JsonResponse({'items': _product_list(list(can), can)})
    if request.method == 'GET' and request.GET.get('catalog'):
        if not _no_invoice_allowed():
            return JsonResponse({'items': []})
        ids = Product.objects.filter(is_active=True).exclude(product_type__in=['SERVICE', 'SERIALIZED']) \
            .values_list('id', flat=True)
        return JsonResponse({'items': _product_list(list(ids), {})})
    if request.method == 'POST':
        try:
            data = json.loads(request.body or b'{}')
            rows = _cart_rows(data)
            mode = 'return' if data.get('mode') == 'return' else 'sale'
            partner = _partner(request, data.get('partner_id'))
            if data.get('partner_id') and partner is None:
                raise ValidationError('العميل مش موجود.')
            disc = data.get('discount_pct') or 0
            dtype = 'amt' if data.get('discount_type') == 'amt' else 'pct'
            if data.get('quote'):
                cart = van.price_cart(rep, request.user, rows, partner, disc, dtype, returning=mode == 'return')
                return JsonResponse({'success': True, **_quote_json(cart)})
            notes = str(data.get('notes') or '')[:300]
            idem_key = data.get('idempotency_key') or request.headers.get('X-Idempotency-Key')
            if mode == 'return':
                inv = van.rep_return(rep, request.user, rows, partner, refund=data.get('refund') or 'CREDIT',
                                     discount_pct=disc, notes=notes, discount_type=dtype,
                                     without_invoice=bool(data.get('no_invoice')),
                                     idempotency_key=idem_key)
            else:
                from django.utils.dateparse import parse_date
                inv = van.rep_sale(rep, request.user, rows, partner=partner, payment=data.get('payment') or 'CASH',
                                   paid_amount=data.get('paid_amount'), discount_pct=disc, notes=notes,
                                   discount_type=dtype,
                                   due_date=parse_date(str(data.get('due_date') or '')) or None,
                                   idempotency_key=idem_key)
            return JsonResponse({'success': True, 'invoice_id': inv.id, 'number': inv.invoice_number,
                                 'url': reverse('invoicing:rep_app_invoice', args=[inv.id])})
        except ValidationError as e:
            return JsonResponse({'success': False, 'error': _err(e)}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'error': 'بيانات غير صالحة.'}, status=400)
    partner = _partner(request, request.GET.get('customer'))
    return render(request, 'rep_app/sell.html', _ctx(
        request, 'sell', title='فاتورة', products_json=_van_products(rep),
        customer_json=_customer_info(partner), start_mode='return' if request.GET.get('mode') == 'return' else 'sale',
        cash=van.cash_balance(rep), can_edit_price=_rep_price_edit(), no_invoice_ok=_no_invoice_allowed()))


def _no_invoice_allowed():
    from apps.tenant.core.models import SystemSetting
    return bool(getattr(SystemSetting.get_settings(), 'rep_allow_return_without_invoice', False))


def _rep_price_edit():
    from apps.tenant.core.models import SystemSetting
    return bool(getattr(SystemSetting.get_settings(), 'rep_allow_price_edit', False))


def _quote_json(cart):
    s = lambda d: str(money(d))  # noqa: E731
    pct = lambda d: f'{money(d).normalize():f}'  # noqa: E731
    return {'lines': [{'product_id': l['product'].id, 'uom_id': l['uom'].id if l['uom'] else '',
                       'price': s(l['price']), 'list_price': s(l['list_price']), 'disc_pct': pct(l['disc_pct']), 'disc_amount': s(l['disc_amount']),
                       'tax_rate': pct(l['tax_rate']), 'tax': s(l['tax']), 'wht_rate': pct(l['wht_rate']),
                       'wht': s(l['wht']), 'total': s(l['total'])} for l in cart['lines']],
            'gross': s(cart['gross']), 'discount': s(cart['discount']), 'subtotal': s(cart['subtotal']),
            'tax': s(cart['tax']), 'wht': s(cart['wht']), 'total': s(cart['total']),
            'taxes': [{'rate': pct(r), 'amount': s(a)} for r, a in cart['taxes']]}


@rep_app_view
def invoice(request, pk):
    inv = get_object_or_404(Invoice.objects.select_related('partner').prefetch_related('lines__product', 'lines__uom__uom'),
                            pk=pk, sales_rep=request.rep)
    if request.method == 'POST' and request.POST.get('action') == 'cancel':
        try:
            van.rep_cancel(request.rep, request.user, inv, request.POST.get('reason'))
            messages.success(request, f'اتلغت {inv.invoice_number} — المخزون والفلوس رجعوا زي ما كانوا.')
        except ValidationError as e:
            messages.error(request, _err(e))
        return redirect('invoicing:rep_app_invoice', pk=inv.pk)
    lines = list(inv.lines.all())
    _decorate([inv])
    share = f'{"مرتجع" if inv.invoice_type == Invoice.RETURN_SALE else "فاتورة"} {inv.invoice_number}\n' + '\n'.join(
        f'{l.product.name} × {l.quantity.normalize():f} {l.display_unit_name} = {l.total_amount:.2f}' for l in lines
    ) + f'\nالإجمالي: {inv.total_amount:.2f}'
    if inv.paid_credit:
        share += f'\nآجل: {inv.paid_credit:.2f}'
    from apps.tenant.einvoicing.models import EInvoiceLog
    return render(request, 'rep_app/invoice.html', _ctx(request, 'sell', title=inv.invoice_number, inv=inv,
                                                         lines=lines, share=share,
                                                         einvoice=EInvoiceLog.objects.filter(invoice=inv).first(),
                                                         cancel_block=van.cancel_block_reason(request.rep, inv),
                                                         cancel_enabled=_rep_cancel_enabled()))


def _rep_cancel_enabled():
    from apps.tenant.core.models import SystemSetting
    return bool(getattr(SystemSetting.get_settings(), 'rep_allow_cancel_invoice', False))


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
    # every customer; the ones this rep already sold to come first
    from django.db.models import Exists, OuterRef
    qs = qs.annotate(mine=Exists(Invoice.objects.filter(sales_rep=request.rep, status=Invoice.POSTED,
                                                        partner=OuterRef('pk'))))
    total = qs.count()
    qs = list(qs.order_by('-mine', 'name')[:60])
    balances = batch_get_partners_balances(partner_qs=Partner.objects.filter(pk__in=[p.pk for p in qs]))
    rows = [{'p': p, 'balance': (balances.get(p.id) or {}).get('final_balance', ZERO)} for p in qs]
    if request.GET.get('json'):
        return JsonResponse({'total': total, 'items': [
            {'id': r['p'].id, 'name': r['p'].name, 'mobile': r['p'].mobile or r['p'].phone, 'mine': r['p'].mine,
             'balance': str(r['balance'])} for r in rows]})
    return render(request, 'rep_app/customers.html', _ctx(
        request, 'customers', title='العملاء', rows=rows, q=q, total=total,
        can_add=request.user.has_perm('partners.add_partner')))


@rep_app_view
def customer(request, pk):
    p = _partner(request, pk)
    if p is None:
        return redirect('invoicing:rep_app_customers')
    if request.method == 'POST':
        try:
            idem_key = request.POST.get('idempotency_key') or request.headers.get('X-Idempotency-Key')
            v = van.rep_collect(
                request.rep, request.user, p, request.POST.get('amount'),
                request.POST.get('notes', ''), idempotency_key=idem_key,
            )
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
    available = van.van_stock(source, sellable=True) if source else {}
    in_van = van.van_stock(rep.van_warehouse, list(available)) if rep.van_warehouse_id else {}
    products = Product.objects.filter(id__in=list(available), is_active=True).exclude(
        product_type__in=['SERVICE', 'SERIALIZED']).select_related('category').prefetch_related(
        'uoms__uom').order_by('category__name', 'name')
    catalog = [{'id': p.id, 'name': p.name, 'barcode': p.barcode or '', 'unit': p.pos_unit_name,
                'category': p.category.name if p.category_id else '',
                'available': f'{available[p.id].normalize():f}', 'in_van': f'{(in_van.get(p.id) or ZERO).normalize():f}',
                'uoms': [{'id': '', 'name': p.pos_unit_name, 'factor': '1'}] + [
                    {'id': u.id, 'name': u.uom.name, 'factor': f'{Decimal(u.conversion_factor or 1).normalize():f}'}
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
def unloading_order(request, pk):
    o = get_object_or_404(UnloadingOrder.objects.select_related('to_warehouse', 'damaged_warehouse', 'from_warehouse',
                                                                'received_by', 'transfer', 'damaged_transfer'),
                          pk=pk, rep=request.rep)
    lines = list(o.lines.select_related('product'))
    for l in lines:
        l.asked_sound = l.requested_sound if l.requested_sound is not None else l.sound_qty
        l.asked_damaged = l.requested_damaged if l.requested_damaged is not None else l.damaged_qty
        l.changed = o.status == UnloadingOrder.RECEIVED and (l.asked_sound != l.sound_qty or l.asked_damaged != l.damaged_qty)
    return render(request, 'rep_app/unloading_order.html', _ctx(request, 'orders', title=o.number, o=o, lines=lines))


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


# ---------------------------------------------------------------------------
# Invoice labels & the rep's day
# ---------------------------------------------------------------------------

def invoice_badges(inv):
    """(kind, kind_css, payment) labels for a rep invoice."""
    if inv.status == Invoice.CANCELLED:
        return ('مرتجع ملغي' if inv.invoice_type == Invoice.RETURN_SALE else 'فاتورة ملغاة'), 'void', 'اتلغت'
    if inv.invoice_type == Invoice.RETURN_SALE:
        return 'مرتجع', 'ret', 'رد نقدي' if inv.payment_type == 'CASH' else 'خصم من الحساب'
    pay = {'CASH': 'نقدي', 'CREDIT': 'آجل', 'SPLIT': 'جزء نقدي'}.get(inv.payment_type, inv.get_payment_type_display())
    return 'بيع', 'sale', pay


def _decorate(invoices):
    for inv in invoices:
        inv.kind, inv.kind_css, inv.pay = invoice_badges(inv)
    return invoices


def _box_balance_until(rep, day, inclusive=True):
    """The rep's cash box balance at the end of ``day`` (or at its start)."""
    from apps.tenant.accounting.models import JournalEntry, JournalItem
    if not rep.treasury_id or not rep.treasury.account_id:
        return ZERO
    flt = {'entry__date__lte': day} if inclusive else {'entry__date__lt': day}
    s = JournalItem.objects.filter(account_id=rep.treasury.account_id, entry__status=JournalEntry.POSTED, **flt) \
        .aggregate(d=Sum('debit'), c=Sum('credit'))
    return money((s['d'] or ZERO) - (s['c'] or ZERO))


@rep_app_view
def day(request):
    """يوميتي: what the rep sold, took back, collected and handed over on a day, and his cash box."""
    import datetime
    from django.db.models import DecimalField, F
    from django.db.models.functions import Coalesce
    from django.utils.dateparse import parse_date
    from apps.tenant.accounting.models import Voucher
    rep = request.rep
    today = timezone.localdate()
    d = parse_date(request.GET.get('date') or '') or today
    if d > today:
        d = today
    money_field = DecimalField(max_digits=18, decimal_places=2)
    invoices = list(Invoice.objects.filter(sales_rep=rep, status__in=[Invoice.POSTED, Invoice.CANCELLED], date=d,
                                           invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE])
                    .select_related('partner').order_by('-id'))
    _decorate(invoices)
    agg = Invoice.objects.filter(sales_rep=rep, status=Invoice.POSTED, date=d).aggregate(
        sales=Coalesce(Sum('total_amount', filter=Q(invoice_type=Invoice.SALE)), ZERO, output_field=money_field),
        sales_n=Count('id', filter=Q(invoice_type=Invoice.SALE)),
        sales_cash=Coalesce(Sum('paid_cash', filter=Q(invoice_type=Invoice.SALE)), ZERO, output_field=money_field),
        sales_credit=Coalesce(Sum('paid_credit', filter=Q(invoice_type=Invoice.SALE)), ZERO, output_field=money_field),
        ret=Coalesce(Sum('total_amount', filter=Q(invoice_type=Invoice.RETURN_SALE)), ZERO, output_field=money_field),
        ret_n=Count('id', filter=Q(invoice_type=Invoice.RETURN_SALE)),
        ret_cash=Coalesce(Sum('total_amount', filter=Q(invoice_type=Invoice.RETURN_SALE, payment_type='CASH')),
                          ZERO, output_field=money_field),
        customers=Count('partner', filter=Q(invoice_type=Invoice.SALE), distinct=True))
    agg['ret_credit'] = agg['ret'] - agg['ret_cash']
    agg['net'] = agg['sales'] - agg['ret']
    receipts = list(Voucher.objects.filter(voucher_type=Voucher.RECEIPT, status=Voucher.POSTED, date=d,
                                           treasury_id=rep.treasury_id).select_related('partner').order_by('-id')) \
        if rep.treasury_id else []
    collected = sum((v.amount for v in receipts), ZERO)
    handovers = list(RepCashHandover.objects.filter(rep=rep, date=d).select_related('treasury', 'bank_account'))
    handed = sum((h.amount for h in handovers), ZERO)
    shortage = sum((h.shortage_charged for h in handovers), ZERO)
    opening = _box_balance_until(rep, d, inclusive=False)
    closing = _box_balance_until(rep, d)
    base = F('quantity') * Coalesce(F('uom__conversion_factor'), Decimal('1'))
    qty = DecimalField(max_digits=20, decimal_places=4)
    items = list(InvoiceLine.objects.filter(invoice__in=[i.id for i in invoices if i.status == Invoice.POSTED]).order_by()
                 .values('product__name').annotate(
                     sold=Coalesce(Sum(base, filter=Q(invoice__invoice_type=Invoice.SALE)), ZERO, output_field=qty),
                     back=Coalesce(Sum(base, filter=Q(invoice__invoice_type=Invoice.RETURN_SALE)), ZERO, output_field=qty),
                     amount=Coalesce(Sum('total_amount', filter=Q(invoice__invoice_type=Invoice.SALE)), ZERO,
                                     output_field=money_field))
                 .order_by('-amount', 'product__name'))
    for it in items:
        it['sold'], it['back'] = f"{it['sold'].normalize():f}", f"{it['back'].normalize():f}"
    return render(request, 'rep_app/day.html', _ctx(
        request, 'home', title='يوميتي', d=d, today=today,
        prev=d - datetime.timedelta(days=1), next=(d + datetime.timedelta(days=1)) if d < today else None,
        invoices=invoices, a=agg, receipts=receipts, collected=collected, handovers=handovers, handed=handed,
        shortage=shortage, opening=opening, closing=closing, items=items, cash_now=van.cash_balance(rep)))
