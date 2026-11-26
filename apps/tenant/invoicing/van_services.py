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


def van_stock(warehouse, product_ids=None):
    """{product_id: quantity on hand (base unit)} — one query."""
    from apps.tenant.inventory.models import InventoryBatch
    if warehouse is None:
        return {}
    qs = InventoryBatch.objects.filter(warehouse=warehouse, quantity_remaining__gt=0)
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
            out[key] = {'product': product, 'uom': uom, 'factor': factor, 'qty': qty, 'base_qty': qty * factor}
    if not out:
        raise ValidationError('ضيف صنف واحد على الأقل بكمية.')
    return out


def _check_stock(warehouse, needed, label):
    """needed: {product: base qty}. Raises when the warehouse doesn't have it."""
    have = van_stock(warehouse, [p.id for p in needed])
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
        _check_stock(order.from_warehouse, needed, f'الرصيد في {order.from_warehouse.name} مش كفاية')
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
                                notes=f'طلب تحميل {order.number} — المندوب {rep_name(order.rep)}')
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
        UnloadingOrderLine.objects.bulk_create([UnloadingOrderLine(order=order, product=p, sound_qty=s, damaged_qty=d)
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


def rep_sale(rep, user, rows, partner=None, payment='CASH', paid_amount=None, discount_pct=ZERO, notes=''):
    """Sell from the van. payment: CASH | CREDIT | PARTIAL (paid_amount cash, the rest on credit).
    Prices come from the pricing engine (never from the phone); stock must be in the van."""
    from apps.tenant.core.models import SystemSetting
    from apps.tenant.services.invoice_service import confirm_invoice
    from .views import _is_generic_cash_customer, _max_discount_pct_for, _pos_unit_price

    if not rep.van_warehouse_id or not rep.treasury_id:
        raise ValidationError('المندوب لازم يكون ليه مخزن عربية وخزنة — كلّم الإدارة.')
    if payment not in ('CASH', 'CREDIT', 'PARTIAL'):
        raise ValidationError('طريقة دفع غير معروفة.')
    discount_pct = _dec(discount_pct, 'الخصم')
    cap = _max_discount_pct_for(user, SystemSetting.get_settings())
    if discount_pct > 100 or (cap is not None and discount_pct > cap):
        raise ValidationError(f'الخصم أكبر من المسموح ({cap if cap is not None else 100}%).')
    real_customer = partner is not None and not _is_generic_cash_customer(partner)
    if payment != 'CASH' and not real_customer:
        raise ValidationError('البيع الآجل لازم يكون لعميل مسجّل.')
    partner = partner or _cash_customer()
    items = _product_rows(rows, allow_service=True)
    _check_stock(rep.van_warehouse,
                 _by_product([i for i in items.values() if i['product'].product_type != 'SERVICE']),
                 'رصيد العربية مش كفاية')
    warehouse = rep.van_warehouse
    branch = warehouse.branch

    with transaction.atomic():
        invoice = Invoice.objects.create(
            invoice_type=Invoice.SALE, branch=branch, warehouse=warehouse, partner=partner,
            date=timezone.localdate(), payment_type='CASH', treasury=rep.treasury, status=Invoice.DRAFT,
            cashier=user, discount_percentage=discount_pct, sales_rep=rep,
            notes=(notes or '').strip() or f'بيع مندوب — {rep_name(rep)}')
        for i in items.values():
            price, min_price = _pos_unit_price(i['product'], i['uom'], partner, branch, i['qty'])
            net = price * (Decimal('1') - discount_pct / 100)
            if min_price > 0 and net < min_price:
                raise ValidationError(f'"{i["product"].name}": السعر بعد الخصم أقل من أدنى سعر بيع.')
            InvoiceLine.objects.create(invoice=invoice, product=i['product'], quantity=i['qty'], unit_price=price,
                                       uom=i['uom'], tax_rate=i['product'].tax_rate or ZERO, wht_rate=ZERO)
        invoice.recalculate_totals()
        total = Decimal(str(invoice.total_amount)).quantize(CENT)
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
                raise ValidationError(f'العميل هيعدّي حد الائتمان ({partner.credit_limit:.2f}) — '
                                      f'رصيده الحالي {balance:.2f}.')
        invoice.payment_type = 'CASH' if credit == 0 else ('CREDIT' if cash == 0 else 'SPLIT')
        invoice.paid_cash, invoice.paid_credit = cash, credit
        invoice.save(update_fields=['payment_type', 'paid_cash', 'paid_credit', 'updated_at'])
        return confirm_invoice(invoice.id)


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

def post_handover(rep, amount, user=None, treasury=None, bank_account=None, charge_shortage=False, notes=''):
    from apps.tenant.accounting.models import Treasury
    from apps.tenant.services.journal_service import _get_system_account
    from .shift_services import _post
    amount = _dec(amount, 'المبلغ').quantize(CENT)
    if not rep.treasury_id:
        raise ValidationError('المندوب مالوش خزنة.')
    if (treasury is None) == (bank_account is None):
        raise ValidationError('اختار خزنة أو بنك يستلم الفلوس.')
    if treasury is not None and treasury.pk == rep.treasury_id:
        raise ValidationError('مينفعش التوريد لنفس خزنة المندوب.')
    with transaction.atomic():
        Treasury.objects.select_for_update().filter(pk=rep.treasury_id).first()     # one hand-over at a time
        rep_acc = rep.treasury.ensure_account()
        dest_acc = (treasury or bank_account).ensure_account()
        if rep_acc is None or dest_acc is None:
            raise ValidationError('الخزنة أو البنك مش مربوط بحساب.')
        balance = (rep_acc.current_balance or ZERO).quantize(CENT)
        if amount <= 0 and not (charge_shortage and balance > 0):
            raise ValidationError('اكتب المبلغ المورّد.')
        if amount > balance:
            raise ValidationError(f'المبلغ أكبر من رصيد خزنة المندوب ({balance:.2f}).')
        shortage = (balance - amount) if charge_shortage else ZERO
        h = RepCashHandover.objects.create(rep=rep, expected=balance, amount=amount, shortage_charged=shortage,
                                           treasury=treasury, bank_account=bank_account,
                                           notes=(notes or '').strip()[:255], created_by=user)
        name = rep_name(rep)
        label = f'توريد نقدية المندوب {name} — {h.number}'
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
