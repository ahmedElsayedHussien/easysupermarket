from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db import transaction
from django.contrib import messages
from django.utils import timezone
from decimal import Decimal, InvalidOperation
import logging

from .models import MaintenanceTicket, TicketPart
from apps.tenant.partners.models import Partner
from apps.tenant.inventory.models import Product, Warehouse, SerialItem
from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
from apps.tenant.core.models import Branch
from apps.tenant.core.decorators import custom_permission_required
from apps.tenant.core.access import (
    filter_by_branch, get_branch_scope, is_branch_unassigned, BRANCH_UNASSIGNED_MESSAGE,
)

logger = logging.getLogger(__name__)

# Manual status changes allowed from the edit panel. DELIVERED is NEVER allowed here:
# it must go through deliver_ticket -> MaintenanceTicket.deliver() (journal + payment + COGS).
_OPEN_STATUSES = {
    MaintenanceTicket.STATUS_PENDING,
    MaintenanceTicket.STATUS_IN_PROGRESS,
    MaintenanceTicket.STATUS_WAITING_PARTS,
    MaintenanceTicket.STATUS_DONE,
}
ALLOWED_STATUS_TRANSITIONS = {st: _OPEN_STATUSES - {st} for st in _OPEN_STATUSES}


# ---------------------------------------------------------------------------
# Ticket List
# ---------------------------------------------------------------------------
@custom_permission_required('maintenance.view_maintenanceticket', redirect_url='core:main_screen')
def ticket_list(request):
    from django.core.paginator import Paginator
    status_filter = request.GET.get('status', '')
    q = request.GET.get('q', '').strip()
    qs = filter_by_branch(
        MaintenanceTicket.objects.select_related('customer', 'technician', 'branch').order_by('-created_at'),
        request,
    )
    if status_filter:
        qs = qs.filter(status=status_filter)
    if q:
        qs = qs.filter(
            __import__('django.db.models', fromlist=['Q']).Q(device_model__icontains=q) |
            __import__('django.db.models', fromlist=['Q']).Q(device_serial__icontains=q) |
            __import__('django.db.models', fromlist=['Q']).Q(customer__name__icontains=q)
        )

    paginator = Paginator(qs, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'tickets': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
        'status_choices': MaintenanceTicket.STATUS_CHOICES,
        'active_status': status_filter,
        'search_query': q,
        'title': 'تذاكر الصيانة',
    }
    return render(request, 'maintenance/ticket_list.html', context)


