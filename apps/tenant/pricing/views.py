"""
Pricing and Price Lists Views:
  - Customer Sectors management (CRUD)
  - Price Lists management (CRUD)
  - Price List Items management (add/edit/delete items with fixed price or discount %)
  - API endpoint for dynamic real-time price resolution
"""
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.db.models import Q, Count
from django.utils import timezone

from apps.tenant.core.decorators import custom_permission_required
from apps.tenant.core.access import allowed_branches, get_branch_scope
from apps.tenant.inventory.models import Product, UnitOfMeasure
from apps.tenant.partners.models import Partner
from .models import CustomerSector, PriceList, PriceListItem
from .services import PricingEngineService


# =============================================================================
# Customer Sectors (قطاعات العملاء)
# =============================================================================

@login_required
@custom_permission_required('pricing.view_customersector', redirect_url='core:main_screen')
def sector_list(request):
    """Lists customer sectors with partner counts and search filter."""
    q = request.GET.get('q', '').strip()
    sectors = CustomerSector.objects.annotate(partners_count=Count('partners')).order_by('name')
    if q:
        sectors = sectors.filter(Q(name__icontains=q) | Q(code__icontains=q))

    paginator = Paginator(sectors, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'sectors': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'search_query': q,
        'title': 'قطاعات العملاء',
    }
    return render(request, 'pricing/sector_list.html', context)


