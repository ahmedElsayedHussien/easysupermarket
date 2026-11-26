"""
بيع المناديب (عربيات التوزيع).

Each rep (SalesRep) has a van warehouse, a cash box (Treasury) and a source warehouse he loads from.

  طلب تحميل   PENDING → approve (approved quantities, stock checked) → APPROVED → issue → ISSUED
              (issue = one StockTransfer source → van, FIFO cost; nothing posts to the GL: same inventory account)
  طلب تفريغ   PENDING → receive (actual sound / damaged quantities) → RECEIVED
              (sound → source warehouse, damaged → the damaged warehouse when chosen)
  بيع         SALE invoice from the van warehouse; cash lands in the rep's cash box, credit on the customer
  تحصيل       receipt voucher: Dr rep cash box / Cr customer
  توريد       Dr receiving treasury/bank / Cr rep cash box; a shortage charged to the rep:
              Dr 1135 / Cr rep cash box + a shortage loan deducted from his next payroll
"""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (Invoice, InvoiceLine, LoadingOrder, LoadingOrderLine, RepCashHandover, SalesRep,
                     UnloadingOrder, UnloadingOrderLine)

ZERO = Decimal('0')
CENT = Decimal('0.01')
EMPLOYEES_ACCOUNT_CODE = '1135'


def _dec(value, label):
    try:
        d = Decimal(str(value).strip().replace(',', '')) if value not in (None, '') else ZERO
    except (InvalidOperation, ValueError):
        raise ValidationError(f'{label}: رقم غير صالح.')
    if not d.is_finite() or d < 0:
        raise ValidationError(f'{label}: لازم رقم موجب.')
    return d


def rep_name(rep):
    u = rep.employee.user
    return u.get_full_name() or u.username


# ---------------------------------------------------------------------------
# Setup & balances
# ---------------------------------------------------------------------------

def default_source_warehouse(rep):
    from apps.tenant.inventory.models import Warehouse
    if rep.source_warehouse_id:
        return rep.source_warehouse
    vans = SalesRep.objects.exclude(van_warehouse=None).values('van_warehouse')
    qs = Warehouse.objects.filter(is_active=True).exclude(pk__in=vans)
    branch_id = rep.van_warehouse.branch_id if rep.van_warehouse_id else rep.employee.branch_id
    return (qs.filter(branch_id=branch_id).order_by('pk').first() if branch_id else None) or qs.order_by('pk').first()


def ensure_rep_setup(rep, create_van=False, create_treasury=False):
    """Create the rep's van warehouse and/or cash box when asked and missing."""
    from apps.tenant.accounting.models import Treasury
    from apps.tenant.core.models import Branch
    from apps.tenant.inventory.models import Warehouse
    branch = rep.employee.branch or Branch.objects.order_by('pk').first()
    changed = []
    if create_van and not rep.van_warehouse_id and branch:
        code = f'VAN-{rep.pk}'
        n = 1
        while Warehouse.objects.filter(code=code).exists():
            n += 1
            code = f'VAN-{rep.pk}-{n}'
        rep.van_warehouse = Warehouse.objects.create(branch=branch, name=f'عربية {rep_name(rep)}', code=code,
                                                     description='مخزن عربية مندوب')
        changed.append('van_warehouse')
    if create_treasury and not rep.treasury_id and branch:
        t = Treasury.objects.create(branch=branch, name=f'خزنة المندوب {rep_name(rep)}')
        t.ensure_account()
        rep.treasury = t
        changed.append('treasury')
    if changed:
        rep.save(update_fields=changed)
    return rep


def sync_rep_branch(rep):
    """The rep works in his van's branch: his employee branch follows it (set automatically when the
    van is in another branch or he has none). Returns that branch (None when nothing is set)."""
    target = rep.van_warehouse.branch if rep.van_warehouse_id else rep.employee.branch
    emp = rep.employee
    if target is not None and emp.branch_id != target.id:
        emp.branch = target
        emp.save(update_fields=['branch'])
    return target


def van_stock(warehouse, product_ids=None, sellable=False):
    """{product_id: quantity on hand (base unit)} — one query. ``sellable``: expired batches left out."""
    from django.db.models import Q
    from apps.tenant.inventory.models import InventoryBatch
    if warehouse is None:
        return {}
    qs = InventoryBatch.objects.filter(warehouse=warehouse, quantity_remaining__gt=0)
    if sellable:
        qs = qs.filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate()))
    if product_ids is not None:
        qs = qs.filter(product_id__in=list(product_ids))
    return dict(qs.order_by().values('product_id').annotate(q=Sum('quantity_remaining')).values_list('product_id', 'q'))


def cash_balance(rep):
    if not rep.treasury_id:
        return ZERO
    acc = rep.treasury.ensure_account()
    return (acc.current_balance if acc else ZERO) or ZERO