# ---------------------------------------------------------------------------
# Ticket Create
# ---------------------------------------------------------------------------
@custom_permission_required('maintenance.add_maintenanceticket', redirect_url='core:main_screen')
def ticket_create(request):
    if is_branch_unassigned(request):
        messages.error(request, BRANCH_UNASSIGNED_MESSAGE)
        return redirect('maintenance:ticket_list')
    branch = get_branch_scope(request)
    customers = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH'], is_active=True).order_by('name')
    technicians = __import__('django.contrib.auth', fromlist=['get_user_model']).get_user_model().objects.filter(is_active=True)
    parent_id = request.GET.get('parent_id')
    parent_ticket = None
    if parent_id:
        parent_ticket = filter_by_branch(MaintenanceTicket.objects.all(), request).filter(pk=parent_id).first()

    if request.method == 'POST':
        try:
            with transaction.atomic():
                customer_id = request.POST.get('customer_id')
                technician_id = request.POST.get('technician_id') or None
                device_model = request.POST.get('device_model', '').strip()
                device_serial = request.POST.get('device_serial', '').strip() or None
                issue_description = request.POST.get('issue_description', '').strip()
                labor_cost = Decimal(request.POST.get('labor_cost', '0') or '0')
                estimated_cost = request.POST.get('estimated_cost') or None
                warranty_days = int(request.POST.get('warranty_days', 0) or 0)
                parent_ticket_id = request.POST.get('parent_ticket_id') or None
                if parent_ticket_id and not filter_by_branch(
                        MaintenanceTicket.objects.all(), request).filter(pk=parent_ticket_id).exists():
                    parent_ticket_id = None
                notes = request.POST.get('notes', '').strip()
                device_condition = request.POST.get('device_condition_on_receipt', '').strip()

                if not customer_id or not device_model or not issue_description:
                    raise ValueError('يجب ملء جميع الحقول المطلوبة (العميل، الجهاز، وصف العطل)')

                ticket = MaintenanceTicket.objects.create(
                    branch=branch or Branch.objects.first(),
                    customer_id=customer_id,
                    technician_id=technician_id,
                    device_model=device_model,
                    device_serial=device_serial,
                    device_condition_on_receipt=device_condition,
                    issue_description=issue_description,
                    labor_cost=labor_cost,
                    estimated_cost=Decimal(str(estimated_cost)) if estimated_cost else None,
                    warranty_days=warranty_days,
                    parent_ticket_id=parent_ticket_id,
                    notes=notes,
                )
                messages.success(request, f'تم فتح تذكرة الصيانة #{ticket.id} بنجاح.')
                return redirect('maintenance:ticket_detail', pk=ticket.pk)
        except (ValueError, InvalidOperation) as e:
            messages.error(request, str(e))

    context = {
        'customers': customers,
        'technicians': technicians,
        'parent_ticket': parent_ticket,
        'title': 'فتح تذكرة صيانة جديدة',
    }
    return render(request, 'maintenance/ticket_form.html', context)


# ---------------------------------------------------------------------------
# Ticket Detail
# ---------------------------------------------------------------------------
@custom_permission_required('maintenance.view_maintenanceticket', redirect_url='core:main_screen')
def ticket_detail(request, pk):
    qs = filter_by_branch(MaintenanceTicket.objects.select_related(
        'customer', 'technician', 'branch', 'journal_entry', 'treasury', 'bank_account', 'ewallet'
    ), request)
    ticket = get_object_or_404(qs, pk=pk)
    parts = ticket.parts.select_related('product', 'warehouse', 'serial_item')
    
    products = Product.objects.filter(is_active=True).exclude(product_type='SERVICE').order_by('name')
    warehouses = Warehouse.objects.filter(is_active=True)
    treasuries = Treasury.objects.all()
    bank_accounts = BankAccount.objects.all()
    ewallets = EWallet.objects.all()

    if branch:
        warehouses = warehouses.filter(branch=branch)
        treasuries = treasuries.filter(branch=branch)
        
    technicians = __import__('django.contrib.auth', fromlist=['get_user_model']).get_user_model().objects.filter(is_active=True)

    context = {
        'ticket': ticket,
        'parts': parts,
        'products': products,
        'warehouses': warehouses,
        'treasuries': treasuries,
        'bank_accounts': bank_accounts,
        'ewallets': ewallets,
        'technicians': technicians,
        'status_choices': MaintenanceTicket.STATUS_CHOICES,
        'payment_method_choices': MaintenanceTicket.PAYMENT_METHOD_CHOICES,
        'title': f'تذكرة صيانة #{ticket.id}',
    }
    return render(request, 'maintenance/ticket_detail.html', context)


