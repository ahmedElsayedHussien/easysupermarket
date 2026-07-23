from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.db import transaction
from django.contrib import messages
from django.utils import timezone
from decimal import Decimal, InvalidOperation

from .models import MaintenanceTicket, TicketPart
from apps.tenant.partners.models import Partner
from apps.tenant.inventory.models import Product, Warehouse, SerialItem
from apps.tenant.accounting.models import Treasury
from apps.tenant.core.models import Branch


# ---------------------------------------------------------------------------
# Ticket List
# ---------------------------------------------------------------------------
@login_required
def ticket_list(request):
    status_filter = request.GET.get('status', '')
    q = request.GET.get('q', '').strip()
    branch = getattr(request, 'branch', None)

    qs = MaintenanceTicket.objects.select_related('customer', 'technician', 'branch')
    if branch:
        qs = qs.filter(branch=branch)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if q:
        qs = qs.filter(
            __import__('django.db.models', fromlist=['Q']).Q(device_model__icontains=q) |
            __import__('django.db.models', fromlist=['Q']).Q(device_serial__icontains=q) |
            __import__('django.db.models', fromlist=['Q']).Q(customer__name__icontains=q)
        )

    context = {
        'tickets': qs,
        'status_choices': MaintenanceTicket.STATUS_CHOICES,
        'active_status': status_filter,
        'title': 'تذاكر الصيانة',
    }
    return render(request, 'maintenance/ticket_list.html', context)


# ---------------------------------------------------------------------------
# Ticket Create
# ---------------------------------------------------------------------------
@login_required
def ticket_create(request):
    branch = getattr(request, 'branch', None)
    customers = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH'], is_active=True).order_by('name')
    technicians = __import__('django.contrib.auth', fromlist=['get_user_model']).get_user_model().objects.filter(is_active=True)
    parent_id = request.GET.get('parent_id')
    parent_ticket = None
    if parent_id:
        parent_ticket = MaintenanceTicket.objects.filter(pk=parent_id).first()

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
@login_required
def ticket_detail(request, pk):
    branch = getattr(request, 'branch', None)
    qs = MaintenanceTicket.objects.select_related(
        'customer', 'technician', 'branch', 'journal_entry', 'treasury'
    )
    if branch:
        qs = qs.filter(branch=branch)
    ticket = get_object_or_404(qs, pk=pk)
    parts = ticket.parts.select_related('product', 'warehouse', 'serial_item')
    
    # For adding parts
    products = Product.objects.filter(is_active=True).exclude(product_type='SERVICE').order_by('name')
    products = Product.objects.filter(is_active=True).exclude(product_type='SERVICE').order_by('name')
    
    warehouses = Warehouse.objects.filter(is_active=True)
    treasuries = Treasury.objects.all()
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
        'technicians': technicians,
        'status_choices': MaintenanceTicket.STATUS_CHOICES,
        'title': f'تذكرة صيانة #{ticket.id}',
    }
    return render(request, 'maintenance/ticket_detail.html', context)


# ---------------------------------------------------------------------------
# Ticket Edit (status / labor_cost / notes)
# ---------------------------------------------------------------------------
@login_required
def ticket_edit(request, pk):
    branch = getattr(request, 'branch', None)
    qs = MaintenanceTicket.objects.all()
    if branch:
        qs = qs.filter(branch=branch)
    ticket = get_object_or_404(qs, pk=pk)
    if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
        return JsonResponse({'error': 'لا يمكن تعديل تذكرة مسلّمة.'}, status=400)

    if request.method == 'POST':
        try:
            field = request.POST.get('field')
            value = request.POST.get('value', '').strip()
            if field == 'status':
                if value in dict(MaintenanceTicket.STATUS_CHOICES):
                    ticket.status = value
                    ticket.save(update_fields=['status', 'updated_at'])
            elif field == 'labor_cost':
                ticket.labor_cost = Decimal(value)
                ticket.save(update_fields=['labor_cost', 'updated_at'])
            elif field == 'technician_id':
                ticket.technician_id = value or None
                ticket.save(update_fields=['technician', 'updated_at'])
            elif field == 'notes':
                ticket.notes = value
                ticket.save(update_fields=['notes', 'updated_at'])
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Invalid method'}, status=405)


# ---------------------------------------------------------------------------
# Add Part to Ticket (with immediate stock deduction)
# ---------------------------------------------------------------------------
@login_required
def add_part(request, pk):
    branch = getattr(request, 'branch', None)
    qs = MaintenanceTicket.objects.all()
    if branch:
        qs = qs.filter(branch=branch)
    ticket = get_object_or_404(qs, pk=pk)
    if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
        return JsonResponse({'error': 'لا يمكن إضافة قطع لتذكرة مسلّمة.'}, status=400)

    if request.method == 'POST':
        try:
            product_id  = request.POST.get('product_id')
            warehouse_id = request.POST.get('warehouse_id')
            quantity    = Decimal(request.POST.get('quantity', '1'))
            selling_price = Decimal(request.POST.get('selling_price', '0'))
            serial_item_id = request.POST.get('serial_item_id') or None

            product   = get_object_or_404(Product, id=product_id)
            warehouse = get_object_or_404(Warehouse, id=warehouse_id)

            with transaction.atomic():
                part = TicketPart(
                    ticket=ticket,
                    product=product,
                    warehouse=warehouse,
                    quantity=quantity,
                    selling_price=selling_price,
                    serial_item_id=serial_item_id,
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
@login_required
def delete_part(request, part_pk):
    part = get_object_or_404(TicketPart, pk=part_pk)
    if part.ticket.status == MaintenanceTicket.STATUS_DELIVERED:
        return JsonResponse({'error': 'لا يمكن حذف قطع من تذكرة مسلّمة.'}, status=400)
    part.delete()
    return JsonResponse({'success': True})


# ---------------------------------------------------------------------------
# Deliver Ticket (post journal + close ticket)
# ---------------------------------------------------------------------------
@login_required
def deliver_ticket(request, pk):
    branch = getattr(request, 'branch', None)
    qs = MaintenanceTicket.objects.all()
    if branch:
        qs = qs.filter(branch=branch)
    ticket = get_object_or_404(qs, pk=pk)
    if ticket.status == MaintenanceTicket.STATUS_DELIVERED:
        return JsonResponse({'error': 'هذه التذكرة مسلّمة بالفعل.'}, status=400)

    if request.method == 'POST':
        treasury_id = request.POST.get('treasury_id')
        if not treasury_id:
            return JsonResponse({'error': 'يجب تحديد الخزينة عند التسليم.'}, status=400)
        try:
            treasury = Treasury.objects.get(id=treasury_id, is_active=True)
            ticket.deliver(user=request.user, treasury=treasury)
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