def cash_balances(reps):
    """{rep_id: cash-box balance} for many reps in ONE query (cash-box accounts are leaf, debit-normal)."""
    from django.db.models import DecimalField, F
    from django.db.models.functions import Coalesce
    from apps.tenant.accounting.models import JournalEntry, JournalItem
    by_acc = {r.treasury.account_id: r.id for r in reps if r.treasury_id and r.treasury.account_id}
    sums = dict(JournalItem.objects.filter(account_id__in=list(by_acc), entry__status=JournalEntry.POSTED)
                .order_by().values('account_id').annotate(
                    b=Coalesce(Sum(F('debit') - F('credit'), output_field=DecimalField(max_digits=18, decimal_places=2)),
                               ZERO)).values_list('account_id', 'b'))
    return {rep_id: (sums.get(acc) or ZERO).quantize(CENT) for acc, rep_id in by_acc.items()}


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

def _product_rows(rows, allow_service=False):
    """[{'product_id','uom_id','qty'}] → OrderedDict {(product_id, uom_id): item} with product, uom, factor,
    qty, base_qty (duplicates merged)."""
    from apps.tenant.inventory.models import Product, ProductUoM
    ids = [int(r['product_id']) for r in rows if str(r.get('product_id') or '').isdigit()]
    products = Product.objects.in_bulk(ids)
    uom_ids = [int(r['uom_id']) for r in rows if str(r.get('uom_id') or '').isdigit()]
    uoms = ProductUoM.objects.select_related('uom').in_bulk(uom_ids)
    out = OrderedDict()
    for r in rows:
        if not str(r.get('product_id') or '').isdigit():
            continue
        product = products.get(int(r['product_id']))
        if product is None or not product.is_active:
            raise ValidationError('صنف غير موجود أو غير نشط.')
        qty = _dec(r.get('qty'), product.name)
        if qty <= 0:
            continue
        if product.product_type == 'SERIALIZED':
            raise ValidationError(f'"{product.name}" صنف بالسيريال — مش متاح في عربية المندوب.')
        if product.product_type == 'SERVICE' and not allow_service:
            raise ValidationError(f'"{product.name}" خدمة ومالهاش مخزون.')
        uom = uoms.get(int(r['uom_id'])) if str(r.get('uom_id') or '').isdigit() else None
        if uom is not None and uom.product_id != product.id:
            raise ValidationError(f'الوحدة مش بتاعة "{product.name}".')
        if uom is not None and uom.is_base:
            uom = None
        factor = (uom.conversion_factor if uom else Decimal('1')) or Decimal('1')
        key = (product.id, uom.id if uom else None)
        if key in out:
            out[key]['qty'] += qty
            out[key]['base_qty'] += qty * factor
        else:
            out[key] = {'product': product, 'uom': uom, 'factor': factor, 'qty': qty, 'base_qty': qty * factor,
                        'disc': _dec(r.get('disc'), product.name),
                        'disc_type': 'amt' if r.get('disc_type') == 'amt' else 'pct',
                        'asked_price': _dec(r.get('price'), product.name) if r.get('price') not in (None, '') else None}
    if not out:
        raise ValidationError('ضيف صنف واحد على الأقل بكمية.')
    return out


def _check_stock(warehouse, needed, label, sellable=False):
    """needed: {product: base qty}. Raises when the warehouse doesn't have it."""
    have = van_stock(warehouse, [p.id for p in needed], sellable=sellable)
    short = [f'"{p.name}" (المطلوب {q.normalize():f} — المتاح {(have.get(p.id) or ZERO).normalize():f})'
             for p, q in needed.items() if q > (have.get(p.id) or ZERO)]
    if short:
        raise ValidationError(f'{label}: ' + '، '.join(short))


def _by_product(items):
    out = OrderedDict()
    for i in items:
        out[i['product']] = out.get(i['product'], ZERO) + i['base_qty']
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def create_loading_request(rep, rows, user=None, notes=''):
    if not rep.van_warehouse_id:
        raise ValidationError('المندوب مالوش مخزن عربية — حدده من شاشة المندوب.')
    source = default_source_warehouse(rep)
    if source is None or source.pk == rep.van_warehouse_id:
        raise ValidationError('حدد المخزن اللي المندوب بيحمّل منه.')
    items = _product_rows(rows)
    with transaction.atomic():
        order = LoadingOrder.objects.create(rep=rep, from_warehouse=source, to_warehouse=rep.van_warehouse,
                                            notes=(notes or '').strip(), requested_by=user)
        LoadingOrderLine.objects.bulk_create([
            LoadingOrderLine(order=order, product=i['product'], uom=i['uom'], conversion_factor=i['factor'],
                             requested_qty=i['qty']) for i in items.values()])
    return order


def _locked(model, pk):
    return model.objects.select_for_update(of=('self',)).get(pk=pk)


