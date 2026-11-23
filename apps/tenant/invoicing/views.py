import logging

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, permission_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db import models
from .models import Invoice, InvoiceLine, POSSession
from apps.tenant.inventory.models import Product, Warehouse, InventoryBatch
from apps.tenant.core.models import Branch, SystemSetting
from apps.tenant.partners.models import Partner
import json
import re
from decimal import Decimal, InvalidOperation
from apps.tenant.core.decorators import custom_permission_required, is_admin_user
from apps.tenant.core.access import (
    BRANCH_UNASSIGNED_MESSAGE, allowed_branches, get_allowed_branch_ids, get_branch_scope,
    get_working_branch, is_branch_restricted, is_branch_unassigned, set_header_branch,
    shift_branch_lock_error,
)

from django.views.decorators.cache import never_cache
from django.utils.dateparse import parse_date
from apps.tenant.core.settings_helpers import get_pos_defaults, round_cash, setting as setting_value

logger = logging.getLogger(__name__)


_JSON_SCRIPT_ESCAPES = {
    ord('>'): r'\u003E',
    ord('<'): r'\u003C',
    ord('&'): r'\u0026',
}


def _safe_json_for_script(obj):
    """
    Serialize obj to JSON that is safe to embed inside an inline <script> with |safe
    (same escaping Django's json_script uses: <, >, & and line separators are escaped).
    """
    from django.core.serializers.json import DjangoJSONEncoder
    return json.dumps(obj, cls=DjangoJSONEncoder).translate(_JSON_SCRIPT_ESCAPES)


def _scope_by_allowed_branches(qs, request, field='branch_id'):
    """
    Admins: unchanged (all branches). Non-admins: only rows of their own branch;
    a non-admin without a branch gets nothing (get_allowed_branch_ids -> []).
    """
    if is_branch_restricted(request):
        return qs.filter(**{f'{field}__in': get_allowed_branch_ids(request)})
    return qs


# Tolerance for browser float rounding when comparing discounts with the cap.
DISCOUNT_CAP_TOLERANCE = Decimal('0.01')


def _max_discount_pct_for(user, setting=None):
    """
    SystemSetting.max_pos_discount_percentage for non-admins (None = no cap:
    admins, or a cap of 100%).
    """
    if is_admin_user(user):
        return None
    if setting is None:
        setting = SystemSetting.get_settings()
    try:
        cap = Decimal(str(getattr(setting, 'max_pos_discount_percentage', 100)))
    except Exception:
        cap = Decimal('100')
    return None if cap >= Decimal('100') else cap

@never_cache
@login_required
@custom_permission_required('invoicing.view_invoice', redirect_url='core:main_screen')
def pos_view(request):
    """Hybrid Sales Point View"""
    if is_branch_unassigned(request):
        messages.error(request, BRANCH_UNASSIGNED_MESSAGE)
        return redirect('core:main_screen')
    
    # Check if cashier has an open shift
    open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
    if not open_session:
        messages.warning(request, 'يجب فتح وردية أولاً قبل الدخول لنقطة البيع.')
        return redirect('invoicing:pos_open_shift')

    products = Product.objects.filter(is_active=True).select_related('category').prefetch_related('uoms__uom')
    warehouses = Warehouse.objects.filter(is_active=True)
    
    customers = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH'], is_active=True)
    
    # Determine if user is admin
    is_admin = is_admin_user(request.user)
    
    # Get user's branch from Employee profile
    user_branch = getattr(getattr(request.user, 'employee_profile', None), 'branch', None)
    
    # The POS always sells from the OPEN SHIFT's branch (its cash goes to that
    # branch's treasury). The shift was opened on the working branch
    # (core.access.get_working_branch). If an admin's header is on another
    # branch, the header follows the shift so header and POS always match.
    current_branch = open_session.branch if open_session.branch_id else get_working_branch(request)
    if is_admin and current_branch and getattr(request, 'branch', None) != current_branch:
        if getattr(request, 'branch', None) is not None:
            messages.info(request, f'تم ضبط الفرع على "{current_branch.name}" — فرع الوردية المفتوحة. '
                                   'لتغيير فرع البيع أغلق الوردية ثم افتح وردية في الفرع المطلوب.')
        set_header_branch(request, current_branch)
        
    current_warehouse = None
    warehouse_id = request.GET.get('warehouse_id')
    if warehouse_id and str(warehouse_id).isdigit():
        current_warehouse = warehouses.filter(id=int(warehouse_id), branch=current_branch).first()
    if not current_warehouse:
        current_warehouse = warehouses.filter(branch=current_branch).first() if current_branch else None
    
    # Fetch latest sale prices for products
    latest_sale_lines = InvoiceLine.objects.filter(
        invoice__invoice_type='SALE',
        invoice__status='POSTED'
    ).order_by('-id').values('product_id', 'unit_price')[:1000]
    latest_sale_dict = {}
    for item in latest_sale_lines:
        pid = item['product_id']
        if pid not in latest_sale_dict:
            latest_sale_dict[pid] = item['unit_price']

    products_list = list(products[:50])
    p_ids = [p.id for p in products_list]
    from django.db.models import Sum, F
    if current_warehouse and p_ids:
        stock_qs = InventoryBatch.objects.filter(
            product_id__in=p_ids,
            warehouse=current_warehouse,
            quantity_remaining__gt=0
        )
        stock_map = dict(
            stock_qs.values('product_id').annotate(total=Sum('quantity_remaining')).values_list('product_id', 'total')
        )
        cost_data = stock_qs.values('product_id').annotate(
            total_qty=Sum('quantity_remaining'),
            total_val=Sum(F('quantity_remaining') * F('unit_cost'))
        )
        cost_map = {
            item['product_id']: (item['total_val'] / item['total_qty'])
            for item in cost_data if item['total_qty'] and item['total_qty'] > 0
        }
    else:
        stock_map = {}
        cost_map = {}

    for p in products_list:
        p.sale_price = p.get_price_for_branch(current_branch)
        p.pos_stock = stock_map.get(p.id, Decimal('0'))
        p.pos_avg_cost = cost_map.get(p.id, p.last_purchase_price or Decimal('0'))
        p.pos_last_purchase = p.last_purchase_price if p.last_purchase_price else p.pos_avg_cost
        p.pos_last_sale = latest_sale_dict.get(p.id, p.sale_price or Decimal('0'))
    
    from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
    
    if is_admin:
        # Admin sees everything
        treasuries = Treasury.objects.all()
        bank_accounts = BankAccount.objects.all()
        ewallets = EWallet.objects.all()
        branches = Branch.objects.filter(is_active=True)
    else:
        # Non-admin sees only their branch's data
        treasuries = Treasury.objects.filter(branch=current_branch) if current_branch else Treasury.objects.none()
        bank_accounts = BankAccount.objects.all()
        ewallets = EWallet.objects.all()
        branches = Branch.objects.filter(id=current_branch.id) if current_branch else Branch.objects.none()

    last_sale_qs = Invoice.objects.filter(
        invoice_type='SALE',
        status='POSTED'
    )
    if open_session:
        last_sale_qs = last_sale_qs.filter(pos_session=open_session)
    elif current_branch:
        last_sale_qs = last_sale_qs.filter(branch=current_branch)
        
    last_sale = last_sale_qs.order_by('-id').select_related('partner').first()
    last_sale_info = None
    if last_sale and last_sale.partner and 'نقدي' not in last_sale.partner.name:
        last_sale_info = {
            'customer_id': str(last_sale.partner_id),
            'customer_name': last_sale.partner.name,
            'invoice_number': last_sale.invoice_number,
        }

    fallback_warehouse_branch = None
    if not current_warehouse and is_admin:
        first_wh = warehouses.select_related('branch').filter(branch__is_active=True).first()
        if first_wh and first_wh.branch_id != (current_branch.id if current_branch else None):
            fallback_warehouse_branch = first_wh.branch

    context = {
        'products': products_list,
        'warehouses': warehouses.filter(branch=current_branch),
        'customers': customers,
        'current_warehouse': current_warehouse,
        'current_branch': current_branch,
        'fallback_warehouse_branch': fallback_warehouse_branch,
        'treasuries': treasuries,
        'bank_accounts': bank_accounts,
        'ewallets': ewallets,
        'branches': branches,
        'is_admin': is_admin,
        'pos_session': open_session,
        'shift_stats': get_shift_stats_data(open_session),
        'settings': SystemSetting.get_settings(),
        'last_sale_info': last_sale_info,
        **_pos_default_ids(current_branch, open_session),
    }
    return render(request, 'pos/index.html', context)


def _pos_default_ids(branch, open_session=None):
    """Pre-selected payment accounts in the POS header: the branch's defaults
    (get_pos_defaults), the cash treasury falling back to the open shift's treasury."""
    d = get_pos_defaults(branch)
    treasury = d['treasury']
    treasury_id = treasury.id if treasury else (open_session.treasury_id if open_session else None)
    return {
        'pos_default_treasury_id': treasury_id,
        'pos_default_bank_account_id': d['bank_account'].id if d['bank_account'] else None,
        'pos_default_instapay_account_id': d['instapay_account'].id if d['instapay_account'] else None,
        'pos_default_ewallet_id': d['ewallet'].id if d['ewallet'] else None,
    }

@never_cache
@login_required
@custom_permission_required('invoicing.view_invoice', redirect_url='core:main_screen')
def pos_open_shift(request):
    """View to open a new cashier shift"""
    open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
    if open_session:
        messages.info(request, 'لديك وردية مفتوحة بالفعل.')
        return redirect('invoicing:pos')

    from apps.tenant.accounting.models import Treasury

    if is_branch_unassigned(request):
        messages.error(request, BRANCH_UNASSIGNED_MESSAGE)
        return redirect('core:main_screen')
    
    # The shift is opened on the working branch: the header branch for admins,
    # the employee's own branch for everyone else (core.access.get_working_branch).
    user_branch = get_working_branch(request)
    if user_branch is None:
        messages.error(request, 'لا يوجد فرع نشط لفتح الوردية عليه.')
        return redirect('core:main_screen')
    from django.db.models import Q as _Q
    treasuries = Treasury.objects.filter(_Q(branch=user_branch) | _Q(branch__isnull=True))

    if request.method == 'POST':
        opening_balance_str = request.POST.get('opening_balance', '0')
        treasury_id = request.POST.get('treasury_id')
        if not treasury_id:
            messages.error(request, 'برجاء اختيار خزينة.')
        else:
            try:
                opening_balance = Decimal(opening_balance_str)
                treasury = treasuries.get(id=treasury_id)

                POSSession.objects.create(
                    branch=user_branch,
                    treasury=treasury,
                    user=request.user,
                    opening_balance=opening_balance,
                    status=POSSession.OPEN
                )
                messages.success(request, 'تم فتح الوردية بنجاح.')
                return redirect('/invoicing/pos/?new_shift=1')
            except Treasury.DoesNotExist:
                messages.error(request, 'الخزينة المختارة لا تتبع هذا الفرع.')
            except Exception as e:
                messages.error(request, f'حدث خطأ: {str(e)}')

    context = {
        'title': 'فتح وردية كاشير',
        'treasuries': treasuries,
        'current_branch': user_branch
    }
    return render(request, 'pos/open_shift.html', context)


