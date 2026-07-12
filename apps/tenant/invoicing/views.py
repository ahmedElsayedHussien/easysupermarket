from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.contrib import messages
from .models import Invoice, InvoiceLine, POSSession
from apps.tenant.inventory.models import Product, Warehouse
from apps.tenant.core.models import Branch, SystemSetting
from apps.tenant.partners.models import Partner
import json
from decimal import Decimal

@login_required
def pos_view(request):
    """Hybrid Sales Point View"""
    
    # Check if cashier has an open shift
    open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
    if not open_session:
        messages.warning(request, 'يجب فتح وردية أولاً قبل الدخول لنقطة البيع.')
        return redirect('invoicing:pos_open_shift')

    products = Product.objects.filter(is_active=True).select_related('category').prefetch_related('uoms__uom')
    warehouses = Warehouse.objects.filter(is_active=True)
    
    customers = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH'], is_active=True)
    
    # Determine if user is admin
    is_admin = request.user.is_superuser or request.user.is_staff
    
    # Get user's branch from Employee profile
    user_branch = None
    if hasattr(request.user, 'employee_profile'):
        user_branch = request.user.employee_profile.branch
    
    current_branch = None
    branch_id = request.GET.get('branch_id')
    if branch_id:
        current_branch = Branch.objects.filter(id=branch_id, is_active=True).first()
    
    if not is_admin and user_branch:
        # Non-admin: force their branch
        current_branch = user_branch
    elif not current_branch:
        if user_branch:
            current_branch = user_branch
        else:
            current_branch = getattr(request, 'branch', None)
        if not current_branch:
            current_branch = Branch.objects.filter(is_active=True).first()
        
    current_warehouse = None
    warehouse_id = request.GET.get('warehouse_id')
    if warehouse_id:
        current_warehouse = warehouses.filter(id=warehouse_id, branch=current_branch).first()
    if not current_warehouse:
        current_warehouse = warehouses.filter(branch=current_branch).first() if current_branch else None
    
    products_list = list(products)
    for p in products_list:
        p.sale_price = p.get_price_for_branch(current_branch)
        if current_warehouse:
            p.pos_stock = p.get_stock(warehouse=current_warehouse)
        else:
            p.pos_stock = 0
    
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
        bank_accounts = BankAccount.objects.filter(branch=current_branch) if current_branch else BankAccount.objects.none()
        ewallets = EWallet.objects.filter(branch=current_branch) if current_branch else EWallet.objects.none()
        branches = Branch.objects.filter(id=current_branch.id) if current_branch else Branch.objects.none()

    context = {
        'products': products_list,
        'warehouses': warehouses.filter(branch=current_branch),
        'customers': customers,
        'current_warehouse': current_warehouse,
        'current_branch': current_branch,
        'treasuries': treasuries,
        'bank_accounts': bank_accounts,
        'bank_accounts': bank_accounts,
        'ewallets': ewallets,
        'branches': branches,
        'is_admin': is_admin,
        'pos_session': open_session,
        'settings': SystemSetting.get_settings(),
    }
    return render(request, 'pos/index.html', context)