def approve_loading(order, approved, user=None):
    """approved: {line_id: qty in the line's unit} (missing = as requested)."""
    with transaction.atomic():
        order = _locked(LoadingOrder, order.pk)
        if order.status != LoadingOrder.PENDING:
            raise ValidationError('الطلب مش مستني اعتماد.')
        lines = list(order.lines.select_related('product', 'uom__uom'))
        needed = OrderedDict()
        for line in lines:
            qty = _dec(approved.get(line.id, line.requested_qty), line.product.name)
            if qty > line.requested_qty:
                raise ValidationError(f'"{line.product.name}": المعتمد أكبر من المطلوب.')
            line.approved_qty = qty
            if qty > 0:
                needed[line.product] = needed.get(line.product, ZERO) + qty * line.conversion_factor
        if not needed:
            raise ValidationError('اعتمد كمية لصنف واحد على الأقل، أو الغي الطلب.')
        _check_stock(order.from_warehouse, needed, f'الرصيد الصالح (غير المنتهي) في {order.from_warehouse.name} مش كفاية',
                     sellable=True)
        LoadingOrderLine.objects.bulk_update(lines, ['approved_qty'])
        order.status, order.approved_by, order.approved_at = LoadingOrder.APPROVED, user, timezone.now()
        order.save(update_fields=['status', 'approved_by', 'approved_at'])
    return order


def issue_loading(order, user=None):
    from apps.tenant.inventory.transfer_services import post_transfer
    with transaction.atomic():
        order = _locked(LoadingOrder, order.pk)
        if order.status != LoadingOrder.APPROVED:
            raise ValidationError('الطلب لازم يتعمد الأول قبل الصرف.')
        items = [{'product': l.product, 'uom': l.uom, 'factor': l.conversion_factor, 'qty': l.approved_qty,
                  'base_qty': l.approved_qty * l.conversion_factor}
                 for l in order.lines.select_related('product', 'uom') if (l.approved_qty or ZERO) > 0]
        try:
            doc = post_transfer(order.from_warehouse, order.to_warehouse, items, user=user,
                                notes=f'طلب تحميل {order.number} — المندوب {rep_name(order.rep)}',
                                skip_expired=True)
        except ValueError as e:
            raise ValidationError(str(e))
        order.status, order.issued_by, order.issued_at, order.transfer = LoadingOrder.ISSUED, user, timezone.now(), doc
        order.save(update_fields=['status', 'issued_by', 'issued_at', 'transfer'])
    return order


def cancel_loading(order, user=None):
    with transaction.atomic():
        order = _locked(LoadingOrder, order.pk)
        if order.status not in (LoadingOrder.PENDING, LoadingOrder.APPROVED):
            raise ValidationError('الطلب اتصرف أو اتلغى قبل كده.')
        order.status = LoadingOrder.CANCELLED
        order.save(update_fields=['status'])
    return order


# ---------------------------------------------------------------------------
# Unloading
# ---------------------------------------------------------------------------

def create_unloading_request(rep, rows, user=None, notes=''):
    """rows: [{'product_id', 'sound', 'damaged'}] in base units."""
    from apps.tenant.inventory.models import Product
    if not rep.van_warehouse_id:
        raise ValidationError('المندوب مالوش مخزن عربية.')
    target = default_source_warehouse(rep)
    if target is None or target.pk == rep.van_warehouse_id:
        raise ValidationError('حدد المخزن اللي المندوب بيحمّل منه (هيرجّع له).')
    products = Product.objects.in_bulk([int(r['product_id']) for r in rows if str(r.get('product_id') or '').isdigit()])
    lines, needed = [], OrderedDict()
    for r in rows:
        product = products.get(int(r['product_id'])) if str(r.get('product_id') or '').isdigit() else None
        if product is None:
            continue
        sound, damaged = _dec(r.get('sound'), product.name), _dec(r.get('damaged'), product.name)
        if sound + damaged <= 0:
            continue
        lines.append((product, sound, damaged))
        needed[product] = needed.get(product, ZERO) + sound + damaged
    if not lines:
        raise ValidationError('اكتب كمية لصنف واحد على الأقل.')
    _check_stock(rep.van_warehouse, needed, 'رصيد العربية مش كفاية')
    with transaction.atomic():
        order = UnloadingOrder.objects.create(rep=rep, from_warehouse=rep.van_warehouse, to_warehouse=target,
                                              notes=(notes or '').strip(), requested_by=user)
        UnloadingOrderLine.objects.bulk_create([UnloadingOrderLine(order=order, product=p, sound_qty=s, damaged_qty=d,
                                                                   requested_sound=s, requested_damaged=d)
                                                for p, s, d in lines])
    return order