@never_cache
@login_required
@custom_permission_required('invoicing.view_invoice', redirect_url='core:main_screen')
@login_required
@custom_permission_required('invoicing.add_invoice', redirect_url='core:main_screen')
def pos_close_shift(request):
    """View to close an active cashier shift"""
    requested_session_id = (request.POST.get('session_id') or request.GET.get('session_id') or '').strip()
    open_session = None
    if requested_session_id:
        if not requested_session_id.isdigit():
            messages.error(request, 'رقم الوردية غير صالح.')
            return redirect('invoicing:pos_open_shift')
        open_session = POSSession.objects.filter(id=int(requested_session_id), status=POSSession.OPEN).first()
        # Closing another cashier's shift requires an explicit session id AND admin rights
        if open_session and open_session.user_id != request.user.id and not is_admin_user(request.user):
            open_session = None
    else:
        open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
        if not open_session and is_admin_user(request.user):
            other_open = POSSession.objects.filter(status=POSSession.OPEN).count()
            if other_open:
                messages.warning(
                    request,
                    'ليس لديك وردية مفتوحة. لإغلاق وردية كاشير آخر يجب تحديد رقم الوردية صراحةً (session_id).'
                )
                return redirect('invoicing:pos_open_shift')

    if not open_session:
        messages.warning(request, 'ليس لديك وردية مفتوحة لإغلاقها.')
        return redirect('invoicing:pos_open_shift')

    # Calculate Expected Closing Balance and payment breakdowns via direct DB Aggregation
    from django.db.models import Sum, Q, F, Case, When
    from django.db.models.functions import Coalesce

    posted_invoices = Invoice.objects.filter(pos_session=open_session, status=Invoice.POSTED)
    inv_agg = posted_invoices.aggregate(
        total_cash_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_cash')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.CASH), then=F('total_amount') + F('rounding_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_cash_refunds=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=F('paid_cash')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.CASH), then=F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_card_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_card')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.CARD), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_card')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.CARD), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_ewallet_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_ewallet')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.EWALLET), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_ewallet')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.EWALLET), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_bank_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_bank')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.BANK_TRANSFER), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_bank')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.BANK_TRANSFER), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_credit_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_credit')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.CREDIT), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_credit')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.CREDIT), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
    )

    total_cash_sales = inv_agg['total_cash_sales']
    total_cash_refunds = inv_agg['total_cash_refunds']
    total_card_sales = inv_agg['total_card_sales']
    total_ewallet_sales = inv_agg['total_ewallet_sales']
    total_bank_sales = inv_agg['total_bank_sales']
    total_credit_sales = inv_agg['total_credit_sales']

    # Calculate Cash Receipt Vouchers and Cash Payment Vouchers created during the active shift for the treasury
    from apps.tenant.accounting.models import Voucher
    v_base = Voucher.objects.filter(status=Voucher.POSTED).filter(
        Q(payment_method=Voucher.CASH) | Q(payment_method=Voucher.SPLIT, paid_cash__gt=0)
    )
    time_filter = Q(treasury=open_session.treasury, created_at__gte=open_session.start_time)
    if open_session.end_time:
        time_filter &= Q(created_at__lte=open_session.end_time)

    shift_vouchers = v_base.filter(Q(pos_session=open_session) | time_filter).distinct()
    v_cash_expr = Case(
        When(payment_method=Voucher.SPLIT, then=F('paid_cash')),
        default=F('amount'),
        output_field=models.DecimalField(max_digits=18, decimal_places=4)
    )
    v_agg = shift_vouchers.aggregate(
        total_cash_receipts=Coalesce(Sum(v_cash_expr, filter=Q(voucher_type=Voucher.RECEIPT)), Decimal('0.00')),
        total_cash_payments=Coalesce(Sum(v_cash_expr, filter=Q(voucher_type=Voucher.PAYMENT)), Decimal('0.00')),
    )
    total_cash_receipts = v_agg['total_cash_receipts']
    total_cash_payments = v_agg['total_cash_payments']

    # Calculate Cash Rentals created during the active shift for the treasury
    from apps.tenant.rentals.models import Rental
    rental_time_filter = Q(created_at__gte=open_session.start_time)
    if open_session.end_time:
        rental_time_filter &= Q(created_at__lte=open_session.end_time)

    shift_rentals = Rental.objects.filter(
        rental_time_filter & (Q(created_by=open_session.user) | Q(treasury=open_session.treasury))
    )
    total_cash_rentals = shift_rentals.filter(payment_method=Rental.PAYMENT_CASH).aggregate(s=Sum('total_amount'))['s'] or Decimal('0')

    expected_balance = (
        open_session.opening_balance +
        total_cash_sales -
        total_cash_refunds +
        total_cash_receipts -
        total_cash_payments +
        total_cash_rentals
    )

    if request.method == 'POST':
        actual_balance_str = request.POST.get('actual_balance', '').strip()
        if actual_balance_str == '':
            messages.error(request, 'يرجى إدخال مبلغ النقدية الفعلية بالدرج أولاً قبل إغلاق الوردية.')
            from django.urls import reverse
            return redirect(f"{reverse('invoicing:pos_close_shift')}?session_id={open_session.id}")
        try:
            actual_balance = Decimal(actual_balance_str)
            difference = actual_balance - expected_balance
            notes = request.POST.get('notes', '').strip()

            open_session.closing_balance_expected = expected_balance
            open_session.closing_balance_actual = actual_balance
            open_session.difference = difference
            open_session.end_time = timezone.now()
            open_session.status = POSSession.CLOSED
            if notes:
                open_session.notes = notes
            open_session.save()
            
            messages.success(request, f'تم إغلاق الوردية رقم #{open_session.id} بنجاح وتحويل حالتها إلى مغلقة.')
            return redirect('core:report_shift_detail', pk=open_session.id)
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء إغلاق الوردية: {str(e)}')

    context = {
        'title': 'إغلاق الوردية وجرد الدرج',
        'session': open_session,
        'expected_balance': expected_balance,
        'total_cash_sales': total_cash_sales,
        'total_cash_refunds': total_cash_refunds,
        'total_cash_receipts': total_cash_receipts,
        'total_cash_payments': total_cash_payments,
        'total_card_sales': total_card_sales,
        'total_ewallet_sales': total_ewallet_sales,
        'total_bank_sales': total_bank_sales,
        'total_credit_sales': total_credit_sales,
    }
    return render(request, 'pos/close_shift.html', context)

@login_required
@custom_permission_required('invoicing.add_invoice', redirect_url='core:main_screen')
def purchase_invoice_view(request):
    """Purchase Invoice Form"""
    import json
    suppliers = Partner.objects.filter(partner_type__in=['SUPPLIER', 'BOTH'], is_active=True)
    warehouses = Warehouse.objects.filter(is_active=True)
    products = Product.objects.filter(is_active=True).prefetch_related('uoms__uom')
    
    def normalize_name(name):
        if not name: return ""
        return str(name).strip().replace('ة', 'ه').replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').lower()

    product_uoms_map = {}
    product_last_prices = {}
    for p in products:
        product_last_prices[p.id] = float(p.last_purchase_price) if p.last_purchase_price else 0.0
        base_name = getattr(p, 'pos_unit_name', None) or p.get_unit_display() or 'وحدة'
        uoms = [{'id': '', 'name': base_name, 'factor': 1.0}]
        seen_normalized = {normalize_name(base_name)}
        for puom in p.uoms.all():
            norm_name = normalize_name(puom.uom.name)
            if norm_name not in seen_normalized:
                factor = float(puom.conversion_factor) if getattr(puom, 'conversion_factor', None) else 1.0
                uoms.append({'id': puom.id, 'name': puom.uom.name, 'factor': factor})
                seen_normalized.add(norm_name)
        product_uoms_map[p.id] = uoms
    product_uoms_json = _safe_json_for_script(product_uoms_map)
    product_last_prices_json = _safe_json_for_script(product_last_prices)

    from django.db.models import Sum, Count
    from apps.tenant.inventory.models import InventoryBatch, SerialItem
    stock_by_product = dict(
        InventoryBatch.objects.filter(quantity_remaining__gt=0)
        .values('product_id')
        .annotate(total=Sum('quantity_remaining'))
        .values_list('product_id', 'total')
    )
    serial_stock = dict(
        SerialItem.objects.filter(is_sold=False)
        .values('product_id')
        .annotate(total=Count('id'))
        .values_list('product_id', 'total')
    )

    product_stock_info = {}
    for p in products:
        cur_stock = float(serial_stock.get(p.id, 0)) if p.product_type == 'SERIALIZED' else float(stock_by_product.get(p.id, 0))
        product_stock_info[p.id] = {
            'current_stock': cur_stock,
            'max_stock': float(p.max_stock_level) if p.max_stock_level else 0.0,
            'min_stock': float(p.min_stock_level) if p.min_stock_level else 0.0,
        }
    product_stock_info_json = _safe_json_for_script(product_stock_info)

    is_admin = is_admin_user(request.user)
    # Working branch (header branch for admins, own branch for users); admins may
    # still pick another branch in the form, pre-selected to the header branch.
    user_branch = get_working_branch(request)

    branches = Branch.objects.filter(is_active=True) if is_admin else []
    if is_admin and getattr(request, 'branch_locked_by_shift', False):
        branches = Branch.objects.filter(id=request.branch.id)  # open shift: branch locked

    
    from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
    treasuries = Treasury.objects.all()
    bank_accounts = BankAccount.objects.all()
    ewallets = EWallet.objects.all()
    expiry_tracking_enabled = bool(setting_value('enable_expiry_tracking', False))

    if request.method == 'POST':
        from django.db import transaction
        from django.contrib import messages
        try:
            invoice_type = request.POST.get('invoice_type')
            # This form only creates purchase documents; sales must go through
            # POS (complete_sale) where discount/price limits are enforced.
            if invoice_type not in (Invoice.PURCHASE, Invoice.RETURN_PURCHASE):
                raise ValueError('نوع الفاتورة غير صالح لهذه الشاشة.')
            supplier_id = request.POST.get('supplier_id')
            warehouse_id = request.POST.get('warehouse_id')
            payment_type = request.POST.get('payment_type')
            treasury_id = request.POST.get('treasury_id') or None
            bank_account_id = request.POST.get('bank_account_id') or None
            ewallet_id = request.POST.get('ewallet_id') or None

            # Reset irrelevant payment source based on payment_type. Missing defaults are
            # filled once the invoice branch is known (get_pos_defaults(branch)).
            if payment_type == 'CASH':
                bank_account_id = None
                ewallet_id = None
            elif payment_type in ['BANK_TRANSFER', 'CARD']:
                treasury_id = None
                ewallet_id = None
            elif payment_type == 'EWALLET':
                treasury_id = None
                bank_account_id = None
            elif payment_type == 'CREDIT':
                treasury_id = None
                bank_account_id = None
                ewallet_id = None
            
            product_ids = request.POST.getlist('product_id[]')
            quantities = request.POST.getlist('quantity[]')
            prices = request.POST.getlist('price[]')
            uom_ids = request.POST.getlist('uom_id[]')
            expiry_dates = request.POST.getlist('expiry_date[]') if expiry_tracking_enabled else []
            if len(expiry_dates) != len(product_ids):
                expiry_dates = [''] * len(product_ids)

            # Ensure uom_ids list matches length of product_ids (fill with None if empty)
            if not uom_ids or len(uom_ids) != len(product_ids):
                uom_ids = [None] * len(product_ids)

            valid_lines_count = sum(
                1 for pid, qty, prc in zip(product_ids, quantities, prices)
                if pid and qty and prc and Decimal(str(qty or '0')) > 0
            )
            if valid_lines_count == 0:
                raise ValueError("يجب إضافة بند واحد على الأقل في الفاتورة بكمية وسعر صحيحين.")

            discount_percentage = Decimal(request.POST.get('discount_percentage') or 0)
            
            vat_str = request.POST.get('vat_percentage', '')
            wht_str = request.POST.get('wht_percentage', '')
            vat_percentage = Decimal(vat_str) if vat_str != '' else None
            wht_percentage = Decimal(wht_str) if wht_str != '' else None
            e_invoice_number = request.POST.get('e_invoice_number', '')
            
            with transaction.atomic():
                supplier = Partner.objects.get(id=supplier_id)
                warehouse = Warehouse.objects.get(id=warehouse_id)
                
                if payment_type == 'CREDIT':
                    if not supplier or 'مورد نقدي' in supplier.name or supplier.name.strip() in ['مورد نقدي', 'نقدي', 'طياري', 'عميل نقدي']:
                        raise ValueError('لا يمكن إنشاء فاتورة مشتريات (آجلة) لمورد نقدي. يرجى اختيار مورد آخر مخصص للآجل.')

                if is_admin:
                    branch_id = request.POST.get('branch_id')
                    branch = Branch.objects.filter(id=branch_id, is_active=True).first() if branch_id else user_branch
                else:
                    branch = user_branch
                lock_error = shift_branch_lock_error(request, branch)
                if lock_error:
                    raise ValueError(lock_error)
                if branch and warehouse and warehouse.branch_id and warehouse.branch_id != branch.id:
                    raise ValueError('المستودع المختار لا يتبع الفرع المحدد.')
                    
                if not branch:
                    raise ValueError("لا يوجد فرع مرتبط بهذا المستخدم. يرجى مراجعة مدير النظام.")

                # Default payment accounts of the invoice branch (then global defaults).
                branch_defaults = get_pos_defaults(branch)
                if payment_type == 'CASH' and not treasury_id:
                    def_t = (branch_defaults['treasury'] or Treasury.objects.filter(branch=branch).first()
                             or Treasury.objects.first())
                    treasury_id = def_t.id if def_t else None
                elif payment_type in ['BANK_TRANSFER', 'CARD'] and not bank_account_id:
                    def_b = branch_defaults['bank_account'] or BankAccount.objects.first()
                    bank_account_id = def_b.id if def_b else None
                elif payment_type == 'EWALLET' and not ewallet_id:
                    def_w = branch_defaults['ewallet'] or EWallet.objects.first()
                    ewallet_id = def_w.id if def_w else None

                invoice = Invoice.objects.create(
                    invoice_type=invoice_type,
                    partner=supplier,
                    warehouse=warehouse,
                    branch=branch,
                    date=timezone.now().date(),
                    payment_type=payment_type,
                    treasury_id=treasury_id,
                    bank_account_id=bank_account_id,
                    ewallet_id=ewallet_id,
                    status=Invoice.DRAFT,
                    cashier=request.user,
                    discount_percentage=discount_percentage,
                    vat_percentage=vat_percentage if vat_percentage is not None else Decimal('0'),
                    wht_percentage=wht_percentage if wht_percentage is not None else Decimal('0'),
                    e_invoice_number=e_invoice_number if e_invoice_number else None,
                )
                
                for i, (pid, qty, price, uom_id) in enumerate(zip(product_ids, quantities, prices, uom_ids)):
                    if pid and qty and price:
                        product = Product.objects.get(id=pid)
                        line_expiry = None
                        raw_expiry = (expiry_dates[i] or '').strip()
                        if raw_expiry and invoice_type == Invoice.PURCHASE:
                            try:
                                line_expiry = parse_date(raw_expiry)
                            except ValueError:
                                line_expiry = None
                            if line_expiry is None:
                                raise ValueError(f'تاريخ الصلاحية غير صالح للصنف {product.name}.')
                        
                        # Apply taxes: if explicitly provided on invoice use it, else fallback to product defaults
                        if vat_percentage is not None:
                            tax_r = vat_percentage
                        else:
                            tax_r = product.tax_rate or Decimal('0')

                        if wht_percentage is not None:
                            wht_r = wht_percentage
                        else:
                            if getattr(supplier, 'is_withholding_tax_subject', False):
                                wht_r = product.withholding_tax_rate or Decimal('0')
                            else:
                                wht_r = Decimal('0')
                        
                        uom_id_val = uom_id if uom_id and uom_id != '' else None
                        
                        # Handle Serial/IMEI creation
                        if product.product_type == 'SERIALIZED':
                            from apps.tenant.inventory.models import SerialItem
                            # Find all serials posted for this specific line
                            imei_1_list = request.POST.getlist(f'imei_1_line_{i}[]')
                            imei_2_list = request.POST.getlist(f'imei_2_line_{i}[]')
                            serial_list = request.POST.getlist(f'serial_line_{i}[]')
                            
                            num_items = int(Decimal(qty))
                            if not imei_1_list: imei_1_list = [''] * num_items
                            if not imei_2_list: imei_2_list = [''] * num_items
                            if not serial_list: serial_list = [''] * num_items
                            
                            for j in range(num_items):
                                i1 = imei_1_list[j] if j < len(imei_1_list) else ''
                                i2 = imei_2_list[j] if j < len(imei_2_list) else ''
                                sn = serial_list[j] if j < len(serial_list) else ''
                                
                                if product.has_imei and not i1:
                                    raise ValueError(f"يرجى إدخال IMEI 1 للمنتج {product.name}")
                                if product.has_serial and not sn:
                                    raise ValueError(f"يرجى إدخال السيريال للمنتج {product.name}")
                                    
                                serial_item = SerialItem.objects.create(
                                    product=product,
                                    serial_number=sn if sn else None,
                                    imei_1=i1 if i1 else None,
                                    imei_2=i2 if i2 else None,
                                    condition=SerialItem.CONDITION_NEW,
                                    warehouse=warehouse,
                                    is_sold=False,
                                    actual_cost=Decimal(price),
                                )
                                
                                InvoiceLine.objects.create(
                                    invoice=invoice,
                                    product=product,
                                    serial_item=serial_item,
                                    quantity=Decimal('1'),
                                    unit_price=Decimal(price),
                                    tax_rate=tax_r,
                                    wht_rate=wht_r,
                                    discount_pct=invoice.discount_percentage,
                                    uom_id=uom_id_val,
                                )
                        else:
                            InvoiceLine.objects.create(
                                invoice=invoice,
                                product=product,
                                quantity=Decimal(qty),
                                unit_price=Decimal(price),
                                tax_rate=tax_r,
                                wht_rate=wht_r,
                                discount_pct=invoice.discount_percentage,
                                uom_id=uom_id_val,
                                expiry_date=line_expiry,
                            )

                invoice.recalculate_totals()
                
                # Sync paid breakdown fields
                if invoice.payment_type == Invoice.CASH:
                    invoice.paid_cash = invoice.total_amount
                elif invoice.payment_type == Invoice.CARD:
                    invoice.paid_card = invoice.total_amount
                elif invoice.payment_type == Invoice.BANK_TRANSFER:
                    invoice.paid_bank = invoice.total_amount
                elif invoice.payment_type == Invoice.EWALLET:
                    invoice.paid_ewallet = invoice.total_amount
                elif invoice.payment_type == Invoice.CREDIT:
                    invoice.paid_credit = invoice.total_amount
                invoice.save(update_fields=['paid_cash', 'paid_card', 'paid_bank', 'paid_ewallet', 'paid_credit'])
                
                from apps.tenant.services.invoice_service import confirm_invoice
                confirm_invoice(invoice.id)
                
            messages.success(request, 'تم حفظ وترحيل الفاتورة بنجاح!')
            return redirect('invoicing:purchase_invoice_list')
        except Exception as e:
            import traceback
            traceback.print_exc()
            messages.error(request, f'حدث خطأ: {str(e)}')
            
            import json
            posted_lines = []
            posted_uoms = request.POST.getlist('uom_id[]')
            posted_expiry = request.POST.getlist('expiry_date[]')
            for i, (pid, qty, prc) in enumerate(zip(request.POST.getlist('product_id[]'), request.POST.getlist('quantity[]'), request.POST.getlist('price[]'))):
                u_id = posted_uoms[i] if i < len(posted_uoms) else ''
                exp = posted_expiry[i] if i < len(posted_expiry) else ''
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', exp or ''):
                    exp = ''  # only echo back well-formed dates into the re-rendered form
                posted_lines.append({'product_id': pid, 'quantity': qty, 'price': prc, 'uom_id': u_id, 'expiry_date': exp})
            
            # Re-pass the posted data back to context to prevent form clearing
            context = {
                'suppliers': suppliers,
                'warehouses': warehouses,
                'products': products,
                'product_uoms_json': product_uoms_json,
                'product_last_prices_json': product_last_prices_json,
                'product_stock_info_json': product_stock_info_json,
                'branches': branches,
                'treasuries': treasuries,
                'bank_accounts': bank_accounts,
                'ewallets': ewallets,
                'title': 'إنشاء فاتورة مشتريات',
                'expiry_tracking_enabled': expiry_tracking_enabled,
                'posted_data': request.POST,
                'posted_lines_json': _safe_json_for_script(posted_lines),
            }
            return render(request, 'invoicing/purchase_invoice.html', context)
            
    context = {
        'suppliers': suppliers,
        'warehouses': warehouses,
        'products': products,
        'product_uoms_json': product_uoms_json,
        'product_last_prices_json': product_last_prices_json,
        'product_stock_info_json': product_stock_info_json,
        'branches': branches,
        'treasuries': treasuries,
        'bank_accounts': bank_accounts,
        'ewallets': ewallets,
        'is_admin': is_admin,
        'expiry_tracking_enabled': expiry_tracking_enabled,
        'title': 'فاتورة مشتريات'
    }
    return render(request, 'invoicing/purchase_invoice.html', context)