# ---------------------------------------------------------------------------
# Ticket Edit (status / labor_cost / notes)
# ---------------------------------------------------------------------------
@custom_permission_required('maintenance.change_maintenanceticket', redirect_url='core:main_screen')
def ticket_edit(request, pk):
    qs = filter_by_branch(MaintenanceTicket.objects.all(), request)
    ticket = get_object_or_404(qs, pk=pk)
    if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
        return JsonResponse({'error': 'لا يمكن تعديل تذكرة مسلّمة.'}, status=400)

    if request.method == 'POST':
        try:
            field = request.POST.get('field')
            value = request.POST.get('value', '').strip()
            with transaction.atomic():
                ticket = MaintenanceTicket.objects.select_for_update().get(pk=ticket.pk)
                if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
                    return JsonResponse({'error': 'لا يمكن تعديل تذكرة مسلّمة.'}, status=400)
                if field == 'status':
                    if value == ticket.status:
                        return JsonResponse({'success': True})
                    if value == MaintenanceTicket.STATUS_DELIVERED:
                        return JsonResponse({'error': 'التسليم يتم فقط من خلال زر "تسليم التذكرة" لتسجيل القيد والتحصيل.'}, status=400)
                    if value not in ALLOWED_STATUS_TRANSITIONS.get(ticket.status, set()):
                        return JsonResponse({'error': 'انتقال الحالة غير مسموح.'}, status=400)
                    ticket.status = value
                    ticket.save(update_fields=['status', 'updated_at'])
                elif field == 'labor_cost':
                    labor = Decimal(value or '0')
                    if not labor.is_finite() or labor < 0:
                        return JsonResponse({'error': 'المصنعية لا يمكن أن تكون سالبة.'}, status=400)
                    ticket.labor_cost = labor
                    ticket.save(update_fields=['labor_cost', 'updated_at'])
                elif field == 'technician_id':
                    ticket.technician_id = value or None
                    ticket.save(update_fields=['technician', 'updated_at'])
                elif field == 'notes':
                    ticket.notes = value
                    ticket.save(update_fields=['notes', 'updated_at'])
                else:
                    return JsonResponse({'error': 'حقل غير معروف.'}, status=400)
            return JsonResponse({'success': True})
        except InvalidOperation:
            return JsonResponse({'error': 'قيمة غير صالحة.'}, status=400)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Invalid method'}, status=405)


# ---------------------------------------------------------------------------
# Add Part to Ticket (with immediate stock deduction)
# ---------------------------------------------------------------------------
@custom_permission_required('maintenance.add_ticketpart', redirect_url='core:main_screen')
def add_part(request, pk):
    qs = filter_by_branch(MaintenanceTicket.objects.all(), request)
    ticket = get_object_or_404(qs, pk=pk)
    if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
        return JsonResponse({'error': 'لا يمكن إضافة قطع لتذكرة مسلّمة.'}, status=400)

    if request.method == 'POST':
        try:
            product_id  = request.POST.get('product_id')
            warehouse_id = request.POST.get('warehouse_id')
            try:
                quantity = Decimal(request.POST.get('quantity', '1') or '1')
                selling_price = Decimal(request.POST.get('selling_price', '0') or '0')
            except InvalidOperation:
                return JsonResponse({'error': 'الكمية أو السعر غير صالح.'}, status=400)
            if not quantity.is_finite() or quantity <= 0:
                return JsonResponse({'error': 'الكمية يجب أن تكون أكبر من صفر.'}, status=400)
            if not selling_price.is_finite() or selling_price < 0:
                return JsonResponse({'error': 'سعر البيع لا يمكن أن يكون سالباً.'}, status=400)
            serial_item_id = request.POST.get('serial_item_id') or None

            product   = get_object_or_404(Product, id=product_id)
            wh_qs = filter_by_branch(Warehouse.objects.all(), request)
            warehouse = get_object_or_404(wh_qs, id=warehouse_id)

            with transaction.atomic():
                ticket = MaintenanceTicket.objects.select_for_update().get(pk=ticket.pk)
                if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
                    return JsonResponse({'error': 'لا يمكن إضافة قطع لتذكرة مسلّمة.'}, status=400)

                serial_item = None
                if product.product_type == 'SERIALIZED':
                    if not serial_item_id:
                        return JsonResponse({'error': f'يجب تحديد السيريال للقطعة "{product.name}"'}, status=400)
                    if quantity != 1:
                        return JsonResponse({'error': 'كمية القطعة ذات السيريال يجب أن تكون 1.'}, status=400)
                    serial_item = SerialItem.objects.select_for_update().filter(
                        pk=serial_item_id, product=product, warehouse=warehouse, is_sold=False,
                    ).first()
                    if serial_item is None:
                        return JsonResponse({'error': 'السيريال غير متاح لهذا المنتج في هذا المخزن (أو تم بيعه).'}, status=400)

                part = TicketPart(
                    ticket=ticket,
                    product=product,
                    warehouse=warehouse,
                    quantity=quantity,
                    selling_price=selling_price,
                    serial_item=serial_item,
                )
                part.save()  # triggers _deduct_stock internally

            return JsonResponse({
                'success': True,
                'part_id': part.id,
                'product_name': product.name,
                'quantity': str(quantity),
                'selling_price': str(selling_price),
                'actual_cost': str(part.actual_cost),
            })
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Invalid method'}, status=405)