def receive_unloading(order, actual, user=None, damaged_warehouse=None):
    """actual: {line_id: (sound, damaged)} as counted in the warehouse (missing = as requested)."""
    from apps.tenant.inventory.transfer_services import post_transfer
    with transaction.atomic():
        order = _locked(UnloadingOrder, order.pk)
        if order.status != UnloadingOrder.PENDING:
            raise ValidationError('الطلب مش مستني استلام.')
        if damaged_warehouse is not None and damaged_warehouse.pk == order.from_warehouse_id:
            raise ValidationError('مخزن التوالف لازم يكون غير عربية المندوب.')
        lines = list(order.lines.select_related('product'))
        needed = OrderedDict()
        for line in lines:
            s, d = actual.get(line.id, (line.sound_qty, line.damaged_qty))
            line.sound_qty, line.damaged_qty = _dec(s, line.product.name), _dec(d, line.product.name)
            if line.sound_qty + line.damaged_qty > 0:
                needed[line.product] = needed.get(line.product, ZERO) + line.sound_qty + line.damaged_qty
        if not needed:
            raise ValidationError('مفيش كميات اتستلمت — الغي الطلب بدل كده.')
        _check_stock(order.from_warehouse, needed, 'رصيد العربية مش كفاية')
        UnloadingOrderLine.objects.bulk_update(lines, ['sound_qty', 'damaged_qty'])
        note = f'طلب تفريغ {order.number} — المندوب {rep_name(order.rep)}'

        def items(field):
            return [{'product': l.product, 'uom': None, 'factor': Decimal('1'), 'qty': getattr(l, field),
                     'base_qty': getattr(l, field)} for l in lines if getattr(l, field) > 0]
        damaged_to = damaged_warehouse or order.to_warehouse
        try:
            if damaged_to.pk == order.to_warehouse_id:
                merged = OrderedDict()
                for l in lines:
                    q = l.sound_qty + l.damaged_qty
                    if q > 0:
                        merged[l.product] = merged.get(l.product, ZERO) + q
                order.transfer = post_transfer(order.from_warehouse, order.to_warehouse, [
                    {'product': p, 'uom': None, 'factor': Decimal('1'), 'qty': q, 'base_qty': q}
                    for p, q in merged.items()], user=user, notes=note)
            else:
                if items('sound_qty'):
                    order.transfer = post_transfer(order.from_warehouse, order.to_warehouse, items('sound_qty'),
                                                   user=user, notes=note + ' (سليم)')
                if items('damaged_qty'):
                    order.damaged_transfer = post_transfer(order.from_warehouse, damaged_to, items('damaged_qty'),
                                                           user=user, notes=note + ' (تالف)')
        except ValueError as e:
            raise ValidationError(str(e))
        order.damaged_warehouse = damaged_warehouse
        order.status, order.received_by, order.received_at = UnloadingOrder.RECEIVED, user, timezone.now()
        order.save(update_fields=['status', 'received_by', 'received_at', 'transfer', 'damaged_transfer',
                                  'damaged_warehouse'])
    return order


def cancel_unloading(order, user=None):
    with transaction.atomic():
        order = _locked(UnloadingOrder, order.pk)
        if order.status != UnloadingOrder.PENDING:
            raise ValidationError('الطلب اتستلم أو اتلغى قبل كده.')
        order.status = UnloadingOrder.CANCELLED
        order.save(update_fields=['status'])
    return order


# ---------------------------------------------------------------------------
# Selling & collecting (rep app)
# ---------------------------------------------------------------------------

def _cash_customer():
    from apps.tenant.partners.models import Partner
    partner, _ = Partner.objects.get_or_create(name='عميل نقدي',
                                               defaults={'partner_type': 'CUSTOMER', 'is_active': True})
    return partner