@login_required
@custom_permission_required('invoicing.view_invoice', redirect_url='core:main_screen')
def _filter_invoices(request, base_types):
    from django.db.models import Q
    from django.core.paginator import Paginator
    
    invoices = _scope_by_allowed_branches(Invoice.objects.filter(
        invoice_type__in=base_types
    ), request).select_related(
        'partner', 'branch', 'treasury', 'bank_account', 'ewallet'
    ).prefetch_related('lines').order_by('-date', '-created_at')
    
    # 1. Search Query (invoice number, partner name, notes)
    q = request.GET.get('q', '').strip()
    if q:
        invoices = invoices.filter(
            Q(invoice_number__icontains=q) |
            Q(partner__name__icontains=q) |
            Q(notes__icontains=q)
        )
        
    # 2. Payment Type
    payment_type = request.GET.get('payment_type', '').strip()
    if payment_type and payment_type != 'ALL':
        invoices = invoices.filter(payment_type=payment_type)
        
    # 3. Invoice Subtype (Sale vs Return)
    type_filter = request.GET.get('type_filter', '').strip()
    if type_filter and type_filter in base_types:
        invoices = invoices.filter(invoice_type=type_filter)
        
    # 4. Status Filter
    status_filter = request.GET.get('status_filter', '').strip()
    if status_filter and status_filter != 'ALL':
        if status_filter == 'RETURNED':
            invoices = invoices.filter(status=Invoice.POSTED, lines__returned_quantity__gt=0).distinct()
        elif status_filter in [Invoice.POSTED, Invoice.DRAFT, Invoice.CANCELLED]:
            invoices = invoices.filter(status=status_filter)
            
    # 5. Date Range
    start_date = request.GET.get('start_date', '').strip()
    if start_date:
        invoices = invoices.filter(date__gte=start_date)
        
    end_date = request.GET.get('end_date', '').strip()
    if end_date:
        invoices = invoices.filter(date__lte=end_date)
        
    # 6. Branch
    branch_id = request.GET.get('branch_id', '').strip()
    if branch_id and branch_id != 'ALL':
        invoices = invoices.filter(branch_id=branch_id)
        
    # 7. Pagination
    per_page_str = request.GET.get('per_page', '20')
    try:
        per_page = int(per_page_str)
        if per_page not in [10, 20, 50, 100]:
            per_page = 20
    except ValueError:
        per_page = 20
        
    paginator = Paginator(invoices, per_page)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    params = request.GET.copy()
    params.pop('page', None)
    query_string = params.urlencode()
    
    return {
        'invoices': page_obj,
        'page_obj': page_obj,
        'total_count': paginator.count,
        'search_query': q,
        'selected_payment_type': payment_type,
        'selected_type_filter': type_filter,
        'selected_status_filter': status_filter,
        'selected_start_date': start_date,
        'selected_end_date': end_date,
        'selected_branch_id': branch_id,
        'per_page': per_page,
        'query_string': query_string,
        'branches': allowed_branches(request),
    }

@login_required
@custom_permission_required('invoicing.view_invoice', redirect_url='core:main_screen')
def sales_invoice_list(request):
    ctx = _filter_invoices(request, [Invoice.SALE, Invoice.RETURN_SALE])
    ctx['title'] = 'فواتير المبيعات'
    ctx['list_type'] = 'sales'
    return render(request, 'invoicing/invoice_list.html', ctx)

@login_required
@custom_permission_required('invoicing.view_invoice', redirect_url='core:main_screen')
def purchase_invoice_list(request):
    ctx = _filter_invoices(request, [Invoice.PURCHASE, Invoice.RETURN_PURCHASE])
    ctx['title'] = 'فواتير المشتريات'
    ctx['list_type'] = 'purchases'
    return render(request, 'invoicing/invoice_list.html', ctx)

@login_required
@custom_permission_required('invoicing.view_invoice', redirect_url='core:main_screen')
def invoice_detail(request, invoice_id):
    from django.shortcuts import get_object_or_404
    from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
    from apps.tenant.partners.models import Partner

    invoice = get_object_or_404(_scope_by_allowed_branches(Invoice.objects.all(), request), id=invoice_id)
    lines = invoice.lines.select_related('product', 'serial_item', 'uom__uom').all()
    
    is_fully_returned = False
    has_returns = False
    if lines.exists():
        is_fully_returned = all(line.quantity <= line.returned_quantity for line in lines)
        has_returns = any(line.returned_quantity > 0 for line in lines)

    has_returns = has_returns or invoice.return_invoices.exclude(status=Invoice.CANCELLED).exists()
        
    for line in lines:
        line.available_return_qty = line.quantity - line.returned_quantity

    if invoice.invoice_type in [Invoice.SALE, Invoice.RETURN_SALE]:
        partners = Partner.objects.filter(partner_type__in=[Partner.CUSTOMER, Partner.BOTH])
    else:
        partners = Partner.objects.filter(partner_type__in=[Partner.SUPPLIER, Partner.BOTH])
    
    context = {
        'invoice': invoice,
        'lines': lines,
        'is_fully_returned': is_fully_returned,
        'has_returns': has_returns,
        'treasuries': Treasury.objects.all(),
        'bank_accounts': BankAccount.objects.all(),
        'ewallets': EWallet.objects.all(),
        'partners': partners,
        'title': f'تفاصيل فاتورة رقم {invoice.invoice_number}'
    }
    return render(request, 'invoicing/invoice_detail.html', context)

@login_required
@custom_permission_required('invoicing.change_invoice', redirect_url='core:main_screen')
def confirm_invoice_view(request, invoice_id):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
        
    if not _scope_by_allowed_branches(Invoice.objects.filter(id=invoice_id), request).exists():
        return JsonResponse({'success': False, 'error': 'الفاتورة غير موجودة أو خارج نطاق فرعك.'}, status=404)

    from apps.tenant.services.invoice_service import confirm_invoice
    try:
        invoice = confirm_invoice(invoice_id)
        return JsonResponse({'success': True, 'invoice_number': invoice.invoice_number})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
@custom_permission_required('invoicing.change_invoice', redirect_url='core:main_screen')
def update_invoice_payment_method_view(request, invoice_id):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    if not _scope_by_allowed_branches(Invoice.objects.filter(id=invoice_id), request).exists():
        return JsonResponse({'success': False, 'error': 'الفاتورة غير موجودة أو خارج نطاق فرعك.'}, status=404)

    from apps.tenant.services.invoice_service import update_invoice_payment_method
    try:
        payment_type = request.POST.get('payment_type', 'CASH')
        partner_id = request.POST.get('partner') or None
        treasury_id = request.POST.get('treasury') or None
        bank_account_id = request.POST.get('bank_account') or None
        ewallet_id = request.POST.get('ewallet') or None
        
        paid_cash = Decimal(request.POST.get('paid_cash', '0') or '0')
        paid_card = Decimal(request.POST.get('paid_card', '0') or '0')
        paid_ewallet = Decimal(request.POST.get('paid_ewallet', '0') or '0')
        paid_bank = Decimal(request.POST.get('paid_bank', '0') or '0')

        invoice = update_invoice_payment_method(
            invoice_id=invoice_id,
            payment_type=payment_type,
            partner_id=int(partner_id) if partner_id else None,
            treasury_id=int(treasury_id) if treasury_id else None,
            bank_account_id=int(bank_account_id) if bank_account_id else None,
            ewallet_id=int(ewallet_id) if ewallet_id else None,
            paid_cash=paid_cash,
            paid_card=paid_card,
            paid_ewallet=paid_ewallet,
            paid_bank=paid_bank,
            user=request.user,
        )
        return JsonResponse({
            'success': True,
            'message': 'تم تحديث طريقة السداد والقيود المحاسبية المربوطة بنجاح.'
        })
    except Exception as e:
        logger.exception("Error updating invoice payment method")
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@custom_permission_required('invoicing.change_invoice', redirect_url='core:main_screen')
def cancel_invoice_view(request, invoice_id):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    if not _scope_by_allowed_branches(Invoice.objects.filter(id=invoice_id), request).exists():
        return JsonResponse({'success': False, 'error': 'الفاتورة غير موجودة أو خارج نطاق فرعك.'}, status=404)

    from apps.tenant.services.invoice_service import cancel_invoice
    try:
        invoice = cancel_invoice(invoice_id, user=request.user)
        return JsonResponse({
            'success': True,
            'message': f'تم إلغاء الفاتورة رقم {invoice.invoice_number} وعكس كافة الآثار المخزنية والمحاسبية بنجاح.'
        })
    except Exception as e:
        logger.exception("Error cancelling invoice")
        return JsonResponse({'success': False, 'error': str(e)})



