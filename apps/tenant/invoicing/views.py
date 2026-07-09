from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from .models import Invoice, InvoiceLine
from apps.tenant.inventory.models import Product, Warehouse
from apps.tenant.core.models import Branch
from apps.tenant.partners.models import Partner
import json
from decimal import Decimal

@login_required
def pos_view(request):
    """Hybrid Sales Point View"""
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
        'ewallets': ewallets,
        'branches': branches,
        'is_admin': is_admin,
    }
    return render(request, 'pos/index.html', context)

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
            
            discount_amount = Decimal(request.POST.get('discount_amount') or 0)
            vat_percentage = Decimal(request.POST.get('vat_percentage') or 0)
            wht_percentage = Decimal(request.POST.get('wht_percentage') or 0)
            
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
                    discount_amount=discount_amount,
                    vat_percentage=vat_percentage,
                    wht_percentage=wht_percentage,
                )
                
                for pid, qty, price in zip(product_ids, quantities, prices):
                    if pid and qty and price:
                        product = Product.objects.get(id=pid)
                        InvoiceLine.objects.create(
                            invoice=invoice,
                            product=product,
                            quantity=Decimal(qty),
                            unit_price=Decimal(price),
                        )
                
                invoice.recalculate_totals()
                
                from apps.tenant.services.invoice_service import confirm_invoice
                confirm_invoice(invoice.id)
                
            messages.success(request, 'تم حفظ وترحيل الفاتورة بنجاح!')
            return redirect('invoicing:invoice_list')
        except Exception as e:
            import traceback
            traceback.print_exc()
            messages.error(request, f'حدث خطأ: {str(e)}')
            
            # Re-pass the posted data back to context to prevent form clearing
            context = {
                'suppliers': suppliers,
                'warehouses': warehouses,
                'products': products,
                'branches': branches,
                'treasuries': treasuries,
                'title': 'إنشاء فاتورة مشتريات',
                'posted_data': request.POST,
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
    invoices = Invoice.objects.filter(invoice_type__in=['SALE', 'RETURN_SALE']).order_by('-date', '-created_at')
    
    context = {
        'invoices': invoices,
        'title': 'فواتير المبيعات',
        'list_type': 'sales'
    }
    return render(request, 'invoicing/invoice_list.html', context)

@login_required
def purchase_invoice_list(request):
    invoices = Invoice.objects.filter(invoice_type__in=['PURCHASE', 'RETURN_PURCHASE']).order_by('-date', '-created_at')
    
    context = {
        'invoices': invoices,
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
                subtotal=Decimal(data.get('subtotal', 0)),
                tax_amount=Decimal(data.get('tax_amount', 0)),
                total_amount=Decimal(data.get('total_amount', 0)),
            )
            
            # Create Lines
            from apps.tenant.accounting.models import Tax
            
            for item in cart:
                product = Product.objects.get(id=item['product_id'])
                uom_id = item.get('uom_id')
                if uom_id == 'base':
                    uom_id = None
                
                tax_rate_val = Decimal(item.get('tax_rate', 0))
                tax_obj = None
                if tax_rate_val > 0:
                    tax_obj = Tax.objects.filter(rate=tax_rate_val).first()
                    
                InvoiceLine.objects.create(
                    invoice=invoice,
                    product=product,
                    quantity=Decimal(item['quantity']),
                    unit_price=Decimal(item['unit_price']),
                    discount_pct=Decimal(item.get('discount_percent', 0)),
                    tax=tax_obj,
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