def price_cart(rep, user, rows, partner=None, invoice_disc=ZERO, invoice_disc_type='pct', returning=False):
    """Server prices for the cart — used by the on-screen quote AND by saving, so both always agree.

    Discounts may be a % or a fixed amount (EGP), per line and on the whole invoice. Everything is turned
    into one exact discount AMOUNT per line (InvoiceLine.discount_fixed); a fixed invoice discount is spread
    over the lines by their value (the last line takes the rounding rest, so the sum is exact). VAT per line
    from the product; withholding tax (خصم وإضافة) when the customer is subject to it."""
    from apps.tenant.core.models import SystemSetting
    from .views import _max_discount_pct_for, _pos_unit_price
    Q4, HUNDRED = Decimal('0.0001'), Decimal('100')
    invoice_disc = _dec(invoice_disc, 'خصم الفاتورة')
    if invoice_disc_type == 'pct' and invoice_disc > HUNDRED:
        raise ValidationError('خصم الفاتورة لازم يكون بين 0 و 100%.')
    setting = SystemSetting.get_settings()
    cap = _max_discount_pct_for(user, setting)
    can_edit_price = bool(getattr(setting, 'rep_allow_price_edit', False))
    margin = Decimal(str(getattr(setting, 'rep_price_margin_percent', 0) or 0))
    items = list((_product_rows(rows, allow_service=True) if rows else {}).values())
    branch = rep.van_warehouse.branch if rep.van_warehouse_id else None
    customer = partner or _cash_customer()
    # Taxes come from the CUSTOMER card (partners): VAT at his rate unless the product itself is exempt
    # (tax_rate 0), withholding at his rate when he is subject to it. Nothing from the phone changes them.
    vat_rate = (customer.vat_rate or ZERO) if customer.subject_to_vat else ZERO
    with_wht = bool(customer.subject_to_withholding_tax)
    cust_wht_rate = (customer.wht_rate or ZERO) if with_wht else ZERO

    # 1) price & the line's own discount
    for i in items:
        p = i['product']
        i['price'], i['min_price'] = _pos_unit_price(p, i['uom'], customer, branch, i['qty'])
        i['list_price'] = i['price']
        asked = i.get('asked_price')
        if asked is not None and can_edit_price and asked != i['price']:      # the rep typed his own price
            if returning and asked > i['price']:
                raise ValidationError(f'"{p.name}": في المرتجع السعر ميزيدش عن {i["price"]:.2f}.')
            if margin > 0 and i['price'] > 0 and abs(asked - i['price']) * HUNDRED / i['price'] > margin + Decimal('0.01'):
                raise ValidationError(f'"{p.name}": السعر ({asked:.2f}) خارج الهامش المسموح '
                                      f'(±{margin.normalize():f}% من {i["price"]:.2f}).')
            i['price'] = asked
        i['gross'] = (i['qty'] * i['price']).quantize(Q4)
        val = i.get('disc') or ZERO
        if i.get('disc_type') == 'amt':
            if val > i['gross']:
                raise ValidationError(f'"{p.name}": الخصم ({val:.2f}) أكبر من قيمة الصنف ({i["gross"]:.2f}).')
            i['line_disc'] = val.quantize(Q4)
        else:
            if val > HUNDRED:
                raise ValidationError(f'"{p.name}": الخصم لازم يكون بين 0 و 100%.')
            i['line_disc'] = (i['gross'] * val / HUNDRED).quantize(Q4)
    # 2) the invoice discount on what is left of each line
    rest = [i['gross'] - i['line_disc'] for i in items]
    base = sum(rest, ZERO)
    if invoice_disc_type == 'amt':
        if invoice_disc > base:
            raise ValidationError(f'خصم الفاتورة ({invoice_disc:.2f}) أكبر من قيمتها ({base:.2f}).')
        shares, left = [], invoice_disc
        for n, r in enumerate(rest):
            s = left if n == len(rest) - 1 else (invoice_disc * r / base).quantize(Q4) if base else ZERO
            shares.append(s)
            left -= s
    else:
        shares = [(r * invoice_disc / HUNDRED).quantize(Q4) for r in rest]

    lines, t = [], {k: ZERO for k in ('gross', 'discount', 'subtotal', 'tax', 'wht')}
    taxes = {}
    for i, share in zip(items, shares):
        p = i['product']
        disc = i['line_disc'] + share
        eff = (disc * HUNDRED / i['gross']).quantize(Q4) if i['gross'] else ZERO
        if cap is not None and eff > cap + Decimal('0.01'):
            raise ValidationError(f'"{p.name}": الخصم ({eff:.2f}%) أكبر من المسموح لك ({cap.normalize():f}%).')
        sub = (i['gross'] - disc).quantize(Q4)
        if (i['min_price'] > 0 and i['qty'] and sub / i['qty'] < i['min_price'] - Decimal('0.005')
                and not user.has_perm('inventory.sell_below_min_price')):
            raise ValidationError(f'"{p.name}": السعر بعد الخصم أقل من أدنى سعر بيع ({i["min_price"]:.2f}).')
        rate = vat_rate if (p.tax_rate or ZERO) > 0 else ZERO
        wht_rate = cust_wht_rate
        tax = (sub * rate / HUNDRED).quantize(Q4)
        wht = (sub * wht_rate / HUNDRED).quantize(Q4)
        lines.append({**i, 'disc_amount': disc, 'disc_pct': eff, 'discount': disc, 'subtotal': sub, 'tax_rate': rate,
                      'tax': tax, 'wht_rate': wht_rate, 'wht': wht, 'total': sub + tax - wht})
        for k, v in (('gross', i['gross']), ('discount', disc), ('subtotal', sub), ('tax', tax), ('wht', wht)):
            t[k] += v
        if rate:
            taxes[rate] = taxes.get(rate, ZERO) + tax
    return {'lines': lines, **t, 'taxes': sorted(taxes.items()), 'with_wht': with_wht,
            'total': (t['subtotal'] + t['tax'] - t['wht']).quantize(CENT),
            'invoice_disc': invoice_disc if invoice_disc_type == 'pct' else ZERO}


def returnable_for(partner, product_ids=None, rep=None):
    """{product_id: base qty the customer can still return} = bought − already returned.
    With ``rep``: only that rep's own invoices (never POS or another rep's sales). Goods only."""
    from django.db.models import Case, DecimalField, F, When
    from django.db.models.functions import Coalesce
    qs = InvoiceLine.objects.filter(invoice__partner=partner, invoice__status=Invoice.POSTED,
                                    invoice__invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE]).exclude(
        product__product_type__in=['SERVICE', 'SERIALIZED'])
    if rep is not None:
        qs = qs.filter(invoice__sales_rep=rep)
    if product_ids is not None:
        qs = qs.filter(product_id__in=list(product_ids))
    base = F('quantity') * Coalesce(F('conversion_factor'), F('uom__conversion_factor'), Decimal('1'))
    money = DecimalField(max_digits=20, decimal_places=4)
    rows = qs.order_by().values('product_id').annotate(
        sold=Coalesce(Sum(Case(When(invoice__invoice_type=Invoice.SALE, then=base), default=0, output_field=money)), ZERO),
        back=Coalesce(Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then=base), default=0,
                               output_field=money)), ZERO))
    return {r['product_id']: max(r['sold'] - r['back'], ZERO) for r in rows if r['sold'] - r['back'] > 0}