@login_required
def get_product_by_barcode(request):
    try:
        barcode = request.GET.get('barcode', '').strip()
        warehouse_id = request.GET.get('warehouse_id')
        if not barcode:
            return JsonResponse({'found': False})
            
        product = None
        selected_uom_id = 'base'
        
        # 1. Direct barcode match
        product = Product.objects.prefetch_related('uoms__uom').filter(barcode=barcode, is_active=True).first()
        
        # 2. UoM barcode match
        if not product:
            from apps.tenant.inventory.models import ProductUoM
            puom = ProductUoM.objects.select_related('product', 'uom').filter(barcode=barcode, product__is_active=True).first()
            if puom:
                product = puom.product
                selected_uom_id = puom.id

        # 3. If barcode is numeric, fallback to matching by product ID
        if not product and barcode.isdigit():
            try:
                product = Product.objects.prefetch_related('uoms__uom').filter(id=int(barcode), is_active=True).first()
            except (ValueError, TypeError):
                pass

        if not product:
            return JsonResponse({'found': False})

        uoms = [{'id': 'base', 'name': product.get_unit_display(), 'factor': 1.0}]
        for puom in product.uoms.all():
            uoms.append({
                'id': puom.id,
                'unit_id': puom.uom_id,
                'name': puom.uom.name,
                'factor': float(puom.conversion_factor or 1.0)
            })
            
        current_branch = get_branch_scope(request)
        current_warehouse = None
        if warehouse_id and str(warehouse_id).isdigit():
            current_warehouse = _scope_by_allowed_branches(
                Warehouse.objects.filter(id=int(warehouse_id), is_active=True), request
            ).first()

        # Multi-Tier Pricing (Customer Price List -> Sector Price List -> Default Product Price)
        partner_id = request.GET.get('partner_id')
        qty_str = request.GET.get('quantity', request.GET.get('qty', '1'))
        partner = None
        if partner_id:
            try:
                partner = Partner.objects.select_related('sector').get(id=partner_id)
            except (Partner.DoesNotExist, ValueError):
                partner = None

        try:
            req_qty = Decimal(str(qty_str or 1))
        except (InvalidOperation, ValueError):
            req_qty = Decimal('1')

        from apps.tenant.pricing.services import PricingEngineService
        selected_puom = None
        if selected_uom_id != 'base':
            selected_puom = next((u for u in product.uoms.all() if u.id == selected_uom_id), None)
        pricing_info = PricingEngineService.get_effective_price(
            product=product,
            partner=partner,
            branch=current_branch,
            uom=selected_puom,
            quantity=req_qty,
            price_per_uom=True,
        )
        unit_price = Decimal(str(pricing_info.get('unit_price', pricing_info['price']) or 0))
        uom_factor = Decimal(str(pricing_info.get('uom_factor', 1) or 1))
        # `sale_price` stays per BASE unit (POS JS multiplies by the uom factor);
        # `unit_price` is the price of one selected uom.
        base_unit_price = Decimal(str(pricing_info.get('base_unit_price', unit_price / uom_factor)))
            
        return JsonResponse({
            'found': True,
            'product': {
                'id': product.id,
                'name': product.name,
                'barcode': product.barcode or '',
                'product_type': product.product_type,
                'sale_price': str(base_unit_price),
                'unit_price': str(unit_price),
                'uom_factor': str(uom_factor),
                'original_sale_price': str(pricing_info['original_price'] / uom_factor if pricing_info.get('price_is_per_uom') else pricing_info['original_price']),
                'pricing_tier': pricing_info['tier'],
                'pricing_tier_display': pricing_info['tier_display'],
                'pricing_badge_text': pricing_info['badge_text'],
                'pricing_badge_color': pricing_info['badge_color'],
                'tax_rate': str(product.tax_rate),
                'available_stock': str(product.get_stock(warehouse=current_warehouse) if current_warehouse else Decimal('0')),
                'allow_negative_stock': product.allow_negative_stock,
                'uoms': uoms,
                'selected_uom_id': selected_uom_id
            }
        })
    except Exception as e:
        return JsonResponse({'found': False, 'error': str(e)})

@login_required
def add_to_cart(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        # Logic to add to session cart
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})

@login_required
def get_available_serials(request):
    """API: Returns all unsold SerialItems for a given product & warehouse."""
    from apps.tenant.inventory.models import SerialItem
    product_id = request.GET.get('product_id')
    warehouse_id = request.GET.get('warehouse_id')
    if not product_id:
        return JsonResponse({'serials': []})
    qs = SerialItem.objects.filter(product_id=product_id, is_sold=False)
    if warehouse_id:
        qs = qs.filter(warehouse_id=warehouse_id)
    serials = [
        {
            'id': s.id,
            'serial_number': s.serial_number,
            'imei_1': s.imei_1,
            'imei_2': s.imei_2,
            'display_label': s.imei_1 or s.serial_number or f"رقم {s.id}",
            'condition': s.get_condition_display(),
            'storage': s.storage or '',
            'ram': s.ram or '',
            'actual_cost': str(s.actual_cost),
        }
        for s in qs
    ]
    return JsonResponse({'serials': serials})