@login_required
def pos_open_shift(request):
    """View to open a new cashier shift"""
    open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
    if open_session:
        messages.info(request, 'لديك وردية مفتوحة بالفعل.')
        return redirect('invoicing:pos')

    from apps.tenant.accounting.models import Treasury
    
    # Determine branch
    user_branch = None
    if hasattr(request.user, 'employee_profile'):
        user_branch = request.user.employee_profile.branch
    
    if not user_branch:
        user_branch = Branch.objects.filter(is_active=True).first()

    if request.method == 'POST':
        opening_balance_str = request.POST.get('opening_balance', '0')
        treasury_id = request.POST.get('treasury_id')
        if not treasury_id:
            messages.error(request, 'برجاء اختيار خزينة.')
        else:
            try:
                opening_balance = Decimal(opening_balance_str)
                treasury = Treasury.objects.get(id=treasury_id)
                
                POSSession.objects.create(
                    branch=user_branch,
                    treasury=treasury,
                    user=request.user,
                    opening_balance=opening_balance,
                    status=POSSession.OPEN
                )
                messages.success(request, 'تم فتح الوردية بنجاح.')
                return redirect('invoicing:pos')
            except Exception as e:
                messages.error(request, f'حدث خطأ: {str(e)}')
            
    treasuries = Treasury.objects.all()
    if user_branch and not (request.user.is_superuser or request.user.is_staff):
        treasuries = treasuries.filter(branch=user_branch)
        
    context = {
        'title': 'فتح وردية كاشير',
        'treasuries': treasuries,
        'current_branch': user_branch
    }
    return render(request, 'pos/open_shift.html', context)


@login_required
def pos_close_shift(request):
    """View to close an active cashier shift"""
    open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
    if not open_session:
        messages.warning(request, 'ليس لديك وردية مفتوحة لإغلاقها.')
        return redirect('invoicing:pos_open_shift')

    # Calculate Expected Closing Balance (Opening + Cash Sales - Cash Refunds)
    # We sum all CASH invoices linked to this session.
    cash_invoices = Invoice.objects.filter(pos_session=open_session, payment_type=Invoice.CASH, status=Invoice.POSTED)
    total_cash_sales = sum(inv.total_amount for inv in cash_invoices if inv.invoice_type == Invoice.SALE)
    total_cash_refunds = sum(inv.total_amount for inv in cash_invoices if inv.invoice_type == Invoice.RETURN_SALE)
    
    # Calculate non-cash sales for reporting
    card_invoices = Invoice.objects.filter(pos_session=open_session, payment_type=Invoice.CARD, status=Invoice.POSTED)
    total_card_sales = sum(inv.total_amount for inv in card_invoices if inv.invoice_type == Invoice.SALE) - sum(inv.total_amount for inv in card_invoices if inv.invoice_type == Invoice.RETURN_SALE)
    
    ewallet_invoices = Invoice.objects.filter(pos_session=open_session, payment_type=Invoice.EWALLET, status=Invoice.POSTED)
    total_ewallet_sales = sum(inv.total_amount for inv in ewallet_invoices if inv.invoice_type == Invoice.SALE) - sum(inv.total_amount for inv in ewallet_invoices if inv.invoice_type == Invoice.RETURN_SALE)
    
    bank_invoices = Invoice.objects.filter(pos_session=open_session, payment_type=Invoice.BANK_TRANSFER, status=Invoice.POSTED)
    total_bank_sales = sum(inv.total_amount for inv in bank_invoices if inv.invoice_type == Invoice.SALE) - sum(inv.total_amount for inv in bank_invoices if inv.invoice_type == Invoice.RETURN_SALE)
    
    credit_invoices = Invoice.objects.filter(pos_session=open_session, payment_type=Invoice.CREDIT, status=Invoice.POSTED)
    total_credit_sales = sum(inv.total_amount for inv in credit_invoices if inv.invoice_type == Invoice.SALE) - sum(inv.total_amount for inv in credit_invoices if inv.invoice_type == Invoice.RETURN_SALE)

    expected_balance = open_session.opening_balance + total_cash_sales - total_cash_refunds

    if request.method == 'POST':
        actual_balance_str = request.POST.get('actual_balance', '0')
        try:
            actual_balance = Decimal(actual_balance_str)
            difference = actual_balance - expected_balance
            
            open_session.closing_balance_expected = expected_balance
            open_session.closing_balance_actual = actual_balance
            open_session.difference = difference
            open_session.end_time = timezone.now()
            open_session.status = POSSession.CLOSED
            open_session.save()
            
            # Here we could generate manual GL entries if configured, 
            # but per user request, it's just a report for the accountant.
            
            messages.success(request, f'تم إغلاق الوردية. النقدية الفعلية: {actual_balance}، العجز/الزيادة: {difference}')
            return redirect('invoicing:shift_report')
        except Exception as e:
            messages.error(request, f'حدث خطأ: {str(e)}')

    context = {
        'title': 'إغلاق الوردية وجرد الدرج',
        'session': open_session,
        'expected_balance': expected_balance,
        'total_cash_sales': total_cash_sales,
        'total_cash_refunds': total_cash_refunds,
        'total_card_sales': total_card_sales,
        'total_ewallet_sales': total_ewallet_sales,
        'total_bank_sales': total_bank_sales,
        'total_credit_sales': total_credit_sales,
    }
    return render(request, 'pos/close_shift.html', context)