def _new_invoice(rep, user, kind, partner, cart, notes, **extra):
    from apps.tenant.services.invoice_service import confirm_invoice
    invoice = Invoice.objects.create(
        invoice_type=kind, branch=rep.van_warehouse.branch, warehouse=rep.van_warehouse, partner=partner,
        date=timezone.localdate(), status=Invoice.DRAFT, cashier=user, sales_rep=rep,
        discount_percentage=cart['invoice_disc'], notes=(notes or '').strip()[:500] or
        f'{"بيع" if kind == Invoice.SALE else "مرتجع"} مندوب — {rep_name(rep)}', **extra)
    for l in cart['lines']:
        InvoiceLine.objects.create(invoice=invoice, product=l['product'], quantity=l['qty'], unit_price=l['price'],
                                   discount_fixed=l['disc_amount'], discount_pct=min(l['disc_pct'], Decimal('100')).quantize(CENT),
                                   uom=l['uom'], tax_rate=l['tax_rate'], wht_rate=l['wht_rate'])
    invoice.recalculate_totals()
    return invoice, confirm_invoice


def rep_sale(rep, user, rows, partner=None, payment='CASH', paid_amount=None, discount_pct=ZERO, notes='',
             due_date=None, discount_type='pct'):
    """Sell from the van. payment: CASH | CREDIT | PARTIAL (paid_amount cash, the rest on credit).
    Prices come from the pricing engine (never from the phone); stock must be in the van."""
    from .views import _is_generic_cash_customer
    if not rep.van_warehouse_id or not rep.treasury_id:
        raise ValidationError('المندوب لازم يكون ليه مخزن عربية وخزنة — كلّم الإدارة.')
    if payment not in ('CASH', 'CREDIT', 'PARTIAL'):
        raise ValidationError('طريقة دفع غير معروفة.')
    real_customer = partner is not None and not _is_generic_cash_customer(partner)
    if payment != 'CASH' and not real_customer:
        raise ValidationError('البيع الآجل لازم يكون لعميل مسجّل.')
    partner = partner or _cash_customer()
    cart = price_cart(rep, user, rows, partner, discount_pct, discount_type)
    if not cart['lines']:
        raise ValidationError('ضيف صنف واحد على الأقل بكمية.')
    _check_stock(rep.van_warehouse,
                 _by_product([l for l in cart['lines'] if l['product'].product_type != 'SERVICE']),
                 'رصيد العربية مش كفاية')
    total = cart['total']
    if payment == 'CASH':
        cash = total
    elif payment == 'CREDIT':
        cash = ZERO
    else:
        cash = _dec(paid_amount, 'المدفوع').quantize(CENT)
        if cash >= total:
            raise ValidationError('المدفوع مغطّي الفاتورة كلها — اختار نقدي.')
    credit = total - cash
    if credit > 0 and (partner.credit_limit or ZERO) > 0:
        balance = Decimal(str(partner.customer_balance or 0))
        if balance + credit > partner.credit_limit:
            raise ValidationError(f'العميل هيعدّي حد الائتمان ({partner.credit_limit:.2f}) — رصيده الحالي {balance:.2f}.')
    with transaction.atomic():
        invoice, confirm = _new_invoice(rep, user, Invoice.SALE, partner, cart, notes, treasury=rep.treasury,
                                        payment_type='CASH', due_date=due_date if credit > 0 else None)
        invoice.payment_type = 'CASH' if credit == 0 else ('CREDIT' if cash == 0 else 'SPLIT')
        invoice.paid_cash, invoice.paid_credit = cash, credit
        invoice.save(update_fields=['payment_type', 'paid_cash', 'paid_credit', 'updated_at'])
        return confirm(invoice.id)