class _SaleAbort(Exception):
    """Raised inside complete_sale's atomic block so every partial write is rolled back."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _to_decimal(value, field_label, default='0'):
    from decimal import InvalidOperation as _InvalidOperation
    try:
        if value is None or value == '':
            value = default
        d = Decimal(str(value))
    except (_InvalidOperation, ValueError, TypeError):
        raise _SaleAbort(f'قيمة غير صالحة في الحقل ({field_label}).')
    if not d.is_finite():
        raise _SaleAbort(f'قيمة غير صالحة في الحقل ({field_label}).')
    return d


def _pos_unit_price(product, puom, partner, branch, quantity):
    """
    Server-side price for one unit of `puom` (or of the base unit when puom is None),
    using the same PricingEngineService call as the barcode API.
    Returns (unit_price, min_unit_price).
    """
    from apps.tenant.pricing.services import PricingEngineService
    factor = Decimal(str(puom.conversion_factor or 1)) if puom else Decimal('1')
    # The engine resolves customer -> sector -> default tiers (highest min_quantity <= qty
    # for this uom) and returns the price of ONE requested uom in `unit_price`.
    info = PricingEngineService.get_effective_price(
        product=product, partner=partner, branch=branch, uom=puom, quantity=quantity,
        price_per_uom=True,
    )
    price = Decimal(str(info.get('unit_price', info['price']) or 0))

    min_price = product.min_sale_price or Decimal('0')
    if branch:
        pb = product.branch_prices.filter(branch=branch).first()
        if pb and pb.min_sale_price is not None:
            min_price = pb.min_sale_price
    min_price = Decimal(str(min_price or 0)) * factor
    return price.quantize(Decimal('0.0001')), min_price.quantize(Decimal('0.0001'))


GENERIC_CASH_CUSTOMER_NAMES = ('عميل نقدي', 'نقدي', 'طياري')


def _is_generic_cash_customer(partner):
    """True when there is no real customer: no partner, or the generic walk-in "عميل نقدي"."""
    if partner is None:
        return True
    name = (partner.name or '').strip()
    return not name or name in GENERIC_CASH_CUSTOMER_NAMES or 'عميل نقدي' in name


@login_required
@custom_permission_required('invoicing.add_invoice', redirect_url='core:main_screen')
def complete_sale(request):
    """
    POS checkout. Creates and posts a SALE invoice.

    Security: prices, taxes and totals are computed server-side; the client may only
    override the unit price for open-price / editable products and never below the
    minimum sale price. Payment breakdown is reconciled against the server total.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False})

    from django.db import transaction
    from apps.tenant.services.invoice_service import confirm_invoice
    from apps.tenant.inventory.models import ProductUoM, SerialItem

    try:
        try:
            data = json.loads(request.body)
        except (ValueError, TypeError):
            raise _SaleAbort('بيانات الطلب غير صالحة.')
        if not isinstance(data, dict):
            raise _SaleAbort('بيانات الطلب غير صالحة.')

        cart = data.get('cart', []) or []
        payment_type = data.get('payment_type', 'CASH')
        warehouse_id = data.get('warehouse_id')
        branch_id = data.get('branch_id')
        treasury_id = data.get('treasury_id')
        ewallet_id = data.get('ewallet_id')
        bank_account_id = data.get('bank_account_id')
        partner_id = data.get('partner_id')

        # This endpoint ONLY creates SALE invoices. Returns must go through the
        # return screens (process_invoice_return / create_standalone_sales_return).
        invoice_type = data.get('invoice_type', 'SALE') or 'SALE'
        if invoice_type != Invoice.SALE:
            raise _SaleAbort(
                'لا يمكن تسجيل مرتجع من شاشة البيع مباشرة. يرجى استخدام شاشة مرتجع المبيعات '
                '(من الفاتورة الأصلية أو مرتجع بدون فاتورة).'
            )

        valid_payment_types = {c[0] for c in Invoice.PAYMENT_TYPE_CHOICES}
        if payment_type not in valid_payment_types:
            raise _SaleAbort('طريقة دفع غير معروفة.')

        if not cart or not isinstance(cart, list):
            raise _SaleAbort('السلة فارغة. يرجى إضافة منتجات أولاً.')

        if not warehouse_id:
            raise _SaleAbort('يجب اختيار مستودع لإتمام عملية البيع', status=200)

        is_admin = is_admin_user(request.user)
        can_override_discount = is_admin or request.user.has_perm('invoicing.change_invoice')
        setting = SystemSetting.get_settings()
        allow_price_edit_setting = bool(getattr(setting, 'allow_pos_price_modification', False))
        # Allowed deviation (±%) from the server price when a non-admin edits a price
        # (0 = no limit). Open-price / service products are exempt.
        try:
            price_margin_pct = Decimal(str(getattr(setting, 'pos_price_margin_percent', 0) or 0))
        except (InvalidOperation, ValueError, TypeError):
            price_margin_pct = Decimal('0')
        try:
            large_amount_threshold = Decimal(str(getattr(setting, 'large_amount_requires_customer', 0) or 0))
        except (InvalidOperation, ValueError, TypeError):
            large_amount_threshold = Decimal('0')
        # Discount cap for non-admins (admins are exempt). Applies to the
        # header discount and to each line's total effective discount (the POS
        # sends line discount combined with the invoice discount).
        max_discount_pct = _max_discount_pct_for(request.user, setting)
        # Float rounding from the browser (values sent with 4 decimals).
        discount_tolerance = DISCOUNT_CAP_TOLERANCE

        with transaction.atomic():
            branch = Branch.objects.filter(id=branch_id).first() if branch_id else None
            if not branch:
                raise _SaleAbort('الفرع المحدد غير موجود.')
            user_branch = get_working_branch(request)
            if not is_admin and is_branch_unassigned(request):
                raise _SaleAbort(BRANCH_UNASSIGNED_MESSAGE, status=403)
            if not is_admin and user_branch and branch.id != user_branch.id:
                raise _SaleAbort('لا يمكنك البيع على فرع غير الفرع المرتبط بحسابك.', status=403)

            warehouse = Warehouse.objects.filter(id=warehouse_id, is_active=True).first()
            if not warehouse:
                raise _SaleAbort('المستودع المحدد غير صالح أو غير نشط', status=200)
            if warehouse.branch_id and warehouse.branch_id != branch.id:
                raise _SaleAbort('المستودع المختار لا يتبع الفرع الحالي', status=200)

            if partner_id:
                partner = Partner.objects.filter(id=partner_id).first()
                if not partner:
                    raise _SaleAbort('العميل المحدد غير موجود.')
            else:
                partner, _ = Partner.objects.get_or_create(
                    name="عميل نقدي",
                    defaults={'partner_type': 'CUSTOMER', 'is_active': True}
                )

            open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
            if not open_session:
                raise _SaleAbort('يجب فتح وردية أولاً قبل الدخول لنقطة البيع أو إجراء أي عملية بيع.')
            # A sale always belongs to the open shift's branch (cash → that branch's treasury).
            if open_session.branch_id and branch.id != open_session.branch_id:
                raise _SaleAbort('الفرع المرسل لا يطابق فرع الوردية المفتوحة. أعد تحميل نقطة البيع.', status=409)

            if not treasury_id and open_session.treasury_id:
                treasury_id = open_session.treasury_id
            # Payment defaults of the sale's branch (= the open shift's branch).
            branch_defaults = get_pos_defaults(branch)
            if not treasury_id and branch_defaults['treasury'] is not None:
                treasury_id = branch_defaults['treasury'].id

            payments = data.get('payments', {}) or {}
            if not isinstance(payments, dict):
                payments = {}

            # Client-declared split amounts (validated against the server total later)
            if payment_type == 'SPLIT':
                cl_cash = _to_decimal(payments.get('cash', 0), 'نقدي')
                cl_card = _to_decimal(payments.get('card', 0), 'بطاقة')
                cl_ewallet = _to_decimal(payments.get('ewallet', 0), 'محفظة')
                cl_bank = _to_decimal(payments.get('bank', 0), 'تحويل بنكي')
                cl_credit = _to_decimal(payments.get('credit', 0), 'آجل')
                if min(cl_cash, cl_card, cl_ewallet, cl_bank, cl_credit) < 0:
                    raise _SaleAbort('مبالغ الدفع لا يمكن أن تكون سالبة.')
            else:
                cl_cash = cl_card = cl_ewallet = cl_bank = cl_credit = Decimal('0')

            has_credit = payment_type == 'CREDIT' or cl_credit > 0
            if has_credit:
                if not partner_id:
                    raise _SaleAbort('يجب اختيار عميل لتسجيل بيع آجل.')
                if partner and ('عميل نقدي' in partner.name or partner.name.strip() in ['عميل نقدي', 'نقدي', 'طياري', 'مورد نقدي']):
                    raise _SaleAbort('لا يمكن تسجيل بيع آجل لـ (عميل نقدي). يرجى اختيار أو إضافة حساب العميل أولاً.')

            visa_bank_account_id = payments.get('visa_bank_id') if payment_type == 'SPLIT' else None
            insta_bank_account_id = payments.get('insta_bank_id') if payment_type == 'SPLIT' else None

            if payment_type == 'CARD' or cl_card > 0:
                if visa_bank_account_id:
                    bank_account_id = visa_bank_account_id
                elif not bank_account_id:
                    default_visa = branch_defaults['bank_account']
                    if default_visa:
                        bank_account_id = default_visa.id
                if not bank_account_id:
                    raise _SaleAbort('يجب اختيار ماكينة الفيزا من القائمة العلوية أو تحديدها في الإعدادات.')

            if payment_type == 'BANK_TRANSFER' or cl_bank > 0:
                if not insta_bank_account_id:
                    default_insta = branch_defaults['instapay_account'] or branch_defaults['bank_account']
                    if default_insta:
                        insta_bank_account_id = default_insta.id
                if not insta_bank_account_id and payment_type == 'BANK_TRANSFER':
                    insta_bank_account_id = bank_account_id
                if not insta_bank_account_id and cl_bank > 0:
                    raise _SaleAbort('يجب اختيار حساب إنستاباي / التحويل البنكي من القائمة العلوية أو تحديده في الإعدادات.')

            notes = str(data.get('notes', '') or '')

            discount_percentage = _to_decimal(data.get('discount_percentage', 0), 'نسبة الخصم')
            if discount_percentage < 0 or discount_percentage > 100:
                raise _SaleAbort('نسبة الخصم يجب أن تكون بين 0 و 100.')
            if max_discount_pct is not None and discount_percentage > max_discount_pct + discount_tolerance:
                raise _SaleAbort(
                    f'نسبة خصم الفاتورة ({discount_percentage:.2f}%) تتجاوز الحد الأقصى المسموح ({max_discount_pct:.2f}%). '
                    'يتطلب ذلك صلاحية مدير.', status=403
                )

            # Totals are recomputed from lines by recalculate_totals(); start from zero.
            invoice = Invoice.objects.create(
                invoice_type=Invoice.SALE,
                branch=branch,
                warehouse=warehouse,
                partner=partner,
                date=timezone.now().date(),
                payment_type=payment_type,
                treasury_id=treasury_id if treasury_id else None,
                ewallet_id=ewallet_id if ewallet_id else None,
                bank_account_id=bank_account_id if bank_account_id else None,
                insta_bank_account_id=insta_bank_account_id if insta_bank_account_id else None,
                status=Invoice.DRAFT,
                cashier=request.user,
                pos_session=open_session,
                discount_percentage=discount_percentage,
                notes=notes,
            )

            used_serial_ids = set()
            line_count = 0
            for item in cart:
                if not isinstance(item, dict):
                    continue
                pid = item.get('product_id')
                if not pid:
                    continue
                try:
                    product = Product.objects.get(id=int(pid))
                except (Product.DoesNotExist, ValueError, TypeError):
                    raise _SaleAbort(
                        f'الصنف برقم ({pid}) غير موجود في قاعدة البيانات أو تم حذفه. يرجى مراجعة الأصناف بالسلة.',
                        status=200,
                    )
                if not product.is_active:
                    raise _SaleAbort(f'الصنف "{product.name}" غير نشط.')

                quantity = _to_decimal(item.get('quantity'), f'الكمية - {product.name}')
                if quantity <= 0:
                    raise _SaleAbort(f'الكمية للصنف "{product.name}" يجب أن تكون أكبر من صفر.')

                uom_id = item.get('uom_id')
                puom = None
                if uom_id not in (None, '', 'base'):
                    try:
                        puom = ProductUoM.objects.select_related('uom').get(id=int(uom_id), product=product)
                    except (ProductUoM.DoesNotExist, ValueError, TypeError):
                        raise _SaleAbort(f'وحدة القياس المحددة غير صالحة للصنف "{product.name}".')
                    if puom.is_base:
                        puom = None

                # --- Server-side pricing ---
                server_price, min_price = _pos_unit_price(product, puom, partner, branch, quantity)
                unit_price = server_price
                client_price_raw = item.get('unit_price')
                can_edit_price = (
                    product.is_open_price
                    or product.product_type == 'SERVICE'
                    or allow_price_edit_setting
                    or is_admin
                )
                if client_price_raw not in (None, ''):
                    client_price = _to_decimal(client_price_raw, f'السعر - {product.name}')
                    if client_price < 0:
                        raise _SaleAbort(f'سعر الصنف "{product.name}" لا يمكن أن يكون سالباً.')
                    if can_edit_price:
                        price_is_free = product.is_open_price or product.product_type == 'SERVICE'
                        if (not is_admin and not price_is_free and price_margin_pct > 0
                                and server_price > 0 and client_price != server_price):
                            deviation_pct = (abs(client_price - server_price) / server_price) * Decimal('100')
                            if deviation_pct > price_margin_pct + Decimal('0.01'):
                                low = server_price * (Decimal('1') - price_margin_pct / Decimal('100'))
                                high = server_price * (Decimal('1') + price_margin_pct / Decimal('100'))
                                raise _SaleAbort(
                                    f'سعر الصنف "{product.name}" ({client_price:.2f}) خارج هامش التعديل المسموح '
                                    f'(±{price_margin_pct:.2f}% من {server_price:.2f}: بين {low:.2f} و {high:.2f}). '
                                    'يتطلب ذلك صلاحية مدير.', status=403
                                )
                        unit_price = client_price
                if min_price > 0 and unit_price < min_price:
                    raise _SaleAbort(
                        f'سعر الصنف "{product.name}" ({unit_price:.2f}) أقل من أدنى سعر بيع مسموح ({min_price:.2f}).'
                    )

                discount_pct = _to_decimal(item.get('discount_percent', 0), f'الخصم - {product.name}')
                if discount_pct < 0 or discount_pct > 100:
                    raise _SaleAbort(f'نسبة الخصم للصنف "{product.name}" يجب أن تكون بين 0 و 100.')
                if max_discount_pct is not None and discount_pct > max_discount_pct + discount_tolerance:
                    raise _SaleAbort(
                        f'الخصم على الصنف "{product.name}" ({discount_pct:.2f}%) يتجاوز الحد الأقصى المسموح ({max_discount_pct:.2f}%). '
                        'يتطلب ذلك صلاحية مدير.', status=403
                    )
                if discount_pct > 0 and not can_override_discount and min_price > 0:
                    net_unit = unit_price * (Decimal('1') - discount_pct / Decimal('100'))
                    if net_unit < min_price:
                        raise _SaleAbort(
                            f'الخصم على الصنف "{product.name}" ينزل بالسعر ({net_unit:.2f}) عن أدنى سعر بيع ({min_price:.2f}). '
                            'يتطلب ذلك صلاحية مدير.'
                        )

                # Taxes: client may only toggle them on/off; the rate comes from the product.
                client_tax = _to_decimal(item.get('tax_rate', 0), 'الضريبة')
                client_wht = _to_decimal(item.get('wht_rate', 0), 'الخصم والإضافة')
                tax_rate = (product.tax_rate or Decimal('0')) if client_tax > 0 else Decimal('0')
                wht_rate = (product.withholding_tax_rate or Decimal('0')) if client_wht > 0 else Decimal('0')

                # --- Serial validation ---
                serial_item = None
                if product.product_type == 'SERIALIZED':
                    serial_item_id = item.get('serial_item_id')
                    if not serial_item_id:
                        raise _SaleAbort(f'يجب تحديد السيريال/IMEI للصنف "{product.name}".')
                    if quantity != 1:
                        raise _SaleAbort(f'الصنف "{product.name}" مسلسل؛ الكمية لكل سطر يجب أن تكون 1.')
                    try:
                        serial_item = SerialItem.objects.select_for_update().get(
                            id=int(serial_item_id), product=product, is_sold=False, warehouse=warehouse,
                        )
                    except (SerialItem.DoesNotExist, ValueError, TypeError):
                        raise _SaleAbort(
                            f'السيريال المحدد للصنف "{product.name}" غير متاح أو تم بيعه مسبقاً.', status=200
                        )
                    if serial_item.id in used_serial_ids:
                        raise _SaleAbort(f'السيريال نفسه مكرر في السلة للصنف "{product.name}".')
                    used_serial_ids.add(serial_item.id)

                InvoiceLine.objects.create(
                    invoice=invoice,
                    product=product,
                    serial_item=serial_item,
                    quantity=quantity,
                    unit_price=unit_price,
                    discount_pct=discount_pct,
                    uom=puom,
                    tax_rate=tax_rate,
                    wht_rate=wht_rate,
                )
                line_count += 1

            if line_count == 0:
                raise _SaleAbort('السلة فارغة. يرجى إضافة منتجات أولاً.')

            # --- Server-side totals and payment reconciliation ---
            invoice.recalculate_totals()
            total = Decimal(str(invoice.total_amount)).quantize(Decimal('0.01'))
            tolerance = Decimal('0.05')

            if large_amount_threshold > 0 and total >= large_amount_threshold and _is_generic_cash_customer(partner):
                raise _SaleAbort(
                    f'إجمالي الفاتورة ({total:.2f}) يساوي أو يتجاوز {large_amount_threshold:.2f}: '
                    'يجب اختيار عميل مسجل (غير العميل النقدي) لإتمام هذه العملية.'
                )

            # Cash rounding (SystemSetting.pos_cash_rounding): only the cash part is rounded.
            # The invoice total stays exact; the difference is posted to 5115.
            rounding_amount = Decimal('0')
            paid = {'paid_cash': Decimal('0'), 'paid_card': Decimal('0'), 'paid_ewallet': Decimal('0'),
                    'paid_bank': Decimal('0'), 'paid_credit': Decimal('0')}
            if payment_type == 'SPLIT':
                non_cash = cl_card + cl_ewallet + cl_bank + cl_credit
                if non_cash > total + tolerance:
                    raise _SaleAbort(
                        f'مجموع المبالغ غير النقدية ({non_cash:.2f}) يتجاوز إجمالي الفاتورة ({total:.2f}).'
                    )
                exact_cash_due = max(Decimal('0'), total - non_cash)
                cash_due = round_cash(exact_cash_due) if exact_cash_due > 0 else exact_cash_due
                rounding_amount = cash_due - exact_cash_due
                if cl_cash + tolerance < cash_due:
                    raise _SaleAbort(
                        f'المبالغ المدفوعة ({(cl_cash + non_cash):.2f}) أقل من إجمالي الفاتورة ({total:.2f}).'
                    )
                # Cash overpayment is change returned to the customer: only the due part is recorded.
                paid.update(paid_cash=cash_due, paid_card=cl_card, paid_ewallet=cl_ewallet,
                            paid_bank=cl_bank, paid_credit=cl_credit)
            else:
                field = {
                    'CASH': 'paid_cash', 'CARD': 'paid_card', 'EWALLET': 'paid_ewallet',
                    'BANK_TRANSFER': 'paid_bank', 'CREDIT': 'paid_credit',
                }[payment_type]
                amount_due = total
                if payment_type == 'CASH':
                    amount_due = round_cash(total)
                    rounding_amount = amount_due - total
                if payment_type == 'CASH' and data.get('cash_received') not in (None, ''):
                    cash_received = _to_decimal(data.get('cash_received'), 'المبلغ المستلم')
                    if cash_received + tolerance < amount_due:
                        raise _SaleAbort(
                            f'المبلغ المستلم ({cash_received:.2f}) أقل من المبلغ المطلوب ({amount_due:.2f}).'
                        )
                paid[field] = amount_due
            for k, v in paid.items():
                setattr(invoice, k, v)
            invoice.rounding_amount = rounding_amount.quantize(Decimal('0.01'))
            invoice.save(update_fields=list(paid.keys()) + ['rounding_amount', 'updated_at'])

            confirmed_invoice = confirm_invoice(invoice.id)

        return JsonResponse({
            'success': True,
            'invoice_number': confirmed_invoice.invoice_number,
            'invoice_id': confirmed_invoice.id,
            'total_amount': str(Decimal(str(confirmed_invoice.total_amount)).quantize(Decimal('0.01'))),
            'rounding_amount': str(confirmed_invoice.rounding_amount or Decimal('0')),
            'paid_cash': str(confirmed_invoice.paid_cash or Decimal('0')),
            'partner_id': confirmed_invoice.partner_id if confirmed_invoice.partner else None,
            'partner_name': confirmed_invoice.partner.name if confirmed_invoice.partner else 'عميل نقدي',
        })
    except _SaleAbort as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=e.status)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