@login_required
def purchase_invoice_view(request):
    """Purchase Invoice Form"""
    suppliers = Partner.objects.filter(partner_type__in=['SUPPLIER', 'BOTH'], is_active=True)
    warehouses = Warehouse.objects.filter(is_active=True)
    products = Product.objects.filter(is_active=True)
    
    is_admin = False
    user_branch = None
    if hasattr(request.user, 'employee_profile'):
        is_admin = request.user.employee_profile.is_admin()
        user_branch = request.user.employee_profile.branch
    else:
        is_admin = request.user.is_superuser

    branches = Branch.objects.filter(is_active=True) if is_admin else []
    
    from apps.tenant.accounting.models import Treasury
    treasuries = Treasury.objects.all()

    if request.method == 'POST':
        from django.db import transaction
        from django.contrib import messages
        try:
            invoice_type = request.POST.get('invoice_type')
            supplier_id = request.POST.get('supplier_id')
            warehouse_id = request.POST.get('warehouse_id')
            payment_type = request.POST.get('payment_type')
            treasury_id = request.POST.get('treasury_id')
            if not treasury_id:
                treasury_id = None
            
            product_ids = request.POST.getlist('product_id[]')
            quantities = request.POST.getlist('quantity[]')
            prices = request.POST.getlist('price[]')
            
            discount_percentage = Decimal(request.POST.get('discount_percentage') or 0)
            
            vat_str = request.POST.get('vat_percentage', '')
            wht_str = request.POST.get('wht_percentage', '')
            vat_percentage = Decimal(vat_str) if vat_str != '' else None
            wht_percentage = Decimal(wht_str) if wht_str != '' else None
            
            with transaction.atomic():
                supplier = Partner.objects.get(id=supplier_id)
                warehouse = Warehouse.objects.get(id=warehouse_id)
                
                if is_admin:
                    branch_id = request.POST.get('branch_id')
                    branch = Branch.objects.get(id=branch_id)
                else:
                    branch = user_branch
                    
                if not branch:
                    raise ValueError("لا يوجد فرع مرتبط بهذا المستخدم. يرجى مراجعة مدير النظام.")
                
                invoice = Invoice.objects.create(
                    invoice_type=invoice_type,
                    partner=supplier,
                    warehouse=warehouse,
                    branch=branch,
                    date=timezone.now().date(),
                    payment_type=payment_type,
                    treasury_id=treasury_id,
                    status=Invoice.DRAFT,
                    cashier=request.user,
                    discount_percentage=discount_percentage,
                    vat_percentage=vat_percentage if vat_percentage is not None else Decimal('0'),
                    wht_percentage=wht_percentage if wht_percentage is not None else Decimal('0'),
                )
                
                for pid, qty, price in zip(product_ids, quantities, prices):
                    if pid and qty and price:
                        product = Product.objects.get(id=pid)
                        
                        # Apply global taxes if provided, else 0
                        wht_r = wht_percentage if wht_percentage is not None else Decimal('0')
                        tax_r = vat_percentage if vat_percentage is not None else Decimal('0')
                        
                        InvoiceLine.objects.create(
                            invoice=invoice,
                            product=product,
                            quantity=Decimal(qty),
                            unit_price=Decimal(price),
                            tax_rate=tax_r,
                            wht_rate=wht_r,
                            discount_pct=invoice.discount_percentage,
                        )
                
                invoice.recalculate_totals()
                
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
            for pid, qty, prc in zip(request.POST.getlist('product_id[]'), request.POST.getlist('quantity[]'), request.POST.getlist('price[]')):
                posted_lines.append({'product_id': pid, 'quantity': qty, 'price': prc})
            
            # Re-pass the posted data back to context to prevent form clearing
            context = {
                'suppliers': suppliers,
                'warehouses': warehouses,
                'products': products,
                'branches': branches,
                'treasuries': treasuries,
                'title': 'إنشاء فاتورة مشتريات',
                'posted_data': request.POST,
                'posted_lines_json': json.dumps(posted_lines),
            }
            return render(request, 'invoicing/purchase_invoice.html', context)
            
    context = {
        'suppliers': suppliers,
        'warehouses': warehouses,
        'products': products,
        'branches': branches,
        'treasuries': treasuries,
        'title': 'إنشاء فاتورة مشتريات'
    }
    return render(request, 'invoicing/purchase_invoice.html', context)