def rep_return(rep, user, rows, partner, refund='CREDIT', discount_pct=ZERO, notes='', discount_type='pct',
               without_invoice=False):
    """A customer gives goods back to the rep: they go back into the van. Only a registered customer,
    never more than he bought (net of earlier returns), at today's server price; refund in cash from the
    rep's cash box (if it holds enough) or off the customer's balance."""
    from .views import _is_generic_cash_customer
    if not rep.van_warehouse_id or not rep.treasury_id:
        raise ValidationError('المندوب لازم يكون ليه مخزن عربية وخزنة — كلّم الإدارة.')
    if partner is None or _is_generic_cash_customer(partner):
        raise ValidationError('المرتجع لازم يكون لعميل مسجّل.')
    if refund not in ('CASH', 'CREDIT'):
        raise ValidationError('طريقة الرد غير معروفة.')
    cart = price_cart(rep, user, rows, partner, discount_pct, discount_type, returning=True)
    if not cart['lines']:
        raise ValidationError('ضيف صنف واحد على الأقل بكمية.')
    if any(l['product'].product_type == 'SERVICE' for l in cart['lines']):
        raise ValidationError('الخدمات مالهاش مرتجع.')
    need = _by_product(cart['lines'])
    if any(p.product_type == 'SERIALIZED' for p in need):
        raise ValidationError('الأصناف بالسيريال مرتجعها من الإدارة.')
    if without_invoice:
        from apps.tenant.core.models import SystemSetting
        setting = SystemSetting.get_settings()
        if not getattr(setting, 'rep_allow_return_without_invoice', False):
            raise ValidationError('المرتجع بدون فاتورة مقفول — كلّم الإدارة.')
        cap = Decimal(str(getattr(setting, 'rep_return_without_invoice_max', 0) or 0))
        if cap > 0 and cart['total'] > cap:
            raise ValidationError(f'المرتجع بدون فاتورة حده الأقصى {cap:.2f} — ده {cart["total"]:.2f}.')
        notes = ('مرتجع بدون فاتورة — ' + (notes or '').strip()).strip(' —')
    else:
        can = returnable_for(partner, [p.id for p in need], rep=rep)
        over = [f'"{p.name}" (اشتراه منك ومتبقي {(can.get(p.id) or ZERO).normalize():f} — طالب {q.normalize():f})'
                for p, q in need.items() if q > (can.get(p.id) or ZERO)]
        if over:
            raise ValidationError('الكمية أكبر من اللي العميل اشتراه منك: ' + '، '.join(over))
    if refund == 'CASH' and cash_balance(rep) < cart['total']:
        raise ValidationError(f'خزنتك فيها {cash_balance(rep):.2f} بس — مش كفاية ترد {cart["total"]:.2f} نقدي. '
                              'اختار «خصم من حساب العميل».')
    with transaction.atomic():
        parent = None if without_invoice else (
            Invoice.objects.filter(partner=partner, sales_rep=rep, invoice_type=Invoice.SALE, status=Invoice.POSTED,
                                   lines__product__in=list(need)).order_by('-date', '-id').first())
        invoice, confirm = _new_invoice(rep, user, Invoice.RETURN_SALE, partner, cart, notes, payment_type=refund,
                                        treasury=rep.treasury if refund == 'CASH' else None, parent_invoice=parent)
        return confirm(invoice.id)


def rep_collect(rep, user, partner, amount, notes=''):
    """Cash collected from a customer by the rep (receipt voucher into the rep's cash box)."""
    from apps.tenant.accounting.models import Voucher
    amount = _dec(amount, 'المبلغ').quantize(CENT)
    if amount <= 0:
        raise ValidationError('اكتب المبلغ المحصّل.')
    if not rep.treasury_id:
        raise ValidationError('المندوب مالوش خزنة — كلّم الإدارة.')
    if partner is None or not partner.receivable_account_id:
        raise ValidationError('اختار العميل.')
    with transaction.atomic():
        v = Voucher.objects.create(
            voucher_type=Voucher.RECEIPT, amount=amount, partner=partner, account=partner.receivable_account,
            payment_method=Voucher.CASH, treasury=rep.treasury, branch=rep.treasury.branch,
            date=timezone.localdate(),
            description=(f'تحصيل المندوب {rep_name(rep)} من {partner.name}' + (f' — {notes}' if notes else ''))[:500])
        v.confirm_voucher()
    return v


# ---------------------------------------------------------------------------
# Cash hand-over (توريد)
# ---------------------------------------------------------------------------

def post_handover(rep, amount, user=None, treasury=None, bank_account=None, charge_shortage=False, notes='',
                  intermediary=None, reference=''):
    from apps.tenant.accounting.models import Treasury
    from apps.tenant.services.journal_service import _get_system_account
    from .shift_services import _post
    amount = _dec(amount, 'المبلغ').quantize(CENT)
    if not rep.treasury_id:
        raise ValidationError('المندوب مالوش خزنة.')
    if sum(x is not None for x in (treasury, bank_account, intermediary)) != 1:
        raise ValidationError('اختار خزنة أو بنك أو شركة وسيطة يستلم الفلوس.')
    if intermediary is not None and not (reference or '').strip():
        raise ValidationError('اكتب رقم عملية الشركة الوسيطة (من إيصال فوري / أمان) عشان المراجعة.')
    if treasury is not None and treasury.pk == rep.treasury_id:
        raise ValidationError('مينفعش التوريد لنفس خزنة المندوب.')
    if intermediary is not None and RepCashHandover.objects.filter(
            intermediary=intermediary, reference=(reference or '').strip()[:100]).exists():
        raise ValidationError(f'رقم العملية ده اتسجّل قبل كده على {intermediary.name} — الإيصال الواحد يتورّد مرة واحدة.')
    with transaction.atomic():
        Treasury.objects.select_for_update().filter(pk=rep.treasury_id).first()     # one hand-over at a time
        rep_acc = rep.treasury.ensure_account()
        dest_acc = (treasury or bank_account or intermediary).ensure_account()
        if rep_acc is None or dest_acc is None:
            raise ValidationError('الخزنة أو البنك مش مربوط بحساب.')
        balance = (rep_acc.current_balance or ZERO).quantize(CENT)
        if amount <= 0 and not (charge_shortage and balance > 0):
            raise ValidationError('اكتب المبلغ المورّد.')
        if amount > balance:
            raise ValidationError(f'المبلغ أكبر من رصيد خزنة المندوب ({balance:.2f}).')
        shortage = (balance - amount) if charge_shortage else ZERO
        h = RepCashHandover.objects.create(rep=rep, expected=balance, amount=amount, shortage_charged=shortage,
                                           treasury=treasury, bank_account=bank_account, intermediary=intermediary,
                                           reference=(reference or '').strip()[:100],
                                           notes=(notes or '').strip()[:255], created_by=user)
        name = rep_name(rep)
        label = f'توريد نقدية المندوب {name} — {h.number}' + (
            f' عن طريق {intermediary.name} (عملية {h.reference})' if intermediary else '')
        lines = []
        if amount > 0:
            lines += [(dest_acc, amount, ZERO, label), (rep_acc, ZERO, amount, label)]
        if shortage > 0:
            sl = f'عجز توريد المندوب {name} — {h.number} (على حسابه)'
            lines += [(_get_system_account(EMPLOYEES_ACCOUNT_CODE), shortage, ZERO, sl), (rep_acc, ZERO, shortage, sl)]
        h.journal_entry = _post(h.number, label, lines, user, h)
        h.save(update_fields=['journal_entry'])
        if shortage > 0:
            from apps.tenant.hr.models import EmployeeLoan
            day = timezone.localdate()
            EmployeeLoan.objects.create(
                employee=rep.employee, source=EmployeeLoan.SHORTAGE, amount=shortage, installments_count=1,
                monthly_installment=shortage, start_year=day.year, start_month=day.month, date=day,
                reason=f'عجز توريد المندوب {h.number}', created_by=user)
    return h