def tafqeet_arabic(number):
    try:
        from decimal import Decimal
        num = Decimal(str(number))
        integer_part = int(num)
        fraction_part = int(round((num - integer_part) * 100))

        ones = ["", "واحد", "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة"]
        tens = ["", "عشرة", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
        teens = ["عشرة", "أحد عشر", "اثنا عشر", "ثلاثة عشر", "أربعة عشر", "خمسة عشر", "ستة عشر", "سبعة عشر", "ثمانية عشر", "تسعة عشر"]
        hundreds = ["", "مائة", "مائتان", "ثلاثمائة", "أربعمائة", "خمسمائة", "ستمائة", "سبعمائة", "ثمانمائة", "تسعمائة"]

        def convert_chunk(n):
            c_res = []
            h = n // 100
            rem = n % 100
            if h > 0:
                c_res.append(hundreds[h])
            if 10 <= rem <= 19:
                c_res.append(teens[rem - 10])
            else:
                t = rem // 10
                o = rem % 10
                if o > 0 and t > 0:
                    c_res.append(f"{ones[o]} و{tens[t]}")
                elif o > 0:
                    c_res.append(ones[o])
                elif t > 0:
                    c_res.append(tens[t])
            return " و".join(c_res)

        if integer_part == 0:
            words = "صفر"
        else:
            chunks = []
            millions = (integer_part // 1000000) % 1000
            thousands = (integer_part // 1000) % 1000
            rem_units = integer_part % 1000

            if millions > 0:
                if millions == 1:
                    chunks.append("مليون")
                elif millions == 2:
                    chunks.append("مليونان")
                elif 3 <= millions <= 10:
                    chunks.append(f"{convert_chunk(millions)} ملايين")
                else:
                    chunks.append(f"{convert_chunk(millions)} مليون")

            if thousands > 0:
                if thousands == 1:
                    chunks.append("ألف")
                elif thousands == 2:
                    chunks.append("ألفان")
                elif 3 <= thousands <= 10:
                    chunks.append(f"{convert_chunk(thousands)} آلاف")
                else:
                    chunks.append(f"{convert_chunk(thousands)} ألف")

            if rem_units > 0:
                chunks.append(convert_chunk(rem_units))

            words = " و".join(chunks)

        res = f"فقط {words} جنيهاً مصرياً"
        if fraction_part > 0:
            res += f" و{convert_chunk(fraction_part)} قرشاً"
        res += " لا غير"
        return res
    except Exception:
        return ""


@login_required
def receipt_view(request, invoice_id):
    invoice = get_object_or_404(
        _scope_by_allowed_branches(Invoice.objects.all(), request)
        .select_related('partner', 'branch', 'cashier').prefetch_related('lines__product', 'lines__uom__uom'),
        id=invoice_id
    )
    from apps.tenant.core.models import SystemSetting
    system_settings = None
    try:
        system_settings = SystemSetting.get_settings()
    except Exception:
        pass

    # Default paper width: SystemSetting.receipt_paper_width (58 / 80 mm).
    default_size = f"{getattr(system_settings, 'receipt_paper_width', '80') or '80'}mm"
    if default_size not in ('58mm', '80mm'):
        default_size = '80mm'
    paper_size = (request.GET.get('size') or default_size).strip().lower()
    if paper_size not in ['80mm', '58mm', 'a4', 'a5']:
        paper_size = default_size

    store_name = ""
    tax_id = ""
    cr_number = ""
    receipt_footer = ""
    store_phone = ""
    store_address = ""
    company_logo_url = ""
    show_branch_info = True

    if system_settings:
        show_branch_info = bool(getattr(system_settings, 'show_branch_info_on_receipt', True))
        logo = getattr(system_settings, 'company_logo', None)
        if logo:
            try:
                company_logo_url = logo.url
            except Exception:
                company_logo_url = ""
        store_name = system_settings.store_name or getattr(system_settings, "company_name", "") or ""
        tax_id = system_settings.tax_number or ""
        cr_number = getattr(system_settings, 'commercial_register', '') or ""
        receipt_footer = system_settings.receipt_footer or ""

    if not store_name and hasattr(request, 'tenant'):
        store_name = request.tenant.name

    if invoice.branch and show_branch_info:
        if not store_phone and invoice.branch.phone:
            store_phone = invoice.branch.phone
        if not store_address and invoice.branch.address:
            store_address = invoice.branch.address

    amount_in_words = tafqeet_arabic(invoice.total_amount)
    rounding_amount = Decimal(str(invoice.rounding_amount or 0))
    cash_after_rounding = None
    if rounding_amount != 0:
        if invoice.payment_type == Invoice.CASH:
            cash_after_rounding = (Decimal(str(invoice.total_amount)) + rounding_amount).quantize(Decimal('0.01'))
        else:
            cash_after_rounding = invoice.paid_cash

    return render(request, 'pos/receipt.html', {
        'company_logo_url': company_logo_url,
        'rounding_amount': rounding_amount,
        'cash_after_rounding': cash_after_rounding,
        'invoice': invoice,
        'paper_size': paper_size,
        'store_name': store_name,
        'tax_id': tax_id,
        'cr_number': cr_number,
        'receipt_footer': receipt_footer,
        'store_phone': store_phone,
        'store_address': store_address,
        'amount_in_words': amount_in_words,
        'system_settings': system_settings,
    })

@login_required
@require_POST
def process_invoice_return(request, invoice_id):
    if not is_admin_user(request.user):
        return JsonResponse({'status': 'error', 'message': 'غير مصرح لك بإجراء هذه العملية.'}, status=403)

        
    try:
        data = json.loads(request.body)
        items = data.get('items', [])
        if not items:
            return JsonResponse({'status': 'error', 'message': 'لم يتم تحديد أصناف للإرجاع.'})
            
        original_invoice = get_object_or_404(Invoice, id=invoice_id, status=Invoice.POSTED)
        
        if original_invoice.invoice_type not in [Invoice.SALE, Invoice.PURCHASE]:
            return JsonResponse({'status': 'error', 'message': 'لا يمكن إرجاع هذه الفاتورة.'})
            
        return_type = Invoice.RETURN_SALE if original_invoice.invoice_type == Invoice.SALE else Invoice.RETURN_PURCHASE
        
        from django.db import transaction
        import datetime
        from apps.tenant.core.models import SystemSetting
        from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
        from apps.tenant.invoicing.models import POSSession

        payment_type = data.get('payment_type') or original_invoice.payment_type
        treasury_id = data.get('treasury_id') or (original_invoice.treasury_id if original_invoice.treasury else None)
        bank_account_id = data.get('bank_account_id') or (original_invoice.bank_account_id if original_invoice.bank_account else None)
        insta_bank_account_id = data.get('insta_bank_account_id') or (original_invoice.insta_bank_account_id if getattr(original_invoice, 'insta_bank_account', None) else None)
        ewallet_id = data.get('ewallet_id') or (original_invoice.ewallet_id if original_invoice.ewallet else None)

        split_data = data.get('split_payments', {})
        req_cash = Decimal(str(split_data.get('cash', 0) or 0)) if payment_type == Invoice.SPLIT else (Decimal('1') if payment_type == Invoice.CASH else Decimal('0'))
        req_card = Decimal(str(split_data.get('card', 0) or 0)) if payment_type == Invoice.SPLIT else (Decimal('1') if payment_type == Invoice.CARD else Decimal('0'))
        req_ewallet = Decimal(str(split_data.get('ewallet', 0) or 0)) if payment_type == Invoice.SPLIT else (Decimal('1') if payment_type == Invoice.EWALLET else Decimal('0'))
        req_bank = Decimal(str(split_data.get('bank', 0) or 0)) if payment_type == Invoice.SPLIT else (Decimal('1') if payment_type == Invoice.BANK_TRANSFER else Decimal('0'))

        # Defaults of the branch the refund belongs to (the original invoice's branch).
        branch_defaults = get_pos_defaults(original_invoice.branch if original_invoice.branch_id else None)
        if req_cash > 0 and not treasury_id:
            def_t = branch_defaults['treasury'] or Treasury.objects.first()
            treasury_id = def_t.id if def_t else None

        if (req_card > 0 or req_bank > 0) and not bank_account_id:
            def_b = branch_defaults['bank_account'] or BankAccount.objects.first()
            bank_account_id = def_b.id if def_b else None

        if req_ewallet > 0 and not ewallet_id:
            def_w = branch_defaults['ewallet'] or EWallet.objects.first()
            ewallet_id = def_w.id if def_w else None

        open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
        session_to_use = open_session or original_invoice.pos_session

        with transaction.atomic():
            # Lock the original invoice so concurrent returns against it are serialized
            original_invoice = Invoice.objects.select_for_update().get(id=original_invoice.id)
            if original_invoice.status != Invoice.POSTED:
                raise Exception('الفاتورة الأصلية لم تعد مرحّلة.')

            # Create Return Invoice
            return_invoice = Invoice.objects.create(
                invoice_type=return_type,
                partner=original_invoice.partner,
                treasury_id=treasury_id,
                ewallet_id=ewallet_id,
                bank_account_id=bank_account_id,
                insta_bank_account_id=insta_bank_account_id,
                pos_machine=original_invoice.pos_machine,
                branch=original_invoice.branch,
                warehouse=original_invoice.warehouse,
                date=datetime.date.today(),
                payment_type=payment_type,
                status=Invoice.DRAFT,
                cashier=request.user,
                parent_invoice=original_invoice,
                discount_percentage=original_invoice.discount_percentage,
                pos_session=session_to_use,
            )
            
            for item in items:
                line_id = item.get('line_id')
                return_qty = Decimal(str(item.get('quantity', 0)))
                serial_id = item.get('serial_id')
                
                if return_qty <= 0:
                    continue

                original_line = InvoiceLine.objects.select_for_update().filter(
                    id=line_id, invoice=original_invoice
                ).select_related('product').first()
                if not original_line:
                    raise Exception('سطر الفاتورة المحدد غير موجود.')
                    
                available_to_return = original_line.quantity - original_line.returned_quantity
                if return_qty > available_to_return:
                    raise Exception(f'الكمية المرتجعة للسطر {original_line.product.name} تتجاوز الكمية المسموحة.')
                    
                # Update original line
                original_line.returned_quantity += return_qty
                original_line.save(update_fields=['returned_quantity'])
                
                # Create return line
                InvoiceLine.objects.create(
                    invoice=return_invoice,
                    original_line=original_line,
                    product=original_line.product,
                    serial_item=original_line.serial_item,
                    quantity=return_qty,
                    unit_price=original_line.unit_price,
                    discount_pct=original_line.discount_pct,
                    uom=original_line.uom,
                    tax_rate=original_line.tax_rate,
                    wht_rate=original_line.wht_rate,
                )
            
            # Recalculate Totals
            return_invoice.recalculate_totals()
            total_amount = return_invoice.total_amount

            # Set Paid Breakdown
            is_cash_partner = not original_invoice.partner or 'عميل نقدي' in original_invoice.partner.name or 'مورد نقدي' in original_invoice.partner.name or original_invoice.partner.name.strip() in ['عميل نقدي', 'نقدي', 'طياري', 'مورد نقدي']
            if payment_type == Invoice.SPLIT:
                paid_cash = Decimal(str(split_data.get('cash', 0) or 0))
                paid_card = Decimal(str(split_data.get('card', 0) or 0))
                paid_ewallet = Decimal(str(split_data.get('ewallet', 0) or 0))
                paid_bank = Decimal(str(split_data.get('bank', 0) or 0))
                paid_credit = Decimal(str(split_data.get('credit', 0) or 0))
                
                total_split = paid_cash + paid_card + paid_ewallet + paid_bank + paid_credit
                if abs(total_split - total_amount) > Decimal('0.05'):
                    raise Exception(f'إجمالي مبالغ رد المرتجع ({total_split}) لا يساوي قيمة المرتجع ({total_amount}).')
                    
                if paid_credit > 0 and is_cash_partner:
                    raise Exception('لا يمكن رد جزء من المبلغ كـ (رصيد دائن / آجل) لعميل أو مورد نقدي.')

                return_invoice.paid_cash = paid_cash
                return_invoice.paid_card = paid_card
                return_invoice.paid_ewallet = paid_ewallet
                return_invoice.paid_bank = paid_bank
                return_invoice.paid_credit = paid_credit
            else:
                if payment_type == Invoice.CREDIT and is_cash_partner:
                    raise Exception('لا يمكن رد المبلغ كـ (رصيد دائن / آجل) لعميل أو مورد نقدي.')

                return_invoice.paid_cash = total_amount if payment_type == Invoice.CASH else Decimal('0')
                return_invoice.paid_card = total_amount if payment_type == Invoice.CARD else Decimal('0')
                return_invoice.paid_ewallet = total_amount if payment_type == Invoice.EWALLET else Decimal('0')
                return_invoice.paid_bank = total_amount if payment_type == Invoice.BANK_TRANSFER else Decimal('0')
                return_invoice.paid_credit = total_amount if payment_type == Invoice.CREDIT else Decimal('0')

            return_invoice.save(update_fields=['paid_cash', 'paid_card', 'paid_ewallet', 'paid_bank', 'paid_credit'])

            # Confirm
            from apps.tenant.services.invoice_service import confirm_invoice
            confirmed_return = confirm_invoice(return_invoice.id)
            
            return JsonResponse({'status': 'success', 'invoice_id': confirmed_return.id})
            
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

@login_required
def shift_report(request):
    import datetime
    from django.db.models import Sum, Q
    from django.db.models.functions import Coalesce
    from decimal import Decimal

    today = datetime.date.today()
    invoices = Invoice.objects.filter(date=today, status='POSTED', cashier=request.user)
    agg = invoices.aggregate(
        total_cash=Coalesce(Sum('total_amount', filter=Q(payment_type='CASH')), Decimal('0.00')),
        total_card=Coalesce(Sum('total_amount', filter=Q(payment_type='CARD')), Decimal('0.00')),
        total_credit=Coalesce(Sum('total_amount', filter=Q(payment_type='CREDIT')), Decimal('0.00')),
        total_amount=Coalesce(Sum('total_amount'), Decimal('0.00')),
    )
    
    context = {
        'invoices': invoices,
        'total_cash': agg['total_cash'],
        'total_card': agg['total_card'],
        'total_credit': agg['total_credit'],
        'total_amount': agg['total_amount'],
        'title': 'تقرير الوردية (اليوم)'
    }
    return render(request, 'invoicing/shift_report.html', context)


@login_required
@custom_permission_required('invoicing.view_invoice', redirect_url='core:main_screen')
def sales_returns_dashboard(request):
    """Dashboard view listing all sales returns and summary statistics"""
    from django.db.models import Sum, Count
    
    returns_qs = Invoice.objects.filter(invoice_type=Invoice.RETURN_SALE).select_related('partner', 'warehouse', 'branch', 'treasury').order_by('-date', '-created_at')
    
    # Filter by partner if provided
    partner_id = request.GET.get('partner_id')
    if partner_id:
        returns_qs = returns_qs.filter(partner_id=partner_id)
        
    payment_type = request.GET.get('payment_type')
    if payment_type:
        returns_qs = returns_qs.filter(payment_type=payment_type)

    total_returns_count = returns_qs.count()
    total_returns_amount = returns_qs.aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
    credit_returns_amount = returns_qs.filter(payment_type=Invoice.CREDIT).aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
    cash_returns_amount = returns_qs.exclude(payment_type=Invoice.CREDIT).aggregate(s=Sum('total_amount'))['s'] or Decimal('0')

    customers = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH'], is_active=True)

    from django.core.paginator import Paginator
    paginator = Paginator(returns_qs, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'title': 'مركز مرتجعات المبيعات',
        'returns': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'customers': customers,
        'total_returns_count': total_returns_count,
        'total_returns_amount': total_returns_amount,
        'credit_returns_amount': credit_returns_amount,
        'cash_returns_amount': cash_returns_amount,
        'selected_partner_id': partner_id,
        'selected_payment_type': payment_type,
    }
    return render(request, 'invoicing/sales_returns_dashboard.html', context)


@login_required
@custom_permission_required('invoicing.add_invoice', redirect_url='core:main_screen')
def create_standalone_sales_return(request):
    """View to create a standalone sales return (e.g. for opening balance items) without original invoice"""
    from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
    from apps.tenant.services.invoice_service import confirm_invoice
    
    if request.method == 'POST':
        try:
            partner_id = request.POST.get('partner_id')
            warehouse_id = request.POST.get('warehouse_id')
            payment_type = request.POST.get('payment_type', Invoice.CREDIT)
            treasury_id = request.POST.get('treasury_id')
            bank_id = request.POST.get('bank_account_id')
            ewallet_id = request.POST.get('ewallet_id')
            notes = request.POST.get('notes', '')
            
            if not partner_id:
                messages.error(request, 'يرجى تحديد العميل.')
                return redirect('invoicing:create_standalone_sales_return')
                
            if not warehouse_id:
                messages.error(request, 'يرجى تحديد المستودع المستلم.')
                return redirect('invoicing:create_standalone_sales_return')

            partner = Partner.objects.get(id=partner_id)
            warehouse = Warehouse.objects.get(id=warehouse_id)
            if is_branch_restricted(request) and warehouse.branch_id not in (get_allowed_branch_ids(request) or []):
                messages.error(request, 'لا يمكنك استلام مرتجع على مستودع خارج فرعك.')
                return redirect('invoicing:create_standalone_sales_return')
            branch = warehouse.branch or get_working_branch(request)
            
            treasury = Treasury.objects.filter(id=treasury_id).first() if treasury_id else None
            bank_account = BankAccount.objects.filter(id=bank_id).first() if bank_id else None
            ewallet = EWallet.objects.filter(id=ewallet_id).first() if ewallet_id else None

            # Get submitted products
            product_ids = request.POST.getlist('product_ids[]')
            quantities = request.POST.getlist('quantities[]')
            prices = request.POST.getlist('prices[]')
            uom_ids = request.POST.getlist('uom_ids[]')
            
            if not product_ids:
                messages.error(request, 'يرجى إضافة صنف واحد على الأقل للمرتجع.')
                return redirect('invoicing:create_standalone_sales_return')
                
            from django.db import transaction
            with transaction.atomic():
                return_invoice = Invoice.objects.create(
                    invoice_type=Invoice.RETURN_SALE,
                    partner=partner,
                    branch=branch,
                    warehouse=warehouse,
                    treasury=treasury,
                    bank_account=bank_account,
                    ewallet=ewallet,
                    payment_type=payment_type,
                    status=Invoice.DRAFT,
                    cashier=request.user,
                    notes=notes,
                    date=timezone.now().date(),
                )
                
                from apps.tenant.inventory.models import ProductUoM, SerialItem
                apply_vat = request.POST.get('apply_vat') in ['true', 'on', '1']
                apply_wht = request.POST.get('apply_wht') in ['true', 'on', '1']
                
                for idx, (pid, qty_str, price_str) in enumerate(zip(product_ids, quantities, prices)):
                    qty = Decimal(qty_str or '0')
                    price = Decimal(price_str or '0')
                    if qty < 0 or price < 0:
                        raise ValueError('الكمية والسعر لا يمكن أن يكونا بقيم سالبة.')
                    if qty > 0:
                        prod = Product.objects.get(id=pid)
                        uom_id = uom_ids[idx] if idx < len(uom_ids) and uom_ids[idx] else None
                        uom_obj = None
                        if uom_id:
                            uom_obj = ProductUoM.objects.filter(id=uom_id, product=prod).first()
                        if not uom_obj:
                            # Empty value in the form means the base unit (factor 1)
                            uom_obj = prod.uoms.filter(is_base=True).first()
                        
                        line_tax_rate = prod.tax_rate if apply_vat else Decimal('0')
                        line_wht_rate = prod.withholding_tax_rate if apply_wht else Decimal('0')
                        
                        if prod.product_type == 'SERIALIZED':
                            imei_1_list = request.POST.getlist(f'imei_1_line_{idx}[]')
                            imei_2_list = request.POST.getlist(f'imei_2_line_{idx}[]')
                            serial_list = request.POST.getlist(f'serial_line_{idx}[]')
                            condition_list = request.POST.getlist(f'condition_line_{idx}[]')
                            
                            num_items = int(qty)
                            for j in range(num_items):
                                sn = serial_list[j] if j < len(serial_list) else ''
                                i1 = imei_1_list[j] if j < len(imei_1_list) else ''
                                i2 = imei_2_list[j] if j < len(imei_2_list) else ''
                                cond = condition_list[j] if j < len(condition_list) else SerialItem.CONDITION_NEW
                                
                                serial_item = None
                                if i1:
                                    serial_item = SerialItem.objects.filter(product=prod, imei_1=i1).first()
                                elif sn:
                                    serial_item = SerialItem.objects.filter(product=prod, serial_number=sn).first()
                                    
                                if serial_item:
                                    serial_item = SerialItem.objects.select_for_update().get(pk=serial_item.pk)
                                    if not serial_item.is_sold and serial_item.warehouse_id:
                                        raise ValueError(
                                            f'السيريال/IMEI ({i1 or sn}) للصنف "{prod.name}" موجود بالفعل في المخزون ولم يُبع.'
                                        )
                                    serial_item.warehouse = warehouse
                                    serial_item.is_sold = False
                                    serial_item.is_returned = False
                                    serial_item.save()
                                else:
                                    serial_item = SerialItem.objects.create(
                                        product=prod,
                                        serial_number=sn if sn else None,
                                        imei_1=i1 if i1 else None,
                                        imei_2=i2 if i2 else None,
                                        condition=cond if cond else SerialItem.CONDITION_NEW,
                                        warehouse=warehouse,
                                        is_sold=False,
                                        actual_cost=price,
                                    )
                                    
                                inv_line = InvoiceLine(
                                    invoice=return_invoice,
                                    product=prod,
                                    quantity=Decimal('1'),
                                    unit_price=price,
                                    uom=uom_obj,
                                    serial_item=serial_item,
                                    tax_rate=line_tax_rate,
                                    wht_rate=line_wht_rate,
                                )
                                inv_line.save()
                        else:
                            inv_line = InvoiceLine(
                                invoice=return_invoice,
                                product=prod,
                                quantity=qty,
                                unit_price=price,
                                uom=uom_obj,
                                tax_rate=line_tax_rate,
                                wht_rate=line_wht_rate,
                            )
                            inv_line.save()
                
                return_invoice.recalculate_totals()
                
                # Confirm and post invoice
                confirmed_invoice = confirm_invoice(return_invoice.id)
                messages.success(request, f'تم إنشاء وقيد مرتجع المبيعات بنجاح رقم ({confirmed_invoice.invoice_number}).')
                return redirect('invoicing:sales_returns_dashboard')
                
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء حفظ المرتجع: {str(e)}')
            return redirect('invoicing:create_standalone_sales_return')

    customers = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH'], is_active=True)
    warehouses = Warehouse.objects.filter(is_active=True)
    treasuries = Treasury.objects.all()
    bank_accounts = BankAccount.objects.all()
    ewallets = EWallet.objects.all()
    products = Product.objects.filter(is_active=True).prefetch_related('uoms__uom')

    from apps.tenant.core.models import SystemSetting

    context = {
        'title': 'إضافة مرتجع مبيعات مباشر (رصيد أول المدة)',
        'customers': customers,
        'warehouses': warehouses,
        'treasuries': treasuries,
        'bank_accounts': bank_accounts,
        'ewallets': ewallets,
        'products': products,
        'settings': SystemSetting.get_settings(),
    }
    return render(request, 'invoicing/standalone_sales_return_form.html', context)


# ---------------------------------------------------------------------------
# Quotation Views (عروض الأسعار)
# ---------------------------------------------------------------------------
from .models import Quotation, QuotationLine
from .forms import QuotationForm, QuotationLineFormSet

@login_required
@custom_permission_required('invoicing.view_quotation', redirect_url='core:main_screen')
def quotation_list(request):
    """List all quotations with status filtering and pagination."""
    from django.core.paginator import Paginator
    status_filter = request.GET.get('status')
    quotations = _scoped_quotations(request).select_related('partner', 'branch', 'warehouse', 'created_by').order_by('-date', '-created_at')
    if status_filter:
        quotations = quotations.filter(status=status_filter)
    
    paginator = Paginator(quotations, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'quotations': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
        'selected_status': status_filter,
    }
    return render(request, 'invoicing/quotation_list.html', context)


def _get_quotation_products_json():
    import json
    products = Product.objects.filter(is_active=True).prefetch_related('uoms__uom')
    products_dict = {}
    for p in products:
        base_unit_name = p.get_unit_display()
        p_uoms = [{'id': '', 'name': f"{base_unit_name} (أساسية)", 'factor': 1.0}]
        for pu in p.uoms.all():
            p_uoms.append({
                'id': pu.id,
                'name': f"{pu.uom.name} (معامل: {float(pu.conversion_factor)})",
                'factor': float(pu.conversion_factor)
            })
        products_dict[str(p.id)] = {
            'name': p.name,
            'sale_price': float(p.sale_price or 0),
            'tax_rate': float(p.tax_rate or 0),
            'wht_rate': float(p.withholding_tax_rate or 0),
            'base_unit': base_unit_name,
            'uoms': p_uoms
        }
    return _safe_json_for_script(products_dict)


def _format_quotation_form_errors(form, formset):
    FIELD_LABELS = {
        'partner': 'العميل',
        'branch': 'الفرع',
        'warehouse': 'المستودع للصرف',
        'date': 'تاريخ العرض',
        'valid_until': 'تاريخ الانتهاء',
        'product': 'المنتج / الخدمة',
        'uom': 'الوحدة',
        'quantity': 'الكمية',
        'unit_price': 'سعر الوحدة',
        'discount_pct': 'نسبة الخصم %',
        'tax_rate': 'ضريبة القيمة المضافة (VAT) %',
        'wht_rate': 'ضريبة الخصم والإضافة (WHT) %',
    }
    msgs = []
    for field, err_list in form.errors.items():
        fname = FIELD_LABELS.get(field, field)
        msgs.append(f"{fname}: {', '.join(err_list)}")
    for idx, f_errors in enumerate(formset.errors):
        for field, err_list in f_errors.items():
            fname = FIELD_LABELS.get(field, field)
            msgs.append(f"البند رقم {idx + 1} ({fname}): {', '.join(err_list)}")
    if formset.non_form_errors():
        msgs.extend(formset.non_form_errors())
    if not msgs:
        return 'يرجى مراجعة البيانات المدخلة وإضافة بند واحد على الأقل.'
    return 'تعذر الحفظ: ' + ' | '.join(msgs)


def _quotation_discount_cap_error(user, form, formset):
    """
    Enforce SystemSetting.max_pos_discount_percentage for non-admins on a
    quotation (header discount + each line discount), same rule/tolerance as
    POS complete_sale. Quotations become sales invoices via quotation_convert.
    Returns an Arabic error message, or None when within the limit.
    """
    lines = []
    for line_form in formset.forms:
        cd = getattr(line_form, 'cleaned_data', None) or {}
        if not cd or cd.get('DELETE') or not cd.get('product'):
            continue
        lines.append((cd['product'].name, cd.get('discount_pct')))
    return _discount_cap_error(user, form.cleaned_data.get('discount_percentage'), lines)


def _discount_cap_error(user, header_pct, lines):
    """
    Core discount-cap rule (same tolerance/messages as complete_sale).
    ``lines`` is an iterable of (product_name, discount_pct). Returns an Arabic
    error message or None. Admins / a 100% cap are never limited.
    """
    max_discount_pct = _max_discount_pct_for(user)
    if max_discount_pct is None:
        return None
    header_pct = Decimal(str(header_pct or 0))
    if header_pct > max_discount_pct + DISCOUNT_CAP_TOLERANCE:
        return (
            f'نسبة خصم الفاتورة ({header_pct:.2f}%) تتجاوز الحد الأقصى المسموح ({max_discount_pct:.2f}%). '
            'يتطلب ذلك صلاحية مدير.'
        )
    for product_name, line_pct in lines:
        line_pct = Decimal(str(line_pct or 0))
        if line_pct > max_discount_pct + DISCOUNT_CAP_TOLERANCE:
            return (
                f'الخصم على الصنف "{product_name}" ({line_pct:.2f}%) يتجاوز الحد الأقصى المسموح ({max_discount_pct:.2f}%). '
                'يتطلب ذلك صلاحية مدير.'
            )
    return None


def _scoped_quotations(request):
    """Quotations the user may see: admins all, non-admins their branch, no branch -> none."""
    return _scope_by_allowed_branches(Quotation.objects.all(), request)


def _quotation_branch_error(request, form):
    """Non-admins may only save a quotation on one of their own branches;
    nobody may pick another branch while their POS shift is open."""
    branch = form.cleaned_data.get('branch')
    lock_error = shift_branch_lock_error(request, branch)
    if lock_error:
        return lock_error
    if not is_branch_restricted(request):
        return None
    if branch is None or branch.id not in (get_allowed_branch_ids(request) or []):
        return 'لا يمكنك حفظ عرض سعر على فرع غير الفرع المرتبط بحسابك.'
    return None


@custom_permission_required('invoicing.add_quotation', redirect_url='core:main_screen')
def quotation_create(request):
    """Create a new Quotation."""
    if is_branch_unassigned(request):
        messages.error(request, BRANCH_UNASSIGNED_MESSAGE)
        return redirect('invoicing:quotation_list')
    if request.method == 'POST':
        form = QuotationForm(request.POST)
        formset = QuotationLineFormSet(request.POST)
        cap_error = None
        if form.is_valid() and formset.is_valid():
            cap_error = (_quotation_branch_error(request, form)
                         or _quotation_discount_cap_error(request.user, form, formset))
        if cap_error:
            messages.error(request, cap_error)
        elif form.is_valid() and formset.is_valid():
            quotation = form.save(commit=False)
            quotation.created_by = request.user
            quotation.save()
            
            formset.instance = quotation
            formset.save()
            quotation.recalculate_totals()
            
            messages.success(request, f'تم إنشاء عرض السعر رقم ({quotation.quotation_number}) بنجاح.')
            return redirect('invoicing:quotation_detail', quotation_id=quotation.id)
        else:
            messages.error(request, _format_quotation_form_errors(form, formset))
    else:
        # Defaults
        initial_branch = get_branch_scope(request)
        if initial_branch is None and not is_branch_restricted(request):
            initial_branch = Branch.objects.filter(is_active=True).first()
        initial_warehouse = Warehouse.objects.filter(is_active=True).first()
        form = QuotationForm(initial={
            'branch': initial_branch,
            'warehouse': initial_warehouse,
            'date': timezone.now().date(),
        })
        formset = QuotationLineFormSet()

    context = {
        'form': form,
        'formset': formset,
        'title': 'إنشاء عرض سعر جديد',
        'is_edit': False,
        'products_json': _get_quotation_products_json(),
    }
    return render(request, 'invoicing/quotation_form.html', context)


@login_required
@custom_permission_required('invoicing.change_quotation', redirect_url='core:main_screen')
def quotation_edit(request, quotation_id):
    """Edit an existing Quotation."""
    quotation = get_object_or_404(_scoped_quotations(request), id=quotation_id)
    if quotation.status in [Quotation.CONVERTED, Quotation.CANCELLED]:
        messages.warning(request, 'لا يمكن تعديل عرض سعر تم تحويله إلى فاتورة أو ملغى.')
        return redirect('invoicing:quotation_detail', quotation_id=quotation.id)

    if request.method == 'POST':
        form = QuotationForm(request.POST, instance=quotation)
        formset = QuotationLineFormSet(request.POST, instance=quotation)
        cap_error = None
        if form.is_valid() and formset.is_valid():
            cap_error = (_quotation_branch_error(request, form)
                         or _quotation_discount_cap_error(request.user, form, formset))
        if cap_error:
            messages.error(request, cap_error)
        elif form.is_valid() and formset.is_valid():
            from django.db import transaction
            with transaction.atomic():
                # Quotations confirmed under the old logic deducted stock; give it back
                # before the lines/warehouse change (quotations no longer move stock).
                quotation.restore_legacy_stock()
                form.save()
                formset.save()
                quotation.recalculate_totals()
            messages.success(request, f'تم تعديل عرض السعر ({quotation.quotation_number}) بنجاح.')
            return redirect('invoicing:quotation_detail', quotation_id=quotation.id)
        else:
            messages.error(request, _format_quotation_form_errors(form, formset))
    else:
        form = QuotationForm(instance=quotation)
        formset = QuotationLineFormSet(instance=quotation)

    context = {
        'form': form,
        'formset': formset,
        'quotation': quotation,
        'title': f'تعديل عرض السعر {quotation.quotation_number}',
        'is_edit': True,
        'products_json': _get_quotation_products_json(),
    }
    return render(request, 'invoicing/quotation_form.html', context)


@login_required
@custom_permission_required('invoicing.view_quotation', redirect_url='core:main_screen')
def quotation_detail(request, quotation_id):
    """View Quotation details."""
    quotation = get_object_or_404(
        _scoped_quotations(request).select_related('partner', 'branch', 'warehouse', 'created_by', 'converted_invoice').prefetch_related('lines__product', 'lines__uom'),
        id=quotation_id
    )
    context = {
        'quotation': quotation,
    }
    return render(request, 'invoicing/quotation_detail.html', context)


@login_required
@custom_permission_required('invoicing.delete_quotation', redirect_url='core:main_screen')
def quotation_delete(request, quotation_id):
    """Delete a Quotation."""
    quotation = get_object_or_404(_scoped_quotations(request), id=quotation_id)
    if quotation.status == Quotation.CONVERTED:
        messages.error(request, 'لا يمكن حذف عرض سعر تم تحويله إلى فاتورة.')
        return redirect('invoicing:quotation_detail', quotation_id=quotation.id)

    if request.method == 'POST':
        from django.db import transaction
        num = quotation.quotation_number
        try:
            with transaction.atomic():
                quotation.restore_legacy_stock()
                quotation.delete()
        except Exception as e:
            messages.error(request, f'تعذر حذف عرض السعر: {str(e)}')
            return redirect('invoicing:quotation_detail', quotation_id=quotation.id)
        messages.success(request, f'تم حذف عرض السعر {num} بنجاح.')
        return redirect('invoicing:quotation_list')

    return render(request, 'invoicing/quotation_confirm_delete.html', {'quotation': quotation})


@login_required
@custom_permission_required('invoicing.change_quotation', redirect_url='core:main_screen')
def quotation_confirm(request, quotation_id):
    """Confirm quotation: status change only (no stock / accounting effect)."""
    quotation = get_object_or_404(_scoped_quotations(request), id=quotation_id)
    if request.method == 'POST':
        try:
            quotation.confirm_quotation()
            messages.success(request, f'تم تأكيد عرض السعر ({quotation.quotation_number}). سيتم خصم المخزون عند تحويله إلى فاتورة بيع.')
        except Exception as e:
            messages.error(request, f'تعذر ترحيل عرض السعر: {str(e)}')
    return redirect('invoicing:quotation_detail', quotation_id=quotation.id)


@login_required
@custom_permission_required('invoicing.change_quotation', redirect_url='core:main_screen')
def quotation_convert(request, quotation_id):
    """Convert quotation directly into a confirmed Sales Invoice with stock deduction in one step."""
    quotation = get_object_or_404(_scoped_quotations(request), id=quotation_id)
    if quotation.status == Quotation.CONVERTED and quotation.converted_invoice:
        messages.info(request, f'عرض السعر محول مسبقاً إلى فاتورة رقم ({quotation.converted_invoice.invoice_number}).')
        return redirect('invoicing:invoice_detail', invoice_id=quotation.converted_invoice.id)

    if request.method == 'POST':
        from django.db import transaction
        from apps.tenant.services.invoice_service import confirm_invoice
        # Re-check the discount cap for non-admins (the quotation may predate the
        # cap or have been written by someone else).
        cap_error = _discount_cap_error(
            request.user,
            quotation.discount_percentage,
            [(line.product.name if line.product_id else '', line.discount_pct)
             for line in quotation.lines.select_related('product')],
        )
        if cap_error:
            messages.error(request, cap_error)
            return redirect('invoicing:quotation_detail', quotation_id=quotation.id)
        try:
            with transaction.atomic():
                invoice = quotation.convert_to_invoice(user=request.user)
                confirm_invoice(invoice.id)
            
            messages.success(
                request,
                f'تم تسليم البضاعة وعمل فاتورة بيع مؤكدة رقم ({invoice.invoice_number}) وخصم المخزون بنجاح.'
            )
            return redirect('invoicing:invoice_detail', invoice_id=invoice.id)
        except Exception as e:
            messages.error(request, f'تعذر تسليم وعمل الفاتورة المؤكدة: {str(e)}')
            return redirect('invoicing:quotation_detail', quotation_id=quotation.id)

    return redirect('invoicing:quotation_detail', quotation_id=quotation.id)


@login_required
@custom_permission_required('invoicing.view_quotation', redirect_url='core:main_screen')
def quotation_print(request, quotation_id):
    """Printable Quotation View."""
    quotation = get_object_or_404(
        _scoped_quotations(request).select_related('partner', 'branch', 'warehouse').prefetch_related('lines__product', 'lines__uom'),
        id=quotation_id
    )
    system_setting = SystemSetting.get_settings()
    context = {
        'quotation': quotation,
        'setting': system_setting,
    }
    return render(request, 'invoicing/quotation_print.html', context)


@login_required
def get_product_last_prices(request):
    """
    Returns the last 5 selling prices/invoice lines for a specific product and customer in POS.
    Query parameters:
    - product_id (int)
    - partner_id (int, optional)
    """
    from django.http import JsonResponse
    from apps.tenant.inventory.models import Product
    from apps.tenant.partners.models import Partner
    from .models import InvoiceLine, Invoice

    product_id = request.GET.get('product_id')
    partner_id = request.GET.get('partner_id')

    if not product_id:
        return JsonResponse({'success': False, 'error': 'معرف الصنف مطلوب'}, status=400)

    try:
        product = Product.objects.get(id=int(product_id))
    except (Product.DoesNotExist, ValueError):
        return JsonResponse({'success': False, 'error': 'الصنف غير موجود'}, status=404)

    lines_qs = InvoiceLine.objects.filter(
        product_id=product.id,
        invoice__invoice_type='SALE',
        invoice__status='POSTED'
    ).select_related('invoice', 'invoice__partner', 'uom', 'uom__uom')

    partner = None
    if partner_id and partner_id not in ['null', 'undefined', '']:
        try:
            partner_id_int = int(partner_id)
            if partner_id_int > 0:
                partner = Partner.objects.get(id=partner_id_int)
                lines_qs = lines_qs.filter(invoice__partner_id=partner_id_int)
        except (Partner.DoesNotExist, ValueError):
            pass

    # Order by invoice created date descending and limit to 5
    recent_lines = lines_qs.order_by('-invoice__created_at', '-invoice__id')[:5]

    history = []
    for line in recent_lines:
        inv = line.invoice
        uom_name = ''
        try:
            if line.uom and hasattr(line.uom, 'uom') and line.uom.uom:
                uom_name = line.uom.uom.name
            else:
                uom_name = product.pos_unit_name
        except Exception:
            uom_name = product.get_unit_display() or ''

        history.append({
            'invoice_id': inv.id,
            'invoice_number': inv.invoice_number,
            'date': inv.date.strftime('%Y-%m-%d') if inv.date else inv.created_at.strftime('%Y-%m-%d'),
            'customer_name': inv.partner.name if inv.partner else 'عميل نقدي',
            'unit_price': float(line.unit_price),
            'quantity': float(line.quantity),
            'discount_pct': float(line.discount_pct),
            'subtotal': float(line.subtotal),
            'uom_name': uom_name or '',
            'payment_type_display': inv.get_payment_type_display(),
        })

    return JsonResponse({
        'success': True,
        'product_name': product.name,
        'product_barcode': product.barcode or '',
        'customer_name': partner.name if partner else 'عميل نقدي',
        'history': history,
        'count': len(history)
    })


@login_required
def recent_transactions_api(request):
    """API returning recent sales transactions for dashboard feed."""
    invoices = _scope_by_allowed_branches(Invoice.objects.filter(invoice_type='SALE'), request)

    invoices = invoices.select_related('branch').order_by('-created_at')[:10]
    data = []
    for inv in invoices:
        branch_name = inv.branch.name if inv.branch else 'المركز الرئيسي'
        created_str = inv.created_at.strftime('%Y-%m-%d %H:%M') if inv.created_at else ''
        data.append({
            'invoice_number': inv.invoice_number,
            'branch_name': branch_name,
            'created_at': created_str,
            'total_amount': float(inv.total_amount or 0),
        })

    return JsonResponse({'transactions': data})


def get_shift_stats_data(open_session):
    """Calculates real-time shift statistics and recent invoices for cashier dashboard."""
    if not open_session:
        return {
            'has_open_shift': False,
            'total_sales': 0.0,
            'invoices_count': 0,
            'cash_in_drawer': 0.0,
            'card_sales': 0.0,
            'opening_balance': 0.0,
            'recent_invoices': [],
        }

    from apps.tenant.accounting.models import Voucher
    from django.db.models import Sum, Q, F, Case, When
    from django.db.models.functions import Coalesce

    posted_invoices = Invoice.objects.filter(pos_session=open_session, status=Invoice.POSTED)
    sales_invoices = posted_invoices.filter(invoice_type=Invoice.SALE)
    total_sales = sales_invoices.aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
    invoices_count = posted_invoices.count()

    inv_agg = posted_invoices.aggregate(
        total_cash_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_cash')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.CASH), then=F('total_amount') + F('rounding_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_cash')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.CASH), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_card_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_card')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.CARD), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_card')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.CARD), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_ewallet_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_ewallet')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.EWALLET), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_ewallet')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.EWALLET), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_bank_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_bank')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.BANK_TRANSFER), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_bank')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.BANK_TRANSFER), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
    )
    total_cash_sales = inv_agg['total_cash_sales']
    total_card_sales = inv_agg['total_card_sales']
    total_ewallet_sales = inv_agg['total_ewallet_sales']
    total_bank_sales = inv_agg['total_bank_sales']

    v_base = Voucher.objects.filter(status=Voucher.POSTED).filter(
        Q(payment_method=Voucher.CASH) | Q(payment_method=Voucher.SPLIT, paid_cash__gt=0)
    )
    time_filter = Q(treasury=open_session.treasury, created_at__gte=open_session.start_time)
    if open_session.end_time:
        time_filter &= Q(created_at__lte=open_session.end_time)
    shift_vouchers = v_base.filter(Q(pos_session=open_session) | time_filter).distinct()
    v_cash_expr = Case(
        When(payment_method=Voucher.SPLIT, then=F('paid_cash')),
        default=F('amount'),
        output_field=models.DecimalField(max_digits=18, decimal_places=4)
    )
    v_agg = shift_vouchers.aggregate(
        total_cash_receipts=Coalesce(Sum(v_cash_expr, filter=Q(voucher_type=Voucher.RECEIPT)), Decimal('0.00')),
        total_cash_payments=Coalesce(Sum(v_cash_expr, filter=Q(voucher_type=Voucher.PAYMENT)), Decimal('0.00')),
    )
    total_cash_receipts = v_agg['total_cash_receipts']
    total_cash_payments = v_agg['total_cash_payments']

    from apps.tenant.rentals.models import Rental
    rental_time_filter = Q(created_at__gte=open_session.start_time, treasury=open_session.treasury)
    if open_session.end_time:
        rental_time_filter &= Q(created_at__lte=open_session.end_time)
    total_cash_rentals = Rental.objects.filter(rental_time_filter, payment_method=Rental.PAYMENT_CASH).aggregate(s=Sum('total_amount'))['s'] or Decimal('0')

    cash_in_drawer = open_session.opening_balance + total_cash_sales + total_cash_receipts - total_cash_payments + total_cash_rentals

    recent_invoices = []
    for inv in posted_invoices.select_related('partner').order_by('-id')[:8]:
        recent_invoices.append({
            'id': inv.id,
            'number': inv.invoice_number,
            'time': inv.created_at.strftime('%I:%M %p') if inv.created_at else '',
            'customer': inv.partner.name if inv.partner else 'عميل نقدي',
            'total': float(inv.total_amount or 0),
            'payment_type': inv.get_payment_type_display(),
            'is_return': inv.invoice_type == Invoice.RETURN_SALE,
        })

    return {
        'has_open_shift': True,
        'shift_id': open_session.id,
        'shift_start': open_session.start_time.strftime('%H:%M') if open_session.start_time else '',
        'treasury_name': open_session.treasury.name if open_session.treasury else '',
        'total_sales': float(total_sales),
        'invoices_count': invoices_count,
        'cash_in_drawer': float(cash_in_drawer),
        'card_sales': float(total_card_sales),
        'ewallet_sales': float(total_ewallet_sales),
        'bank_sales': float(total_bank_sales),
        'opening_balance': float(open_session.opening_balance),
        'recent_invoices': recent_invoices,
    }


def pos_shift_stats_api(request):
    """API endpoint returning live cashier shift stats and recent invoices."""
    if not request.user.is_authenticated:
        return JsonResponse({'has_open_shift': False, 'error': 'Unauthorized'}, status=401)
    open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
    if not open_session and (request.user.is_superuser or (hasattr(request.user, 'employee_profile') and request.user.employee_profile.is_admin())):
        open_session = POSSession.objects.filter(status=POSSession.OPEN).first()
    data = get_shift_stats_data(open_session)
    return JsonResponse(data)