@login_required
def sales_invoice_list(request):
    search_query = request.GET.get('q', '')
    invoices = Invoice.objects.filter(invoice_type__in=['SALE', 'RETURN_SALE']).order_by('-date', '-created_at')
    
    if search_query:
        invoices = invoices.filter(partner__name__icontains=search_query)
        
    from django.core.paginator import Paginator
    paginator = Paginator(invoices, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'invoices': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'title': 'فواتير المبيعات',
        'list_type': 'sales'
    }
    return render(request, 'invoicing/invoice_list.html', context)

@login_required
def purchase_invoice_list(request):
    search_query = request.GET.get('q', '')
    invoices = Invoice.objects.filter(invoice_type__in=['PURCHASE', 'RETURN_PURCHASE']).order_by('-date', '-created_at')
    
    if search_query:
        invoices = invoices.filter(partner__name__icontains=search_query)
        
    from django.core.paginator import Paginator
    paginator = Paginator(invoices, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'invoices': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'title': 'فواتير المشتريات',
        'list_type': 'purchases'
    }
    return render(request, 'invoicing/invoice_list.html', context)

@login_required
def invoice_detail(request, invoice_id):
    from django.shortcuts import get_object_or_404
    invoice = get_object_or_404(Invoice, id=invoice_id)
    lines = invoice.lines.select_related('product').all()
    
    context = {
        'invoice': invoice,
        'lines': lines,
        'title': f'تفاصيل فاتورة رقم {invoice.invoice_number}'
    }
    return render(request, 'invoicing/invoice_detail.html', context)

@login_required
def confirm_invoice_view(request, invoice_id):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
        
    from apps.tenant.services.invoice_service import confirm_invoice
    try:
        invoice = confirm_invoice(invoice_id)
        return JsonResponse({'success': True, 'invoice_number': invoice.invoice_number})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
def get_product_by_barcode(request):
    barcode = request.GET.get('barcode', '').strip()
    if not barcode:
        return JsonResponse({'found': False})
        
    try:
        product = Product.objects.prefetch_related('uoms__uom').get(barcode=barcode, is_active=True)
        uoms = [{'id': 'base', 'name': product.get_unit_display()}]
        for puom in product.uoms.all():
            uoms.append({'id': puom.id, 'name': puom.uom.name})
            
        current_branch = getattr(request, 'branch', None)
            
        return JsonResponse({
            'found': True,
            'product': {
                'id': product.id,
                'name': product.name,
                'barcode': product.barcode,
                'sale_price': str(product.get_price_for_branch(current_branch)),
                'tax_rate': str(product.tax_rate),
                'available_stock': str(product.get_stock()),
                'uoms': uoms
            }
        })
    except Product.DoesNotExist:
        return JsonResponse({'found': False})