# ---------------------------------------------------------------------------
# Delete Part
# ---------------------------------------------------------------------------
@require_POST
@custom_permission_required('maintenance.delete_ticketpart', redirect_url='core:main_screen')
def delete_part(request, part_pk):
    """Delete a part and return its stock (FIFO batch at the consumed cost / un-sell the serial)."""
    from apps.tenant.services.fifo_engine import add_inventory_batch

    qs = filter_by_branch(TicketPart.objects.all(), request, 'ticket__branch')
    part = get_object_or_404(qs, pk=part_pk)

    try:
        with transaction.atomic():
            ticket = MaintenanceTicket.objects.select_for_update().get(pk=part.ticket_id)
            if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
                return JsonResponse({'error': 'لا يمكن حذف قطع من تذكرة مسلّمة.'}, status=400)
            part = (TicketPart.objects.select_for_update()
                    .select_related('product', 'warehouse').filter(pk=part.pk).first())
            if part is None:
                return JsonResponse({'error': 'القطعة غير موجودة (ربما حُذفت بالفعل).'}, status=404)

            product = part.product
            if part.stock_deducted and product.product_type != 'SERVICE':
                if product.product_type == 'SERIALIZED':
                    if part.serial_item_id:
                        serial = SerialItem.objects.select_for_update().get(pk=part.serial_item_id)
                        serial.is_sold = False
                        serial.warehouse = part.warehouse
                        serial.save()
                elif part.quantity and part.quantity > 0:
                    unit_cost = (Decimal(part.actual_cost or 0) / part.quantity).quantize(Decimal('0.0001'))
                    # add_inventory_batch overwrites last_purchase_price; a return is not a purchase.
                    prev_last_price = product.last_purchase_price
                    add_inventory_batch(
                        product=product,
                        warehouse=part.warehouse,
                        quantity=part.quantity,
                        unit_cost=unit_cost,
                        reference=f'صيانة-#{ticket.id}',
                        movement_type='RETURN_IN',
                    )
                    if product.last_purchase_price != prev_last_price:
                        product.last_purchase_price = prev_last_price
                        product.save(update_fields=['last_purchase_price', 'updated_at'])
            part.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        logger.exception('delete_part failed for part %s', part_pk)
        return JsonResponse({'error': str(e)}, status=400)