@login_required
@custom_permission_required('pricing.add_customersector', redirect_url='core:main_screen')
def sector_create(request):
    """Creates a new customer sector."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        code = request.POST.get('code', '').strip() or None
        description = request.POST.get('description', '').strip()
        is_active = request.POST.get('is_active') == 'on'

        if not name:
            messages.error(request, 'يرجى إدخال اسم القطاع.')
            return render(request, 'pricing/sector_form.html', {
                'title': 'إضافة قطاع عملاء جديد',
                'sector': {'name': name, 'code': code, 'description': description, 'is_active': is_active}
            })

        if CustomerSector.objects.filter(name=name).exists():
            messages.error(request, 'يوجد قطاع مسجل بهذا الاسم بالفعل.')
            return render(request, 'pricing/sector_form.html', {
                'title': 'إضافة قطاع عملاء جديد',
                'sector': {'name': name, 'code': code, 'description': description, 'is_active': is_active}
            })

        CustomerSector.objects.create(
            name=name,
            code=code,
            description=description,
            is_active=is_active
        )
        messages.success(request, f'تم إنشاء قطاع "{name}" بنجاح.')
        return redirect('pricing:sector_list')

    return render(request, 'pricing/sector_form.html', {
        'title': 'إضافة قطاع عملاء جديد',
        'sector': None
    })


@login_required
@custom_permission_required('pricing.change_customersector', redirect_url='core:main_screen')
def sector_edit(request, pk):
    """Edits an existing customer sector."""
    sector = get_object_or_404(CustomerSector, pk=pk)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        code = request.POST.get('code', '').strip() or None
        description = request.POST.get('description', '').strip()
        is_active = request.POST.get('is_active') == 'on'

        if not name:
            messages.error(request, 'يرجى إدخال اسم القطاع.')
            return render(request, 'pricing/sector_form.html', {'sector': sector, 'title': 'تعديل قطاع العملاء'})

        if CustomerSector.objects.filter(name=name).exclude(pk=sector.pk).exists():
            messages.error(request, 'يوجد قطاع مسجل بهذا الاسم بالفعل.')
            return render(request, 'pricing/sector_form.html', {'sector': sector, 'title': 'تعديل قطاع العملاء'})

        sector.name = name
        sector.code = code
        sector.description = description
        sector.is_active = is_active
        sector.save()

        messages.success(request, f'تم تحديث بيانات قطاع "{name}" بنجاح.')
        return redirect('pricing:sector_list')

    return render(request, 'pricing/sector_form.html', {'sector': sector, 'title': 'تعديل قطاع العملاء'})


@login_required
@custom_permission_required('pricing.delete_customersector', redirect_url='core:main_screen')
def sector_delete(request, pk):
    """Deletes a customer sector if no partners are linked."""
    sector = get_object_or_404(CustomerSector, pk=pk)
    if request.method == 'POST':
        partners_count = sector.partners.count()
        if partners_count > 0:
            messages.error(request, f'لا يمكن حذف القطاع لأنه مرتبط بعدد ({partners_count}) من العملاء. يمكنك إلغاء تفعيله بدلاً من حذفه.')
        else:
            name = sector.name
            sector.delete()
            messages.success(request, f'تم حذف القطاع "{name}" بنجاح.')
    return redirect('pricing:sector_list')


# =============================================================================
# Price Lists (قوائم الأسعار)
# =============================================================================

@login_required
@custom_permission_required('pricing.view_pricelist', redirect_url='core:main_screen')
def pricelist_list(request):
    """Lists price lists with type filtering, search, and activity status."""
    q = request.GET.get('q', '').strip()
    list_type = request.GET.get('type', '').strip()
    status = request.GET.get('status', '').strip()

    pricelists = PriceList.objects.select_related('partner', 'sector').annotate(items_count=Count('items')).order_by('-created_at')

    if q:
        pricelists = pricelists.filter(
            Q(name__icontains=q) |
            Q(code__icontains=q) |
            Q(partner__name__icontains=q) |
            Q(sector__name__icontains=q)
        )

    if list_type in [PriceList.CUSTOMER, PriceList.SECTOR, PriceList.GENERAL]:
        pricelists = pricelists.filter(list_type=list_type)

    if status == 'active':
        pricelists = pricelists.filter(is_active=True)
    elif status == 'inactive':
        pricelists = pricelists.filter(is_active=False)

    paginator = Paginator(pricelists, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'pricelists': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'search_query': q,
        'current_type': list_type,
        'current_status': status,
        'title': 'قوائم الأسعار',
    }
    return render(request, 'pricing/pricelist_list.html', context)


@login_required
@custom_permission_required('pricing.add_pricelist', redirect_url='core:main_screen')
def pricelist_create(request):
    """Creates a new Price List header."""
    partners = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH'], is_active=True).order_by('name')
    sectors = CustomerSector.objects.filter(is_active=True).order_by('name')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        code = request.POST.get('code', '').strip() or None
        list_type = request.POST.get('list_type', PriceList.SECTOR)
        partner_id = request.POST.get('partner')
        sector_id = request.POST.get('sector')
        start_date_str = request.POST.get('start_date', '').strip()
        end_date_str = request.POST.get('end_date', '').strip()
        is_active = request.POST.get('is_active') == 'on'
        notes = request.POST.get('notes', '').strip()

        if not name:
            messages.error(request, 'يرجى إدخال اسم قائمة الأسعار.')
            return render(request, 'pricing/pricelist_form.html', {
                'title': 'إنشاء قائمة أسعار جديدة',
                'partners': partners,
                'sectors': sectors,
                'pricelist': {'name': name, 'code': code, 'list_type': list_type, 'notes': notes, 'is_active': is_active}
            })

        partner = None
        sector = None
        if list_type == PriceList.CUSTOMER:
            if not partner_id:
                messages.error(request, 'يجب تحديد العميل لقوائم الأسعار المخصصة لعميل.')
                return render(request, 'pricing/pricelist_form.html', {
                    'title': 'إنشاء قائمة أسعار جديدة',
                    'partners': partners,
                    'sectors': sectors,
                    'pricelist': {'name': name, 'code': code, 'list_type': list_type, 'notes': notes, 'is_active': is_active}
                })
            partner = get_object_or_404(Partner, pk=partner_id)

        elif list_type == PriceList.SECTOR:
            if not sector_id:
                messages.error(request, 'يجب تحديد القطاع لقوائم أسعار القطاعات.')
                return render(request, 'pricing/pricelist_form.html', {
                    'title': 'إنشاء قائمة أسعار جديدة',
                    'partners': partners,
                    'sectors': sectors,
                    'pricelist': {'name': name, 'code': code, 'list_type': list_type, 'notes': notes, 'is_active': is_active}
                })
            sector = get_object_or_404(CustomerSector, pk=sector_id)

        import datetime
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        if end_date_str:
            try:
                end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        if start_date and end_date and end_date < start_date:
            messages.error(request, 'تاريخ انتهاء السريان لا يمكن أن يكون قبل تاريخ البدء.')
            return render(request, 'pricing/pricelist_form.html', {
                'title': 'إنشاء قائمة أسعار جديدة',
                'partners': partners,
                'sectors': sectors,
                'pricelist': {'name': name, 'code': code, 'list_type': list_type, 'notes': notes, 'is_active': is_active}
            })

        pricelist = PriceList.objects.create(
            name=name,
            code=code,
            list_type=list_type,
            partner=partner,
            sector=sector,
            start_date=start_date,
            end_date=end_date,
            is_active=is_active,
            notes=notes
        )
        messages.success(request, f'تم إنشاء قائمة الأسعار "{name}" بنجاح. يمكنك الآن إضافة بنود الأصناف والأسعار.')
        return redirect('pricing:pricelist_detail', pk=pricelist.pk)

    return render(request, 'pricing/pricelist_form.html', {
        'title': 'إنشاء قائمة أسعار جديدة',
        'partners': partners,
        'sectors': sectors,
        'pricelist': None
    })


@login_required
@custom_permission_required('pricing.view_pricelist', redirect_url='core:main_screen')
def pricelist_detail(request, pk):
    """
    Detailed view for a Price List:
    Shows all items, supports adding new item with live search,
    updating price/discount %, and removing items.
    """
    pricelist = get_object_or_404(PriceList.objects.select_related('partner', 'sector'), pk=pk)
    items = pricelist.items.select_related('product', 'uom').order_by('product__name')

    # Available products for quick addition
    products = Product.objects.filter(is_active=True).order_by('name')[:300]
    uoms = UnitOfMeasure.objects.all().order_by('name')

    context = {
        'pricelist': pricelist,
        'items': items,
        'items_count': items.count(),
        'products': products,
        'uoms': uoms,
        'title': f'بنود قائمة: {pricelist.name}',
    }
    return render(request, 'pricing/pricelist_detail.html', context)


@login_required
@custom_permission_required('pricing.change_pricelist', redirect_url='core:main_screen')
def pricelist_edit(request, pk):
    """Edits header information of a Price List."""
    pricelist = get_object_or_404(PriceList, pk=pk)
    partners = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH'], is_active=True).order_by('name')
    sectors = CustomerSector.objects.filter(is_active=True).order_by('name')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        code = request.POST.get('code', '').strip() or None
        list_type = request.POST.get('list_type', pricelist.list_type)
        partner_id = request.POST.get('partner')
        sector_id = request.POST.get('sector')
        start_date_str = request.POST.get('start_date', '').strip()
        end_date_str = request.POST.get('end_date', '').strip()
        is_active = request.POST.get('is_active') == 'on'
        notes = request.POST.get('notes', '').strip()

        if not name:
            messages.error(request, 'يرجى إدخال اسم قائمة الأسعار.')
            return render(request, 'pricing/pricelist_form.html', {
                'pricelist': pricelist,
                'title': 'تعديل قائمة الأسعار',
                'partners': partners,
                'sectors': sectors,
            })

        partner = None
        sector = None
        if list_type == PriceList.CUSTOMER:
            if not partner_id:
                messages.error(request, 'يجب تحديد العميل لقوائم الأسعار المخصصة لعميل.')
                return render(request, 'pricing/pricelist_form.html', {
                    'pricelist': pricelist,
                    'title': 'تعديل قائمة الأسعار',
                    'partners': partners,
                    'sectors': sectors,
                })
            partner = get_object_or_404(Partner, pk=partner_id)

        elif list_type == PriceList.SECTOR:
            if not sector_id:
                messages.error(request, 'يجب تحديد القطاع لقوائم أسعار القطاعات.')
                return render(request, 'pricing/pricelist_form.html', {
                    'pricelist': pricelist,
                    'title': 'تعديل قائمة الأسعار',
                    'partners': partners,
                    'sectors': sectors,
                })
            sector = get_object_or_404(CustomerSector, pk=sector_id)

        import datetime
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        if end_date_str:
            try:
                end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        if start_date and end_date and end_date < start_date:
            messages.error(request, 'تاريخ انتهاء السريان لا يمكن أن يكون قبل تاريخ البدء.')
            return render(request, 'pricing/pricelist_form.html', {
                'pricelist': pricelist,
                'title': 'تعديل قائمة الأسعار',
                'partners': partners,
                'sectors': sectors,
            })

        pricelist.name = name
        pricelist.code = code
        pricelist.list_type = list_type
        pricelist.partner = partner
        pricelist.sector = sector
        pricelist.start_date = start_date
        pricelist.end_date = end_date
        pricelist.is_active = is_active
        pricelist.notes = notes
        pricelist.save()

        messages.success(request, f'تم تحديث قائمة الأسعار "{name}" بنجاح.')
        return redirect('pricing:pricelist_detail', pk=pricelist.pk)

    return render(request, 'pricing/pricelist_form.html', {
        'pricelist': pricelist,
        'title': 'تعديل قائمة الأسعار',
        'partners': partners,
        'sectors': sectors,
    })


@login_required
@custom_permission_required('pricing.delete_pricelist', redirect_url='core:main_screen')
def pricelist_delete(request, pk):
    """Deletes a price list and its items."""
    pricelist = get_object_or_404(PriceList, pk=pk)
    if request.method == 'POST':
        name = pricelist.name
        pricelist.delete()
        messages.success(request, f'تم حذف قائمة الأسعار "{name}" بنجاح.')
    return redirect('pricing:pricelist_list')


# =============================================================================
# Price List Items Management
# =============================================================================

@login_required
@custom_permission_required('pricing.change_pricelist', redirect_url='core:main_screen')
def pricelist_item_add(request, pk):
    """Adds or updates an item in a Price List."""
    pricelist = get_object_or_404(PriceList, pk=pk)
    if request.method == 'POST':
        product_id = request.POST.get('product')
        uom_id = request.POST.get('uom') or None
        pricing_method = request.POST.get('pricing_method', PriceListItem.FIXED)
        custom_price_str = request.POST.get('custom_price', '').strip()
        discount_pct_str = request.POST.get('discount_percentage', '').strip()
        min_qty_str = request.POST.get('min_quantity', '1').strip()

        product = get_object_or_404(Product, pk=product_id)
        uom = get_object_or_404(UnitOfMeasure, pk=uom_id) if uom_id else None

        try:
            min_qty = Decimal(min_qty_str) if min_qty_str else Decimal('1')
            if min_qty <= 0:
                min_qty = Decimal('1')
        except InvalidOperation:
            min_qty = Decimal('1')

        custom_price = None
        discount_pct = None

        if pricing_method == PriceListItem.FIXED:
            try:
                custom_price = Decimal(custom_price_str)
                if custom_price < 0:
                    messages.error(request, 'السعر لا يمكن أن يكون سالباً.')
                    return redirect('pricing:pricelist_detail', pk=pricelist.pk)
            except InvalidOperation:
                messages.error(request, 'يرجى إدخال سعر مخصص صحيح.')
                return redirect('pricing:pricelist_detail', pk=pricelist.pk)
        else:
            try:
                discount_pct = Decimal(discount_pct_str)
                if discount_pct < 0 or discount_pct > 100:
                    messages.error(request, 'نسبة الخصم يجب أن تكون بين 0 و 100%.')
                    return redirect('pricing:pricelist_detail', pk=pricelist.pk)
            except InvalidOperation:
                messages.error(request, 'يرجى إدخال نسبة خصم صحيحة.')
                return redirect('pricing:pricelist_detail', pk=pricelist.pk)

        # Check if item with same (product, uom, min_quantity) already exists -> update it
        item, created = PriceListItem.objects.update_or_create(
            price_list=pricelist,
            product=product,
            uom=uom,
            min_quantity=min_qty,
            defaults={
                'pricing_method': pricing_method,
                'custom_price': custom_price,
                'discount_percentage': discount_pct,
            }
        )

        if created:
            messages.success(request, f'تم إضافة الصنف "{product.name}" إلى قائمة الأسعار بنجاح.')
        else:
            messages.success(request, f'تم تحديث تسعير الصنف "{product.name}" بنجاح.')

    return redirect('pricing:pricelist_detail', pk=pricelist.pk)


@login_required
@custom_permission_required('pricing.change_pricelist', redirect_url='core:main_screen')
def pricelist_item_delete(request, pk, item_id):
    """Deletes an item from a Price List."""
    pricelist = get_object_or_404(PriceList, pk=pk)
    item = get_object_or_404(PriceListItem, pk=item_id, price_list=pricelist)
    if request.method == 'POST':
        product_name = item.product.name
        item.delete()
        messages.success(request, f'تم حذف الصنف "{product_name}" من قائمة الأسعار.')
    return redirect('pricing:pricelist_detail', pk=pricelist.pk)


# =============================================================================
# Price Resolution API (للاستعلام الحي من الفواتير وشاشات البيع)
# =============================================================================

@login_required
def api_resolve_price(request):
    """
    API endpoint to resolve the effective price for a product based on customer and quantity.
    GET params:
      - product_id (int)
      - partner_id (int, optional)
      - qty (float/decimal, default 1)
      - uom_id (int, optional)
    """
    product_id = request.GET.get('product_id')
    partner_id = request.GET.get('partner_id')
    qty_str = request.GET.get('qty', '1')
    uom_id = request.GET.get('uom_id')

    if not product_id:
        return JsonResponse({'success': False, 'error': 'product_id is required'}, status=400)

    try:
        product = Product.objects.get(pk=product_id, is_active=True)
    except Product.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Product not found'}, status=404)

    partner = None
    if partner_id:
        try:
            partner = Partner.objects.select_related('sector').get(pk=partner_id)
        except Partner.DoesNotExist:
            partner = None

    try:
        qty = Decimal(str(qty_str))
    except InvalidOperation:
        qty = Decimal('1')

    uom = None
    if uom_id:
        try:
            uom = UnitOfMeasure.objects.get(pk=uom_id)
        except UnitOfMeasure.DoesNotExist:
            uom = None

    current_branch = get_branch_scope(request)
    result = PricingEngineService.get_effective_price(
        product=product,
        partner=partner,
        branch=current_branch,
        uom=uom,
        quantity=qty
    )

    return JsonResponse({
        'success': True,
        'product_id': product.id,
        'product_name': product.name,
        'price': str(result['price']),
        'original_price': str(result['original_price']),
        'tier': result['tier'],
        'tier_display': result['tier_display'],
        'price_list_name': result['price_list_name'],
        'discount_amount': str(result['discount_amount']),
        'discount_percentage': str(result['discount_percentage']),
        'badge_color': result['badge_color'],
        'badge_text': result['badge_text'],
        'badge_class': result['badge_class'],
        # explicit unit semantics (new keys)
        'unit_price': str(result.get('unit_price', result['price'])),
        'base_unit_price': str(result.get('base_unit_price', result['price'])),
        'uom_factor': str(result.get('uom_factor', 1)),
        'price_is_per_uom': bool(result.get('price_is_per_uom', False)),
    })


@login_required
def api_customer_prices(request):
    """
    Returns a JSON mapping of product IDs to their custom prices for a given customer.
    Used by POS to instantly update product table prices and cart items when a customer is chosen.
    """
    partner_id = request.GET.get('partner_id')
    branch_id = request.GET.get('branch_id')

    if not partner_id or partner_id in ['null', 'undefined', 'None', '']:
        return JsonResponse({'success': True, 'prices': {}})

    try:
        partner = Partner.objects.select_related('sector').get(pk=int(partner_id))
    except (Partner.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'success': True, 'prices': {}})

    branch = None
    if branch_id and branch_id not in ['null', 'undefined', 'None', '']:
        try:
            # Only branches this user may access (non-admins: their own branch).
            branch = allowed_branches(request).filter(pk=int(branch_id)).first()
        except (ValueError, TypeError):
            branch = None
    if not branch:
        branch = get_branch_scope(request)

    tiers = PricingEngineService.get_customer_price_tiers(partner=partner, branch=branch)
    prices_map = PricingEngineService.get_customer_price_map(partner=partner, branch=branch, tiers=tiers)
    return JsonResponse({
        'success': True,
        'partner_id': partner.id,
        'partner_name': partner.name,
        'sector_name': partner.sector.name if getattr(partner, 'sector', None) else '',
        # 'prices': legacy map, price for qty 1 of the base unit
        'prices': prices_map,
        # 'tiers': product_id -> all quantity-break / unit-specific tiers (see PricingEngineService)
        'tiers': tiers,
    })