@login_required
def add_to_cart(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        # Logic to add to session cart
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})

@login_required
def complete_sale(request):
    if request.method != 'POST':
        return JsonResponse({'success': False})
        
    try:
        data = json.loads(request.body)
        cart = data.get('cart', [])
        payment_type = data.get('payment_type', 'CASH')
        warehouse_id = data.get('warehouse_id')
        branch_id = data.get('branch_id')
        treasury_id = data.get('treasury_id')
        ewallet_id = data.get('ewallet_id')
        bank_account_id = data.get('bank_account_id')
        
        partner_id = data.get('partner_id')
        invoice_type = data.get('invoice_type', 'SALE')
        
        from django.db import transaction
        from apps.tenant.services.invoice_service import confirm_invoice
        
        with transaction.atomic():
            branch = Branch.objects.get(id=branch_id)
            
            if not warehouse_id:
                return JsonResponse({'success': False, 'error': 'يجب اختيار مستودع لإتمام عملية البيع'})
            
            warehouse = Warehouse.objects.get(id=warehouse_id)
            
            if partner_id:
                partner = Partner.objects.get(id=partner_id)
            else:
                partner, _ = Partner.objects.get_or_create(
                    name="عميل نقدي",
                    defaults={'partner_type': 'CUSTOMER', 'is_active': True}
                )
            
            # Check if there is an open POSSession for this cashier
            open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()

            # Create Invoice
            invoice = Invoice.objects.create(
                invoice_type=invoice_type,
                branch=branch,
                warehouse=warehouse,
                partner=partner,
                date=timezone.now().date(),
                payment_type=payment_type,
                treasury_id=treasury_id if treasury_id else None,
                ewallet_id=ewallet_id if ewallet_id else None,
                bank_account_id=bank_account_id if bank_account_id else None,
                status='DRAFT',
                cashier=request.user,
                pos_session=open_session,
                subtotal=Decimal(data.get('subtotal', 0)),
                tax_amount=Decimal(data.get('tax_amount', 0)),
                total_amount=Decimal(data.get('total_amount', 0)),
            )
            
            for item in cart:
                product = Product.objects.get(id=item['product_id'])
                uom_id = item.get('uom_id')
                if uom_id == 'base':
                    uom_id = None
                
                InvoiceLine.objects.create(
                    invoice=invoice,
                    product=product,
                    quantity=Decimal(item['quantity']),
                    unit_price=Decimal(item['unit_price']),
                    discount_pct=Decimal(item.get('discount_percent', 0)),
                    uom_id=uom_id,
                )
                
            # Confirm
            confirmed_invoice = confirm_invoice(invoice.id)
            
        return JsonResponse({
            'success': True, 
            'invoice_number': confirmed_invoice.invoice_number,
            'invoice_id': confirmed_invoice.id
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
def receipt_view(request, invoice_id):
    invoice = get_object_or_404(Invoice, id=invoice_id)
    return render(request, 'pos/receipt.html', {'invoice': invoice})

@login_required
def shift_report(request):
    import datetime
    today = datetime.date.today()
    invoices = Invoice.objects.filter(date=today, status='POSTED', cashier=request.user)
    total_cash = sum(inv.total_amount for inv in invoices if inv.payment_type == 'CASH')
    total_card = sum(inv.total_amount for inv in invoices if inv.payment_type == 'CARD')
    total_credit = sum(inv.total_amount for inv in invoices if inv.payment_type == 'CREDIT')
    
    context = {
        'invoices': invoices,
        'total_cash': total_cash,
        'total_card': total_card,
        'total_credit': total_credit,
        'total_amount': total_cash + total_card + total_credit,
        'title': 'تقرير الوردية (اليوم)'
    }
    return render(request, 'invoicing/shift_report.html', context)