# ---------------------------------------------------------------------------
# Cancelling his own invoice
# ---------------------------------------------------------------------------

def cancel_block_reason(rep, invoice):
    """Why the rep may NOT cancel this invoice (None = he may)."""
    from apps.tenant.core.models import SystemSetting
    if not getattr(SystemSetting.get_settings(), 'rep_allow_cancel_invoice', False):
        return 'إلغاء الفواتير مقفول للمناديب.'
    if invoice.sales_rep_id != rep.id:
        return 'دي مش فاتورتك.'
    if invoice.status != Invoice.POSTED:
        return 'الفاتورة مش مرحّلة.'
    if invoice.invoice_type not in (Invoice.SALE, Invoice.RETURN_SALE):
        return 'نوع الفاتورة ده مالوش إلغاء من هنا.'
    if invoice.date != timezone.localdate():
        return 'تقدر تلغي فواتير النهارده بس — الأقدم من الإدارة.'
    from apps.tenant.einvoicing.models import EInvoiceLog
    if EInvoiceLog.objects.filter(invoice=invoice, status__in=['SUBMITTED', 'VALID', 'CANCEL_PENDING']).exists():
        return 'الفاتورة اتبعتت لمصلحة الضرائب — إلغاؤها من الإدارة.'
    if invoice.return_invoices.exclude(status=Invoice.CANCELLED).exists():
        return 'الفاتورة عليها مرتجع — الغي المرتجع الأول.'
    return None


def rep_cancel(rep, user, invoice, reason):
    """Cancel the rep's own invoice completely: stock back into the very batches (a return's goods out of its
    return batch), the entries reversed (cash box / customer balance), and the reason kept on the invoice."""
    from apps.tenant.services.invoice_service import cancel_invoice
    reason = (reason or '').strip()
    if len(reason) < 3:
        raise ValidationError('اكتب سبب الإلغاء.')
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update(of=('self',)).get(pk=invoice.pk)
        why = cancel_block_reason(rep, invoice)
        if why:
            raise ValidationError(why)
        lines = list(invoice.lines.select_related('product', 'uom'))
        goods = {}
        for l in lines:
            if l.product.product_type != 'SERVICE':
                goods[l.product] = goods.get(l.product, ZERO) + l.base_quantity
        if invoice.invoice_type == Invoice.SALE:
            if (invoice.paid_cash or ZERO) > cash_balance(rep):
                raise ValidationError(f'الفاتورة اتدفع فيها {invoice.paid_cash:.2f} نقدي وخزنتك فيها '
                                      f'{cash_balance(rep):.2f} بس (ورّدت الفلوس) — الإلغاء من الإدارة.')
            if invoice.partner_id:
                can = returnable_for(invoice.partner, [p.id for p in goods], rep=rep)
                short = [p.name for p, q in goods.items() if q > (can.get(p.id) or ZERO)]
                if short:
                    raise ValidationError('العميل رجّع من الأصناف دي بعد الفاتورة — الغي المرتجع الأول: '
                                          + '، '.join(short))
        else:
            _check_stock(invoice.warehouse, goods, 'البضاعة المرتجعة مش في عربيتك دلوقتي')
        try:
            cancel_invoice(invoice.id, user=user)
        except ValueError as e:
            raise ValidationError(str(e))
        invoice.refresh_from_db()
        invoice.notes = (f'{invoice.notes or ""}\n— ألغاها المندوب {rep_name(rep)}: {reason}').strip()[:1000]
        invoice.save(update_fields=['notes', 'updated_at'])
    return invoice