# ---------------------------------------------------------------------------
# Deliver Ticket (post journal + close ticket)
# ---------------------------------------------------------------------------
@custom_permission_required('maintenance.change_maintenanceticket', redirect_url='core:main_screen')
def deliver_ticket(request, pk):
    qs = filter_by_branch(MaintenanceTicket.objects.all(), request)
    ticket = get_object_or_404(qs, pk=pk)
    if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
        return JsonResponse({'error': 'هذه التذكرة مسلّمة بالفعل.'}, status=400)

    if request.method == 'POST':
        payment_method = request.POST.get('payment_method', MaintenanceTicket.PAYMENT_CASH)
        treasury_id = request.POST.get('treasury_id')
        bank_account_id = request.POST.get('bank_account_id')
        ewallet_id = request.POST.get('ewallet_id')
        paid_amount_str = request.POST.get('paid_amount', '')

        treasury = None
        bank_account = None
        ewallet = None
        paid_amount = None

        if payment_method == MaintenanceTicket.PAYMENT_CASH:
            if not treasury_id:
                return JsonResponse({'error': 'يجب تحديد الخزينة عند الدفع النقدي.'}, status=400)
            treasury = get_object_or_404(Treasury, id=treasury_id)

        elif payment_method == MaintenanceTicket.PAYMENT_BANK_TRANSFER:
            if not bank_account_id:
                return JsonResponse({'error': 'يجب تحديد الحساب البنكي عند السداد بالتحويل/انستا باي.'}, status=400)
            bank_account = get_object_or_404(BankAccount, id=bank_account_id)

        elif payment_method == MaintenanceTicket.PAYMENT_EWALLET:
            if not ewallet_id:
                return JsonResponse({'error': 'يجب تحديد المحفظة الإلكترونية عند السداد بالمحفظة.'}, status=400)
            ewallet = get_object_or_404(EWallet, id=ewallet_id)

        elif payment_method == MaintenanceTicket.PAYMENT_CREDIT:
            pass  # Full amount goes on customer account

        elif payment_method == MaintenanceTicket.PAYMENT_SPLIT:
            try:
                paid_amount = Decimal(paid_amount_str)
            except (InvalidOperation, TypeError):
                paid_amount = Decimal('0')

            if paid_amount > 0:
                if treasury_id:
                    treasury = get_object_or_404(Treasury, id=treasury_id)
                elif bank_account_id:
                    bank_account = get_object_or_404(BankAccount, id=bank_account_id)
                elif ewallet_id:
                    ewallet = get_object_or_404(EWallet, id=ewallet_id)
                else:
                    return JsonResponse({'error': 'يجب تحديد الحساب المالي (خزينة/بنك/محفظة) للدفعة الفورية.'}, status=400)
        else:
            return JsonResponse({'error': 'طريقة الدفع غير صالحة.'}, status=400)

        try:
            ticket.deliver(
                user=request.user,
                payment_method=payment_method,
                treasury=treasury,
                bank_account=bank_account,
                ewallet=ewallet,
                paid_amount=paid_amount
            )
            return JsonResponse({'success': True, 'redirect': f'/maintenance/{ticket.pk}/'})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Invalid method'}, status=405)


# ---------------------------------------------------------------------------
# API: Get Warehouse Products
# ---------------------------------------------------------------------------
@login_required
def get_warehouse_products(request, warehouse_id):
    """
    Returns a list of products that have stock in the given warehouse.
    Includes both normal products (InventoryBatch) and SERIALIZED products (SerialItem).
    Service products don't have stock, so we can optionally return them or not. We'll return them since they can be used anywhere.
    """
    from apps.tenant.inventory.models import InventoryBatch, SerialItem, Product
    from django.db.models import Sum

    # 1. Normal products with stock
    normal_batches = InventoryBatch.objects.filter(
        warehouse_id=warehouse_id,
        quantity_remaining__gt=0
    ).values('product_id').annotate(total_qty=Sum('quantity_remaining'))
    normal_product_ids = [b['product_id'] for b in normal_batches]

    # 2. Serialized products with stock
    serialized_items = SerialItem.objects.filter(
        warehouse_id=warehouse_id,
        is_sold=False
    ).values('product_id').distinct()
    serialized_product_ids = [s['product_id'] for s in serialized_items]

    # Combine IDs
    available_ids = set(normal_product_ids + serialized_product_ids)

    # 3. Always include SERVICE products
    service_products = Product.objects.filter(is_active=True, product_type='SERVICE').values_list('id', flat=True)
    available_ids.update(service_products)

    # Fetch product details
    products = Product.objects.filter(id__in=available_ids, is_active=True).order_by('name')
    
    data = []
    for p in products:
        data.append({
            'id': p.id,
            'name': p.name,
            'product_type': p.product_type,
            'barcode': p.barcode or ''
        })
    
    return JsonResponse({'products': data})
