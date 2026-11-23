from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from .models import Product, InventoryBatch, StockMovement, Warehouse
from apps.tenant.core.decorators import custom_permission_required
from apps.tenant.core.models import SystemSetting

@login_required
@custom_permission_required('inventory.view_inventorybatch', redirect_url='core:main_screen')
def stock_list(request):
    from django.core.paginator import Paginator
    batches = InventoryBatch.objects.filter(quantity_remaining__gt=0).select_related('product', 'warehouse').order_by('-created_at')
    warehouses = Warehouse.objects.filter(is_active=True)
    
    warehouse_filter = request.GET.get('warehouse', '').strip()
    product_search = request.GET.get('q', '').strip()
    
    if warehouse_filter:
        batches = batches.filter(warehouse__name=warehouse_filter)
    if product_search:
        batches = batches.filter(product__name__icontains=product_search)
        
    paginator = Paginator(batches, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'batches': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
        'warehouses': warehouses,
        'selected_warehouse': warehouse_filter,
        'search_query': product_search,
        'title': 'تتبع المخزون (FIFO)',
    }
    return render(request, 'inventory/stock.html', context)

from django.db.models import Q

@login_required
@custom_permission_required('inventory.view_product', redirect_url='core:main_screen')
def product_list(request):
    from django.core.paginator import Paginator
    from django.db.models import Sum
    query = request.GET.get('q', '').strip()
    products = Product.objects.select_related('category').order_by('-id')
    
    if query:
        products = products.filter(
            Q(name__icontains=query) | 
            Q(barcode__icontains=query)
        )
        
    current_branch = getattr(request, 'branch', None)
    
    paginator = Paginator(products, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # تحسين استعلام المخزون للـ 25 صنف دفعة واحدة لتفادي 25 استعلاماً بطيئاً
    p_ids = [p.id for p in page_obj]
    stock_map = dict(
        InventoryBatch.objects.filter(product_id__in=p_ids)
        .values('product_id')
        .annotate(total=Sum('quantity_remaining'))
        .values_list('product_id', 'total')
    )
    for p in page_obj:
        p.sale_price = p.get_price_for_branch(current_branch)
        p.pos_stock = stock_map.get(p.id, 0)
        
    settings = SystemSetting.get_settings()
    context = {
        'products': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
        'title': 'المنتجات',
        'search_query': query,
        'store_name': getattr(settings, 'company_name', '') or 'مسار',
    }
    return render(request, 'inventory/products.html', context)

@login_required
@custom_permission_required('inventory.change_inventorybatch', redirect_url='core:main_screen')
def transfer_stock(request):
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        from_wh_id = request.POST.get('from_warehouse')
        to_wh_id = request.POST.get('to_warehouse')
        quantity = request.POST.get('quantity')
        
        from apps.tenant.services.fifo_engine import transfer_stock as ts
        from decimal import Decimal, InvalidOperation
        from django.contrib import messages
        
        try:
            qty = Decimal(quantity)
            product = Product.objects.get(id=product_id)
            from_wh = Warehouse.objects.get(id=from_wh_id)
            to_wh = Warehouse.objects.get(id=to_wh_id)
            
            ts(product, from_wh, to_wh, qty)
            messages.success(request, 'تم تحويل المخزون بنجاح')
            return redirect('inventory:stock_list')
        except Exception as e:
            messages.error(request, str(e))
    products = Product.objects.filter(is_active=True)
    warehouses = Warehouse.objects.filter(is_active=True)
    context = {'products': products, 'warehouses': warehouses, 'title': 'تحويل مخزني'}
    return render(request, 'inventory/transfer.html', context)

@login_required
def api_stock_level(request):
    product_id = request.GET.get('product_id')
    warehouse_id = request.GET.get('warehouse_id')
    
    try:
        product = Product.objects.get(id=product_id)
        wh = Warehouse.objects.get(id=warehouse_id) if warehouse_id else None
        return JsonResponse({'stock': str(product.get_stock(wh))})
    except Product.DoesNotExist:
        return JsonResponse({'error': 'Product not found'}, status=404)

@login_required
@custom_permission_required('inventory.view_inventorybatch', redirect_url='core:main_screen')
def valuation_report(request):
    from django.db.models import Sum, F
    from django.db.models.functions import Coalesce
    from decimal import Decimal

    batches_qs = InventoryBatch.objects.filter(quantity_remaining__gt=0).select_related('product', 'warehouse')
    total_value = batches_qs.aggregate(
        val=Coalesce(Sum(F('quantity_remaining') * F('unit_cost')), Decimal('0.00'))
    )['val']

    context = {'batches': batches_qs, 'total_value': total_value, 'title': 'تقرير تقييم المخزون'}
    return render(request, 'inventory/valuation_report.html', context)

@login_required
@custom_permission_required('inventory.view_inventorybatch', redirect_url='core:main_screen')
def expiry_report(request):
    import datetime
    today = datetime.date.today()
    warning_date = today + datetime.timedelta(days=30)
    batches = InventoryBatch.objects.filter(quantity_remaining__gt=0, expiry_date__lte=warning_date).select_related('product', 'warehouse').order_by('expiry_date')
    context = {'batches': batches, 'title': 'تقرير الصلاحية والتوالف'}
    return render(request, 'inventory/expiry_report.html', context)


from django.views.generic import ListView, CreateView, UpdateView
from django.urls import reverse_lazy
from django.contrib.auth.mixins import LoginRequiredMixin
from apps.tenant.core.mixins import CustomPermissionRequiredMixin
from .models import Warehouse, Category, Product, UnitOfMeasure
from .forms import UnitOfMeasureForm

# --- Warehouse Generic Views ---
class WarehouseListView(CustomPermissionRequiredMixin, ListView):
    model = Warehouse
    template_name = 'core/generic_list.html'
    context_object_name = 'objects'
    permission_required = 'inventory.view_warehouse'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إدارة المستودعات'
        context['create_url'] = reverse_lazy('inventory:warehouse_create')
        context['update_url_name'] = 'inventory:warehouse_update'
        context['delete_url_name'] = 'inventory:warehouse_delete'
        return context

class WarehouseCreateView(CustomPermissionRequiredMixin, CreateView):
    model = Warehouse
    fields = ['branch', 'name', 'code', 'is_cold_storage', 'is_active', 'description']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('inventory:warehouse_list')
    permission_required = 'inventory.add_warehouse'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة مستودع جديد'
        context['cancel_url'] = reverse_lazy('inventory:warehouse_list')
        return context

class WarehouseUpdateView(CustomPermissionRequiredMixin, UpdateView):
    model = Warehouse
    fields = ['branch', 'name', 'code', 'is_cold_storage', 'is_active', 'description']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('inventory:warehouse_list')
    permission_required = 'inventory.change_warehouse'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل مستودع'
        context['cancel_url'] = reverse_lazy('inventory:warehouse_list')
        return context

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.db.models import ProtectedError

@login_required
@custom_permission_required('inventory.delete_warehouse', redirect_url='core:main_screen')
def warehouse_delete(request, pk):
    from .models import Warehouse
    if request.method == 'POST':
        warehouse = get_object_or_404(Warehouse, pk=pk)
        try:
            name = warehouse.name
            warehouse.delete()
            messages.success(request, f'تم حذف المستودع "{name}" بنجاح.')
        except ProtectedError:
            messages.error(request, f'لا يمكن حذف المستودع "{warehouse.name}" لارتباطه ببيانات أخرى (مثل بضاعة أو فواتير).')
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء الحذف: {str(e)}')
    return redirect('inventory:warehouse_list')


# --- Category Generic Views ---
class CategoryListView(CustomPermissionRequiredMixin, ListView):
    model = Category
    template_name = 'core/generic_list.html'
    context_object_name = 'objects'
    permission_required = 'inventory.view_category'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إدارة فئات المنتجات'
        context['create_url'] = reverse_lazy('inventory:category_create')
        context['update_url_name'] = 'inventory:category_update'
        return context

class CategoryCreateView(CustomPermissionRequiredMixin, CreateView):
    model = Category
    fields = ['name', 'parent', 'is_active', 'description']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('inventory:category_list')
    permission_required = 'inventory.add_category'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة فئة جديدة'
        context['cancel_url'] = reverse_lazy('inventory:category_list')
        return context

class CategoryUpdateView(CustomPermissionRequiredMixin, UpdateView):
    model = Category
    fields = ['name', 'parent', 'is_active', 'description']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('inventory:category_list')
    permission_required = 'inventory.change_category'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل فئة'
        context['cancel_url'] = reverse_lazy('inventory:category_list')
        return context


from .forms import ProductForm, ProductUoMFormSet
from django.http import HttpResponse

@login_required
@custom_permission_required('inventory.add_product', redirect_url='core:main_screen')
def product_template_download(request):
    """
    تنزيل قالب إكسيل استرشادي ومتوافق تماماً لإدخال المنتجات مجمعة.
    الباركود اختياري تماماً.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "قالب المنتجات"
    
    # اتجاه الصفحة من اليمين لليسار للعربية
    ws.views.sheetView[0].rightToLeft = True

    header_fill = PatternFill(start_color="FF7300", end_color="FF7300", fill_type="solid")
    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    
    sample_font = Font(name="Segoe UI", size=10, italic=True, color="444444")
    sample_fill = PatternFill(start_color="FFF8F0", end_color="FFF8F0", fill_type="solid")

    thin_border = Border(
        left=Side(style='thin', color='DDDDDD'),
        right=Side(style='thin', color='DDDDDD'),
        top=Side(style='thin', color='DDDDDD'),
        bottom=Side(style='thin', color='DDDDDD')
    )
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    right_align = Alignment(horizontal="right", vertical="center")

    headers = [
        "اسم المنتج *",
        "الباركود (اختياري)",
        "سعر البيع *",
        "سعر التكلفة (اختياري)",
        "الكمية (اختياري)",
        "القسم / الفئة (اختياري)",
        "وحدة القياس (اختياري)",
        "حد إعادة الطلب (اختياري)"
    ]

    ws.append(headers)
    ws.row_dimensions[1].height = 28

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border

    # صف توضيحي 1: منتج بباركود
    sample_row_1 = [
        "أرز بسمتي فاخر 1 كجم",
        "6221234567890",
        55.00,
        42.50,
        100,
        "بقوليات وأرز",
        "كيلو",
        15
    ]
    # صف توضيحي 2: منتج بدون باركود (الخانة فارغة تماماً للتأكيد على أن الباركود اختياري)
    sample_row_2 = [
        "كيك شوكولاتة يدوي (بدون باركود)",
        "",  # باركود فارغ تماماً
        20.00,
        14.00,
        35,
        "حلويات ومخبوزات",
        "قطعة",
        5
    ]

    for row_data in [sample_row_1, sample_row_2]:
        ws.append(row_data)
        current_row = ws.max_row
        ws.row_dimensions[current_row].height = 22
        for col_num in range(1, len(row_data) + 1):
            cell = ws.cell(row=current_row, column=col_num)
            cell.font = sample_font
            cell.fill = sample_fill
            cell.border = thin_border
            if col_num in [1, 6]:
                cell.alignment = right_align
            else:
                cell.alignment = center_align

    col_widths = {
        'A': 32, # اسم المنتج
        'B': 24, # الباركود
        'C': 16, # سعر البيع
        'D': 20, # سعر التكلفة
        'E': 16, # الكمية
        'F': 22, # القسم
        'G': 18, # وحدة القياس
        'H': 22  # حد إعادة الطلب
    }
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="products_template.xlsx"'
    wb.save(response)
    return response


@login_required
@custom_permission_required('inventory.add_product', redirect_url='core:main_screen')
def product_bulk_import(request):
    """
    شاشة ومعالجة استيراد المنتجات مجمعة من ملف إكسيل.
    الباركود اختياري تماماً.
    """
    warehouses = Warehouse.objects.filter(is_active=True)

    if request.method == 'POST':
        import openpyxl
        from decimal import Decimal, InvalidOperation
        import datetime
        from apps.tenant.services.fifo_engine import add_inventory_batch
        from apps.tenant.services.journal_service import _get_system_account
        from apps.tenant.accounting.models import JournalEntry, JournalItem

        excel_file = request.FILES.get('excel_file')
        warehouse_id = request.POST.get('warehouse')
        duplicate_action = request.POST.get('duplicate_action', 'update')  # 'update' or 'skip'

        if not excel_file:
            messages.error(request, 'يرجى اختيار ملف إكسيل (.xlsx أو .xls) لرفعه.')
            return render(request, 'inventory/product_bulk_import.html', {'warehouses': warehouses, 'title': 'استيراد المنتجات من إكسيل'})

        if not excel_file.name.endswith(('.xlsx', '.xls')):
            messages.error(request, 'صيغة الملف غير مدعومة. يرجى رفع ملف إكسيل بصيغة .xlsx أو .xls')
            return render(request, 'inventory/product_bulk_import.html', {'warehouses': warehouses, 'title': 'استيراد المنتجات من إكسيل'})

        warehouse = None
        if warehouse_id:
            try:
                warehouse = Warehouse.objects.get(id=warehouse_id, is_active=True)
            except Warehouse.DoesNotExist:
                warehouse = None

        try:
            wb = openpyxl.load_workbook(excel_file, data_only=True)
            ws = wb.active
        except Exception as e:
            messages.error(request, f'تعذر قراءة ملف الإكسيل: {str(e)}')
            return render(request, 'inventory/product_bulk_import.html', {'warehouses': warehouses, 'title': 'استيراد المنتجات من إكسيل'})

        UNIT_MAP = {
            'قطعة': 'PIECE', 'قطعه': 'PIECE', 'حبة': 'PIECE', 'حبه': 'PIECE', 'piece': 'PIECE',
            'كيلو': 'KG', 'كجم': 'KG', 'كيلوجرام': 'KG', 'كيلو جرام': 'KG', 'kg': 'KG',
            'كرتونة': 'BOX', 'كرتونه': 'BOX', 'كرتون': 'BOX', 'box': 'BOX',
            'لتر': 'LITER', 'liter': 'LITER',
            'دستة': 'DOZEN', 'دسته': 'DOZEN', 'dozen': 'DOZEN',
            'جرام': 'GRAM', 'جم': 'GRAM', 'gram': 'GRAM', 'g': 'GRAM',
            'متر': 'METER', 'meter': 'METER', 'm': 'METER',
            'عبوة': 'PACK', 'عبوه': 'PACK', 'باكت': 'PACK', 'pack': 'PACK',
        }

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            messages.error(request, 'ملف الإكسيل فارغ ولا يحتوي على أي بيانات.')
            return render(request, 'inventory/product_bulk_import.html', {'warehouses': warehouses, 'title': 'استيراد المنتجات من إكسيل'})

        header_row_idx = -1
        col_map = {}
        for idx, r in enumerate(rows):
            r_str = [str(c).strip() if c is not None else '' for c in r]
            has_name = any('اسم' in s or 'name' in s.lower() for s in r_str)
            has_price = any('سعر' in s or 'price' in s.lower() for s in r_str)
            if has_name and has_price:
                header_row_idx = idx
                for col_idx, h in enumerate(r_str):
                    h_lower = h.lower()
                    if 'اسم' in h or 'product' in h_lower or 'item' in h_lower:
                        col_map['name'] = col_idx
                    elif 'باركود' in h or 'barcode' in h_lower:
                        col_map['barcode'] = col_idx
                    elif 'بيع' in h or ('سعر' in h and 'تكلفة' not in h and 'شراء' not in h) or 'sale' in h_lower:
                        col_map['sale_price'] = col_idx
                    elif 'تكلفة' in h or 'شراء' in h or 'cost' in h_lower:
                        col_map['cost_price'] = col_idx
                    elif 'كمية' in h or 'رصيد' in h or 'qty' in h_lower or 'quantity' in h_lower:
                        col_map['quantity'] = col_idx
                    elif 'قسم' in h or 'فئة' in h or 'category' in h_lower:
                        col_map['category'] = col_idx
                    elif 'وحدة' in h or 'unit' in h_lower:
                        col_map['unit'] = col_idx
                    elif 'حد' in h or 'min' in h_lower:
                        col_map['min_stock'] = col_idx
                break

        if header_row_idx == -1 or 'name' not in col_map:
            col_map = {
                'name': 0,
                'barcode': 1,
                'sale_price': 2,
                'cost_price': 3,
                'quantity': 4,
                'category': 5,
                'unit': 6,
                'min_stock': 7
            }
            header_row_idx = 0

        created_count = 0
        updated_count = 0
        skipped_count = 0
        zeroed_negative_qty_count = 0
        errors = []
        total_opening_cost = Decimal('0')
        seen_barcodes_in_sheet = set()
        seen_names_in_sheet = set()
        sample_names = ["أرز بسمتي فاخر 1 كجم", "كيك شوكولاتة يدوي (بدون باركود)"]

        def normalize_name(text):
            if not text:
                return ''
            s = ' '.join(str(text).strip().split())
            return s.replace('\u200b', '').replace('\xa0', ' ').strip()

        # كاش الفئات لتسريع المعالجة الفائقة لآلاف الأصناف
        category_cache = {normalize_name(c.name): c for c in Category.objects.all()}

        # استخراج الباركودات والأسماء دفعة واحدة وتجزئة الاستعلامات لتفادي حدود قاعدة البيانات
        raw_sheet_barcodes = []
        raw_sheet_names = []
        b_idx = col_map.get('barcode')
        n_idx = col_map.get('name')

        for r in rows[header_row_idx + 1:]:
            if not r:
                continue
            if b_idx is not None and b_idx < len(r) and r[b_idx] is not None:
                b_val = str(r[b_idx]).strip()
                if b_val and b_val.lower() not in ('none', 'null', 'nan', '', '-'):
                    if b_val.endswith('.0') and b_val[:-2].isdigit():
                        b_val = b_val[:-2]
                    raw_sheet_barcodes.append(b_val)
            if n_idx is not None and n_idx < len(r) and r[n_idx] is not None:
                n_val = normalize_name(r[n_idx])
                if n_val:
                    raw_sheet_names.append(n_val)

        # إزالة التكرار من القوائم للبحث السريع
        sheet_barcodes = list(dict.fromkeys(raw_sheet_barcodes))
        sheet_names = list(dict.fromkeys(raw_sheet_names))

        existing_products_by_barcode = {}
        existing_products_by_name = {}

        CHUNK_LIMIT = 500
        for i in range(0, len(sheet_barcodes), CHUNK_LIMIT):
            chunk = sheet_barcodes[i:i + CHUNK_LIMIT]
            for p in Product.objects.filter(barcode__in=chunk):
                if p.barcode:
                    existing_products_by_barcode[p.barcode.strip()] = p
                p_norm = normalize_name(p.name)
                if p_norm and p_norm not in existing_products_by_name:
                    existing_products_by_name[p_norm] = p

        for i in range(0, len(sheet_names), CHUNK_LIMIT):
            chunk = sheet_names[i:i + CHUNK_LIMIT]
            for p in Product.objects.filter(name__in=chunk):
                p_norm = normalize_name(p.name)
                if p_norm and p_norm not in existing_products_by_name:
                    existing_products_by_name[p_norm] = p
                if p.barcode and p.barcode.strip() not in existing_products_by_barcode:
                    existing_products_by_barcode[p.barcode.strip()] = p

        chunk_size = 500
        raw_rows = list(enumerate(rows[header_row_idx + 1:], start=header_row_idx + 2))

        for chunk_start in range(0, len(raw_rows), chunk_size):
            chunk = raw_rows[chunk_start:chunk_start + chunk_size]
            with transaction.atomic():
                for row_num, row_data in chunk:
                    if not row_data or all(c is None or str(c).strip() == '' for c in row_data):
                        continue
    
                    def get_col(field_key):
                        col_i = col_map.get(field_key)
                        if col_i is not None and col_i < len(row_data):
                            return row_data[col_i]
                        return None
    
                    raw_name = get_col('name')
                    if not raw_name or str(raw_name).strip() == '':
                        continue
                    name = str(raw_name).strip()
                    norm_name = normalize_name(name)
                    if not norm_name:
                        continue
    
                    # تخطي الصفوف الاسترشادية التوضيحية لو لم يحذفها المستخدم
                    if name in sample_names and row_num <= 4:
                        continue
    
                    # الباركود: اختياري تماماً
                    raw_barcode = get_col('barcode')
                    barcode = None
                    if raw_barcode is not None:
                        b_str = str(raw_barcode).strip()
                        if b_str and b_str.lower() not in ('none', 'null', 'nan', '', '-'):
                            if b_str.endswith('.0') and b_str[:-2].isdigit():
                                b_str = b_str[:-2]
                            barcode = b_str
    
                    # سعر البيع
                    raw_sale = get_col('sale_price')
                    try:
                        sale_price = Decimal(str(raw_sale).replace(',', '').strip()) if raw_sale not in (None, '') else Decimal('0')
                        if sale_price < Decimal('0'):
                            sale_price = Decimal('0')
                    except (InvalidOperation, ValueError):
                        sale_price = Decimal('0')
    
                    # سعر التكلفة
                    raw_cost = get_col('cost_price')
                    try:
                        cost_price = Decimal(str(raw_cost).replace(',', '').strip()) if raw_cost not in (None, '') else Decimal('0')
                        if cost_price < Decimal('0'):
                            cost_price = Decimal('0')
                    except (InvalidOperation, ValueError):
                        cost_price = Decimal('0')
    
                    # الكمية (مع حماية تصفير الأرصدة السالبة تلقائياً)
                    raw_qty = get_col('quantity')
                    quantity = Decimal('0')
                    if raw_qty not in (None, ''):
                        try:
                            clean_q = str(raw_qty).replace(',', '').strip()
                            if clean_q.startswith('(') and clean_q.endswith(')'):
                                clean_q = '-' + clean_q[1:-1].strip()
                            parsed_qty = Decimal(clean_q)
                            if parsed_qty < Decimal('0'):
                                quantity = Decimal('0')
                                zeroed_negative_qty_count += 1
                            else:
                                quantity = parsed_qty
                        except (InvalidOperation, ValueError):
                            quantity = Decimal('0')
    
                    # الحد الأدنى
                    raw_min = get_col('min_stock')
                    try:
                        min_stock = Decimal(str(raw_min).replace(',', '').strip()) if raw_min not in (None, '') else Decimal('0')
                        if min_stock < Decimal('0'):
                            min_stock = Decimal('0')
                    except (InvalidOperation, ValueError):
                        min_stock = Decimal('0')
    
                    # القسم (باستخدام الكاش لتسريع الأداء)
                    raw_cat = get_col('category')
                    cat_name = str(raw_cat).strip() if raw_cat not in (None, '') else 'عام'
                    norm_cat_name = normalize_name(cat_name)
                    if norm_cat_name in category_cache:
                        category = category_cache[norm_cat_name]
                    else:
                        category, _ = Category.objects.get_or_create(name=cat_name)
                        category_cache[norm_cat_name] = category
    
                    # الوحدة
                    raw_unit = get_col('unit')
                    unit_code = 'PIECE'
                    if raw_unit not in (None, ''):
                        clean_u = str(raw_unit).strip().lower()
                        unit_code = UNIT_MAP.get(clean_u, 'PIECE')
    
                    try:
                        # مطابقة الصنف: أولاً بالباركود، وثانياً باسم الصنف المطابق
                        existing_product = None
                        if barcode and barcode in existing_products_by_barcode:
                            existing_product = existing_products_by_barcode[barcode]
                        if not existing_product and norm_name in existing_products_by_name:
                            existing_product = existing_products_by_name[norm_name]
    
                        if existing_product:
                            if duplicate_action == 'update':
                                update_fields = ['updated_at']
                                if sale_price > Decimal('0'):
                                    existing_product.sale_price = sale_price
                                    update_fields.append('sale_price')
                                if cost_price > Decimal('0'):
                                    existing_product.last_purchase_price = cost_price
                                    update_fields.append('last_purchase_price')
                                if min_stock > Decimal('0'):
                                    existing_product.min_stock_level = min_stock
                                    update_fields.append('min_stock_level')
                                if barcode and not existing_product.barcode:
                                    existing_product.barcode = barcode
                                    update_fields.append('barcode')
                                    existing_products_by_barcode[barcode] = existing_product
                                if category and not existing_product.category:
                                    existing_product.category = category
                                    update_fields.append('category')
                                if unit_code and not existing_product.unit:
                                    existing_product.unit = unit_code
                                    update_fields.append('unit')
                                existing_product.save(update_fields=list(set(update_fields)))
    
                                if quantity > Decimal('0') and warehouse:
                                    add_inventory_batch(
                                        product=existing_product,
                                        warehouse=warehouse,
                                        quantity=quantity,
                                        unit_cost=cost_price if cost_price > 0 else (existing_product.get_average_cost(warehouse) or Decimal('0')),
                                        batch_number=f'رصيد {datetime.date.today().strftime("%Y%m%d")}',
                                        reference='رصيد افتتاحي (استيراد إكسيل)',
                                        movement_type='OPENING_BALANCE'
                                    )
                                    total_opening_cost += (quantity * cost_price)
    
                                updated_count += 1
                            else:
                                skipped_count += 1
                        else:
                            # التحقق من عدم التكرار داخل نفس ملف الإكسيل المرفوع
                            if (barcode and barcode in seen_barcodes_in_sheet) or (norm_name in seen_names_in_sheet):
                                if duplicate_action == 'skip':
                                    skipped_count += 1
                                    continue
                                # في حالة التحديث: الصنف تم إنشاؤه في صف سابق من نفس الشيت، فلنحدّثه
                                in_sheet_prod = existing_products_by_barcode.get(barcode) if barcode else existing_products_by_name.get(norm_name)
                                if in_sheet_prod:
                                    if sale_price > Decimal('0'):
                                        in_sheet_prod.sale_price = sale_price
                                    if quantity > Decimal('0') and warehouse:
                                        add_inventory_batch(
                                            product=in_sheet_prod,
                                            warehouse=warehouse,
                                            quantity=quantity,
                                            unit_cost=cost_price if cost_price > 0 else (in_sheet_prod.get_average_cost(warehouse) or Decimal('0')),
                                            batch_number=f'رصيد {datetime.date.today().strftime("%Y%m%d")}',
                                            reference='رصيد افتتاحي (استيراد إكسيل)',
                                            movement_type='OPENING_BALANCE'
                                        )
                                        total_opening_cost += (quantity * cost_price)
                                    updated_count += 1
                                    continue
    
                            new_prod = Product.objects.create(
                                name=name,
                                barcode=barcode,
                                sale_price=sale_price,
                                last_purchase_price=cost_price,
                                category=category,
                                unit=unit_code,
                                min_stock_level=min_stock,
                                is_active=True,
                                product_type='PRODUCT'
                            )
    
                            if barcode:
                                existing_products_by_barcode[barcode] = new_prod
                                seen_barcodes_in_sheet.add(barcode)
                            existing_products_by_name[norm_name] = new_prod
                            seen_names_in_sheet.add(norm_name)
    
                            if quantity > Decimal('0') and warehouse:
                                add_inventory_batch(
                                    product=new_prod,
                                    warehouse=warehouse,
                                    quantity=quantity,
                                    unit_cost=cost_price,
                                    batch_number=f'رصيد {datetime.date.today().strftime("%Y%m%d")}',
                                    reference='رصيد افتتاحي (استيراد إكسيل)',
                                    movement_type='OPENING_BALANCE'
                                )
                                total_opening_cost += (quantity * cost_price)
    
                            created_count += 1
    
                    except Exception as row_err:
                        errors.append(f'الصف {row_num} ({name}): خطأ أثناء الحفظ - {str(row_err)}')
    
        # إنشاء قيد افتتاحي متوازن إذا كانت هناك كميات وتكلفة
        if total_opening_cost > Decimal('0') and warehouse:
            try:
                inv_acc = _get_system_account('1140')
                opening_acc = _get_system_account('3999')
                je = JournalEntry.objects.create(
                    date=datetime.date.today(),
                    reference=f'OPN-EXCEL-{int(datetime.datetime.now().timestamp())}',
                    description=f'رصيد افتتاحي بضاعة - استيراد إكسيل ({created_count + updated_count} صنف)',
                    status=JournalEntry.DRAFT,
                    created_by=request.user
                )
                JournalItem.objects.create(
                    entry=je, account=inv_acc, debit=total_opening_cost, credit=Decimal('0'),
                    description=f'إجمالي تكلفة بضاعة رصيد افتتاحي (استيراد إكسيل) - {warehouse.name}'
                )
                JournalItem.objects.create(
                    entry=je, account=opening_acc, debit=Decimal('0'), credit=total_opening_cost,
                    description=f'أرصدة افتتاحية رأس مال بضاعة (استيراد إكسيل) - {warehouse.name}'
                )
                je.post()
            except Exception as je_err:
                errors.append(f'تنبيه محاسبي: تم تسجيل البضاعة في المخزن ولكن تعذر ترحيل القيد الافتتاحي تلقائياً: {str(je_err)}')

        summary_msg = f'تمت المعالجة بنجاح: تم إنشاء {created_count} صنف جديد'
        if updated_count > 0:
            summary_msg += f'، وتحديث {updated_count} صنف'
        if skipped_count > 0:
            summary_msg += f'، وتخطي {skipped_count} صنف'
        if zeroed_negative_qty_count > 0:
            summary_msg += f'، وتصفير رصيد {zeroed_negative_qty_count} صنف كانت كمياتها بالسالب لحماية المخزون'
        summary_msg += '.'

        messages.success(request, summary_msg)
        if errors:
            for err in errors[:8]:
                messages.warning(request, err)
            if len(errors) > 8:
                messages.warning(request, f'وهناك {len(errors) - 8} ملاحظات أخرى في الشيت...')

        return redirect('inventory:product_list')

    return render(request, 'inventory/product_bulk_import.html', {
        'warehouses': warehouses,
        'title': 'استيراد المنتجات من شيت إكسيل'
    })


# --- Product Generic Views (List view already exists) ---
class ProductCreateView(CustomPermissionRequiredMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = 'inventory/product_form.html'
    success_url = reverse_lazy('inventory:product_list')
    permission_required = 'inventory.add_product'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة منتج جديد'
        context['cancel_url'] = reverse_lazy('inventory:product_list')
        context['warehouses'] = Warehouse.objects.filter(is_active=True)
        if self.request.POST:
            context['uom_formset'] = ProductUoMFormSet(self.request.POST)
        else:
            context['uom_formset'] = ProductUoMFormSet()
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        uom_formset = context['uom_formset']
        if uom_formset.is_valid():
            self.object = form.save()
            uom_formset.instance = self.object
            uom_formset.save()

            # Handle opening balance if specified
            op_qty_str = self.request.POST.get('opening_quantity', '').strip()
            op_cost_str = self.request.POST.get('opening_unit_cost', '').strip()
            op_wh_id = self.request.POST.get('opening_warehouse')
            if op_qty_str and op_wh_id:
                try:
                    op_qty = Decimal(op_qty_str).quantize(Decimal('0.01'))
                    wh = Warehouse.objects.filter(id=op_wh_id, is_active=True).first()
                    op_cost = (Decimal(op_cost_str) if op_cost_str else (self.object.last_purchase_price or Decimal('0.00'))).quantize(Decimal('0.01'))
                    if wh and op_qty > Decimal('0'):
                        from apps.tenant.services.opening_stock_service import update_or_create_product_opening_stock
                        res = update_or_create_product_opening_stock(
                            product=self.object,
                            warehouse=wh,
                            new_quantity=op_qty,
                            unit_cost=op_cost,
                            user=self.request.user,
                            reason=self.request.POST.get('opening_reason', '') or 'رصيد افتتاحي عند إنشاء المنتج'
                        )
                        je_ref = res['journal_entry'].reference if res.get('journal_entry') else 'قيد مسجل'
                        messages.success(self.request, f'تم إدخال رصيد أول المدة ({op_qty} وحدة) وترحيل القيد المحاسبي ({je_ref}).')
                except Exception as e:
                    messages.warning(self.request, f'تم حفظ المنتج ولكن حدث خطأ في رصيد أول المدة: {str(e)}')

            return super().form_valid(form)
        else:
            return self.render_to_response(self.get_context_data(form=form))

class ProductUpdateView(CustomPermissionRequiredMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = 'inventory/product_form.html'
    success_url = reverse_lazy('inventory:product_list')
    permission_required = 'inventory.change_product'

    def get_context_data(self, **kwargs):
        from apps.tenant.services.opening_stock_service import get_product_opening_info
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل منتج'
        context['cancel_url'] = reverse_lazy('inventory:product_list')
        warehouses = Warehouse.objects.filter(is_active=True)
        context['warehouses'] = warehouses
        default_wh = warehouses.first()
        context['opening_info'] = get_product_opening_info(self.object, default_wh)
        if self.request.POST:
            context['uom_formset'] = ProductUoMFormSet(self.request.POST, instance=self.object)
        else:
            context['uom_formset'] = ProductUoMFormSet(instance=self.object)
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        uom_formset = context['uom_formset']
        if uom_formset.is_valid():
            self.object = form.save()
            uom_formset.instance = self.object
            uom_formset.save()

            # Handle opening balance update if specified
            op_qty_str = self.request.POST.get('opening_quantity', '').strip()
            op_cost_str = self.request.POST.get('opening_unit_cost', '').strip()
            op_wh_id = self.request.POST.get('opening_warehouse')
            if op_qty_str != '' and op_wh_id:
                try:
                    op_qty = Decimal(op_qty_str).quantize(Decimal('0.01'))
                    wh = Warehouse.objects.filter(id=op_wh_id, is_active=True).first()
                    op_cost = (Decimal(op_cost_str) if op_cost_str else (self.object.last_purchase_price or Decimal('0.00'))).quantize(Decimal('0.01'))
                    if wh:
                        from apps.tenant.services.opening_stock_service import update_or_create_product_opening_stock
                        res = update_or_create_product_opening_stock(
                            product=self.object,
                            warehouse=wh,
                            new_quantity=op_qty,
                            unit_cost=op_cost,
                            user=self.request.user,
                            reason=self.request.POST.get('opening_reason', '') or 'تعديل رصيد أول المدة من شاشة تعديل المنتج'
                        )
                        je_ref = res['journal_entry'].reference if res.get('journal_entry') else 'قيد مسجل'
                        if res.get('action') == 'updated' and res.get('delta_quantity') != Decimal('0'):
                            messages.success(self.request, f'تم تحديث رصيد أول المدة بنجاح ({res["old_quantity"]} ⬅️ {res["new_quantity"]}) وترحيل القيد المحاسبي ({je_ref}).')
                        elif res.get('action') == 'created':
                            messages.success(self.request, f'تم تسجيل رصيد أول المدة ({res["new_quantity"]} وحدة) وترحيل القيد المحاسبي ({je_ref}).')
                except Exception as e:
                    messages.error(self.request, f'خطأ أثناء تحديث رصيد أول المدة: {str(e)}')

            return super().form_valid(form)
        else:
            return self.render_to_response(self.get_context_data(form=form))

from django.shortcuts import get_object_or_404
from django.db import transaction
from django.contrib import messages
from apps.tenant.services.fifo_engine import consume_fifo_batches
from apps.tenant.accounting.models import Account, JournalEntry, JournalItem
from django.contrib.contenttypes.models import ContentType
from .models import StockAdjustment, StockAdjustmentLine
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError

@login_required
@custom_permission_required('inventory.view_stockadjustment', redirect_url='core:main_screen')
def adjustment_list(request):
    from django.core.paginator import Paginator
    from django.db.models import Q

    query = request.GET.get('q', '').strip()
    adjustments = StockAdjustment.objects.select_related('warehouse').prefetch_related('lines__product', 'lines__uom__uom').order_by('-date', '-id')

    if query:
        adjustments = adjustments.filter(
            Q(reason__icontains=query) |
            Q(lines__product__name__icontains=query) |
            Q(lines__product__barcode__icontains=query) |
            Q(warehouse__name__icontains=query)
        ).distinct()

    paginator = Paginator(adjustments, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    context = {
        'adjustments': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
        'title': 'تسويات المخزون',
        'query': query,
    }
    return render(request, 'inventory/adjustment_list.html', context)
@login_required
@custom_permission_required('inventory.view_stockadjustment', redirect_url='core:main_screen')
def adjustment_detail(request, pk):
    adj = get_object_or_404(StockAdjustment.objects.select_related('warehouse', 'created_by').prefetch_related('lines__product', 'lines__uom__uom'), pk=pk)
    return render(request, 'inventory/adjustment_detail.html', {'adjustment': adj, 'title': f'تفاصيل التسوية #{adj.id}'})

@login_required
@custom_permission_required('inventory.delete_stockadjustment', redirect_url='core:main_screen')
def adjustment_delete(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk)
    if adj.status != 'DRAFT':
        messages.error(request, 'لا يمكن حذف تسوية تم ترحيلها بالفعل.')
        return redirect('inventory:adjustment_list')
        
    if request.method == 'POST':
        adj.delete()
        messages.success(request, 'تم حذف المسودة بنجاح.')
        return redirect('inventory:adjustment_list')
        
    # Optional: we can just delete via POST without a confirmation template since we use JS confirm in the button.
    return redirect('inventory:adjustment_list')

@login_required
@custom_permission_required('inventory.add_stockadjustment', redirect_url='core:main_screen')
@transaction.atomic
def adjustment_create(request):
    if request.method == 'POST':
        warehouse_id = request.POST.get('warehouse')
        reason = request.POST.get('reason')
        date_str = request.POST.get('date')
        adjustment_type = request.POST.get('adjustment_type')
        
        if adjustment_type == 'OPENING':
            messages.info(request, 'تم تخصيص شاشة مستقلة لإدارة وتعديل أرصدة أول المدة مع الترحيل المحاسبي الآلي.')
            return redirect('inventory:opening_stock_list')
        
        product_ids = request.POST.getlist('product_id[]')
        uom_ids = request.POST.getlist('uom_id[]')
        quantities = request.POST.getlist('quantity[]')
        unit_costs = request.POST.getlist('unit_cost[]')

        if len(uom_ids) < len(product_ids):
            uom_ids += [''] * (len(product_ids) - len(uom_ids))

        # Validate that there is at least one valid line with product and positive quantity
        valid_lines = []
        for pid, uom_id, qty, cost in zip(product_ids, uom_ids, quantities, unit_costs):
            if pid and qty:
                try:
                    q = Decimal(str(qty))
                    if q > Decimal('0'):
                        valid_lines.append((pid, uom_id, q, cost))
                except (InvalidOperation, ValueError):
                    pass

        if not valid_lines:
            messages.error(request, 'لا يمكن إنشاء تسوية مخزنية بدون إضافة أصناف وتحديد كميات صالحة!')
            return redirect('inventory:adjustment_create')

        warehouse = get_object_or_404(Warehouse, id=warehouse_id)
        
        adj = StockAdjustment.objects.create(
            warehouse=warehouse,
            adjustment_type=adjustment_type,
            reason=reason,
            date=date_str,
            created_by=request.user,
            status='DRAFT'
        )

        for pid, uom_id, q, cost in valid_lines:
            product = Product.objects.get(id=pid)
            puom = None
            conversion = Decimal('1.0')
            if uom_id and uom_id != 'base':
                try:
                    puom = ProductUoM.objects.select_related('uom').get(id=uom_id, product=product)
                    conversion = puom.conversion_factor or Decimal('1.0')
                except (ProductUoM.DoesNotExist, ValueError):
                    pass

            entered_cost = Decimal(cost) if cost else Decimal('0')
            if entered_cost > Decimal('0'):
                final_cost = entered_cost
            else:
                base_avg = product.get_average_cost(warehouse) or Decimal('0')
                final_cost = (base_avg * conversion).quantize(Decimal('0.0001'))

            StockAdjustmentLine.objects.create(
                adjustment=adj,
                product=product,
                uom=puom,
                conversion_factor=conversion or Decimal('1.0'),
                quantity=q,
                unit_cost=final_cost
            )
            
        messages.success(request, 'تم إنشاء التسوية بنجاح كمسودة.')
        return redirect('inventory:adjustment_list')
        
    warehouses = Warehouse.objects.filter(is_active=True)
    products = Product.objects.filter(is_active=True).prefetch_related('uoms__uom')
    return render(request, 'inventory/adjustment_form.html', {
        'warehouses': warehouses,
        'products': products,
        'title': 'إضافة تسوية مخزون جديدة'
    })

@login_required
@custom_permission_required('inventory.change_stockadjustment', redirect_url='core:main_screen')
@transaction.atomic
def adjustment_confirm(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk, status='DRAFT')
    
    if request.method == 'POST':
        if not adj.lines.exists():
            messages.error(request, 'لا يمكن ترحيل تسوية لا تحتوي على أي أصناف!')
            return redirect('inventory:adjustment_list')
        from apps.tenant.services.fifo_engine import consume_fifo_batches, add_inventory_batch
        from apps.tenant.services.journal_service import _get_system_account

        total_cost = Decimal('0')
        total_gain_cost = Decimal('0')
        total_loss_cost = Decimal('0')

        if adj.adjustment_type == 'STOCKTAKE':
            for line in adj.lines.select_related('product', 'uom'):
                factor = line.factor
                base_variance = line.quantity * factor  # physical_quantity - system_quantity in base units
                cost_per_base_unit = (line.unit_cost / factor) if (line.unit_cost > Decimal('0') and factor > Decimal('0')) else (
                    line.product.get_average_cost(adj.warehouse) or line.product.last_purchase_price or Decimal('0')
                )

                if base_variance > Decimal('0'):
                    # تسوية زيادة: محرك الـ FIFO يقوم بإغلاق أي عجز سالب سابق وترصيد الباقي
                    add_inventory_batch(
                        product=line.product,
                        warehouse=adj.warehouse,
                        quantity=base_variance,
                        unit_cost=cost_per_base_unit,
                        batch_number=f'تسوية {adj.id}',
                        reference=f'تسوية {adj.id}'
                    )
                    StockMovement.objects.filter(
                        reference=f'تسوية {adj.id}',
                        product=line.product,
                        warehouse=adj.warehouse,
                        movement_type='IN'
                    ).update(movement_type='ADJUSTMENT_IN', notes=adj.reason)
                    total_gain_cost += (base_variance * cost_per_base_unit)

                elif base_variance < Decimal('0'):
                    # تسوية عجز: استهلاك بالـ FIFO
                    qty_to_deduct = abs(base_variance)
                    try:
                        consumptions = consume_fifo_batches(
                            product=line.product,
                            warehouse=adj.warehouse,
                            quantity_needed=qty_to_deduct,
                            reference=f'تسوية {adj.id}',
                            notes=adj.reason
                        )
                        line_total = sum(c['total_cost'] for c in consumptions)
                        total_loss_cost += line_total
                    except ValueError as e:
                        messages.error(request, str(e))
                        return redirect('inventory:adjustment_list')

                    StockMovement.objects.filter(
                        reference=f'تسوية {adj.id}',
                        product=line.product,
                        warehouse=adj.warehouse,
                        movement_type='OUT'
                    ).update(movement_type='ADJUSTMENT_OUT')

            # قيد اليومية الخاص بالجرد الفعلي (مركب ومتوازن)
            if total_gain_cost > Decimal('0') or total_loss_cost > Decimal('0'):
                inventory_acc = _get_system_account('1140')
                gain_acc = _get_system_account('4999')
                loss_acc = _get_system_account('5250')

                je = JournalEntry.objects.create(
                    date=adj.date,
                    reference=f'STK-{adj.id}',
                    description=f'تسوية جرد فعلي رقم {adj.id}: {adj.reason}',
                    status=JournalEntry.DRAFT,
                    created_by=request.user,
                    content_type=ContentType.objects.get_for_model(StockAdjustment),
                    object_id=adj.id
                )

                if total_gain_cost > Decimal('0'):
                    JournalItem.objects.create(
                        entry=je, account=inventory_acc, debit=total_gain_cost, credit=Decimal('0'),
                        description=f'زيادة جرد فعلي تسوية #{adj.id}'
                    )
                    JournalItem.objects.create(
                        entry=je, account=gain_acc, debit=Decimal('0'), credit=total_gain_cost,
                        description=f'إيرادات فروق جرد تسوية #{adj.id}'
                    )

                if total_loss_cost > Decimal('0'):
                    JournalItem.objects.create(
                        entry=je, account=loss_acc, debit=total_loss_cost, credit=Decimal('0'),
                        description=f'مصروف عجز جرد تسوية #{adj.id}'
                    )
                    JournalItem.objects.create(
                        entry=je, account=inventory_acc, debit=Decimal('0'), credit=total_loss_cost,
                        description=f'عجز بضاعة جرد تسوية #{adj.id}'
                    )

                try:
                    je.post()
                except ValidationError as e:
                    messages.error(request, f'حدث خطأ محاسبي أثناء ترحيل قيد الجرد: {e.message if hasattr(e, "message") else str(e)}')
                    return redirect('inventory:adjustment_list')

        elif adj.adjustment_type == 'OUT':
            for line in adj.lines.select_related('product', 'uom'):
                factor = line.factor
                base_qty = line.quantity * factor
                try:
                    consumptions = consume_fifo_batches(
                        product=line.product,
                        warehouse=adj.warehouse,
                        quantity_needed=base_qty,
                        reference=f'تسوية {adj.id}',
                        notes=adj.reason
                    )
                    line_total = sum(c['total_cost'] for c in consumptions)
                    total_cost += line_total
                except ValueError as e:
                    messages.error(request, str(e))
                    return redirect('inventory:adjustment_list')

            StockMovement.objects.filter(
                reference=f'تسوية {adj.id}',
                movement_type='OUT'
            ).update(movement_type='ADJUSTMENT_OUT')

            if total_cost > Decimal('0'):
                exp_acc = _get_system_account('5250')
                inv_acc = _get_system_account('1140')
                je = JournalEntry.objects.create(
                    date=adj.date,
                    reference=f'ADJ-{adj.id}',
                    description=f'تسوية عجز/إعدام مخزون #{adj.id}: {adj.reason}',
                    status=JournalEntry.DRAFT,
                    created_by=request.user,
                    content_type=ContentType.objects.get_for_model(StockAdjustment),
                    object_id=adj.id
                )
                JournalItem.objects.create(entry=je, account=exp_acc, debit=total_cost, credit=Decimal('0'), description=f'عجز بضاعة تسوية {adj.id}')
                JournalItem.objects.create(entry=je, account=inv_acc, debit=Decimal('0'), credit=total_cost, description=f'عجز بضاعة تسوية {adj.id}')
                je.post()

        else:
            # IN or OPENING: Use add_inventory_batch to offset any prior deficit batches
            m_type = 'ADJUSTMENT_IN' if adj.adjustment_type == 'IN' else 'OPENING_BALANCE'
            for line in adj.lines.select_related('product', 'uom'):
                factor = line.factor
                base_qty = line.quantity * factor
                cost_per_base_unit = (line.unit_cost / factor) if (line.unit_cost > Decimal('0') and factor > Decimal('0')) else (
                    line.product.get_average_cost(adj.warehouse) or line.product.last_purchase_price or Decimal('0')
                )
                add_inventory_batch(
                    product=line.product,
                    warehouse=adj.warehouse,
                    quantity=base_qty,
                    unit_cost=cost_per_base_unit,
                    batch_number=f'تسوية {adj.id}',
                    reference=f'تسوية {adj.id}'
                )
                StockMovement.objects.filter(
                    reference=f'تسوية {adj.id}',
                    product=line.product,
                    warehouse=adj.warehouse,
                    movement_type='IN'
                ).update(movement_type=m_type, notes=adj.reason)
                total_cost += (base_qty * cost_per_base_unit)

            if total_cost > Decimal('0'):
                inv_acc = _get_system_account('1140')
                offset_code = '4999' if adj.adjustment_type == 'IN' else '3999'
                offset_acc = _get_system_account(offset_code)
                desc = f'زيادة بضاعة تسوية {adj.id}' if adj.adjustment_type == 'IN' else f'رصيد افتتاحي بضاعة تسوية {adj.id}'

                je = JournalEntry.objects.create(
                    date=adj.date,
                    reference=f'ADJ-{adj.id}',
                    description=f'تسوية مخزون رقم {adj.id}: {adj.reason}',
                    status=JournalEntry.DRAFT,
                    created_by=request.user,
                    content_type=ContentType.objects.get_for_model(StockAdjustment),
                    object_id=adj.id
                )
                JournalItem.objects.create(entry=je, account=inv_acc, debit=total_cost, credit=Decimal('0'), description=desc)
                JournalItem.objects.create(entry=je, account=offset_acc, debit=Decimal('0'), credit=total_cost, description=desc)
                je.post()

        adj.status = 'POSTED'
        adj.save()
        messages.success(request, 'تم ترحيل التسوية بنجاح وتحديث المخزون والحسابات العامة.')

    return redirect('inventory:adjustment_list')


@login_required
@custom_permission_required('inventory.perform_stocktake', redirect_url='core:main_screen')
def stocktake_create(request):
    """
    شاشة الجرد الفعلي وتسوية الأرصدة
    تسمح للمستخدم بإدخال الكمية الفعلية على الرف.
    يقوم النظام باحتساب الفارق آلياً:
    الفارق = الفعلي - الدفتري
    """
    import datetime
    from decimal import InvalidOperation

    # AJAX endpoint to load all products and their on-hand stock for a warehouse
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax') == '1':
        warehouse_id = request.GET.get('warehouse_id')
        if warehouse_id:
            try:
                wh = Warehouse.objects.get(id=warehouse_id)
                products = Product.objects.filter(is_active=True).values('id', 'name', 'barcode', 'sku')
                data = []
                for p in products:
                    prod_obj = Product(id=p['id'])
                    stock = prod_obj.get_stock(warehouse=wh)
                    avg_cost = prod_obj.get_average_cost(warehouse=wh) or prod_obj.last_purchase_price or Decimal('0')
                    data.append({
                        'id': p['id'],
                        'name': p['name'],
                        'barcode': p['barcode'] or '',
                        'sku': p['sku'] or '',
                        'stock': float(stock),
                        'cost': float(avg_cost)
                    })
                return JsonResponse({'status': 'success', 'products': data})
            except Warehouse.DoesNotExist:
                return JsonResponse({'status': 'error', 'message': 'المستودع غير موجود'}, status=404)

    if request.method == 'POST':
        warehouse_id = request.POST.get('warehouse')
        reason = request.POST.get('reason') or 'جرد فعلي وتسوية أرصدة المخزون'
        date_str = request.POST.get('date') or datetime.date.today().isoformat()
        action = request.POST.get('action', 'save_and_confirm')

        try:
            warehouse = Warehouse.objects.get(id=warehouse_id)
        except (Warehouse.DoesNotExist, ValueError):
            messages.error(request, 'يرجى تحديد المستودع بشكل صحيح.')
            return redirect('inventory:stocktake')

        adj = StockAdjustment.objects.create(
            warehouse=warehouse,
            adjustment_type='STOCKTAKE',
            reason=reason,
            date=date_str,
            created_by=request.user,
            status='DRAFT'
        )

        product_ids = request.POST.getlist('product_id[]')
        system_qtys = request.POST.getlist('system_qty[]')
        physical_qtys = request.POST.getlist('physical_qty[]')
        unit_costs = request.POST.getlist('unit_cost[]')

        lines_created = 0
        for pid, s_qty, p_qty, cost in zip(product_ids, system_qtys, physical_qtys, unit_costs):
            if pid and p_qty != '':
                try:
                    product = Product.objects.get(id=pid)
                    sys_q = Decimal(str(s_qty)) if s_qty else product.get_stock(warehouse)
                    phys_q = Decimal(str(p_qty))
                    variance = phys_q - sys_q
                    c = Decimal(str(cost)) if cost else (product.get_average_cost(warehouse) or product.last_purchase_price or Decimal('0'))

                    StockAdjustmentLine.objects.create(
                        adjustment=adj,
                        product=product,
                        system_quantity=sys_q,
                        physical_quantity=phys_q,
                        quantity=variance,
                        unit_cost=c,
                        conversion_factor=Decimal('1.0')
                    )
                    lines_created += 1
                except (Product.DoesNotExist, InvalidOperation, ValueError):
                    continue

        if lines_created == 0:
            adj.delete()
            messages.error(request, 'لم يتم إدخال أي أصناف أو كميات صالحة للجرد.')
            return redirect('inventory:stocktake')

        if action == 'save_and_confirm':
            request.method = 'POST'
            return adjustment_confirm(request, adj.id)
        else:
            messages.success(request, f'تم حفظ مسودة الجرد رقم #{adj.id} بنجاح.')
            return redirect('inventory:adjustment_detail', pk=adj.id)

    warehouses = Warehouse.objects.filter(is_active=True)
    products = Product.objects.filter(is_active=True)
    return render(request, 'inventory/stocktake_form.html', {
        'warehouses': warehouses,
        'products': products,
        'title': '📋 الجرد الفعلي وتسوية المخزون'
    })

# --- Unit of Measure Generic Views ---
class UnitOfMeasureListView(CustomPermissionRequiredMixin, ListView):
    model = UnitOfMeasure
    template_name = 'inventory/uom_list.html'
    context_object_name = 'uoms'
    permission_required = 'inventory.view_unitofmeasure'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'وحدات القياس'
        return context

class UnitOfMeasureCreateView(CustomPermissionRequiredMixin, CreateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'inventory/uom_form.html'
    success_url = reverse_lazy('inventory:uom_list')
    permission_required = 'inventory.add_unitofmeasure'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة وحدة قياس'
        context['cancel_url'] = self.success_url
        return context

class UnitOfMeasureUpdateView(CustomPermissionRequiredMixin, UpdateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'inventory/uom_form.html'
    success_url = reverse_lazy('inventory:uom_list')
    permission_required = 'inventory.change_unitofmeasure'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل وحدة قياس'
        context['cancel_url'] = self.success_url
        return context


@login_required
def api_products(request):
    from django.db.models import Q, Sum, F
    from decimal import Decimal

    warehouse_id = request.GET.get('warehouse_id')
    wh = None
    if warehouse_id:
        try:
            wh = Warehouse.objects.filter(id=int(warehouse_id), is_active=True).first()
        except (ValueError, TypeError):
            wh = None

    query = request.GET.get('q', '').strip()
    products_qs = Product.objects.filter(is_active=True).select_related('category').prefetch_related('uoms__uom')
    if query:
        products_qs = products_qs.filter(Q(name__icontains=query) | Q(barcode__icontains=query))[:50]
    else:
        products_qs = products_qs[:50]
    products_list = list(products_qs)
    p_ids = [p.id for p in products_list]

    current_branch = None
    branch_id = request.GET.get('branch_id')
    if branch_id and str(branch_id).isdigit():
        from apps.tenant.core.models import Branch
        current_branch = Branch.objects.filter(id=int(branch_id), is_active=True).first()

    if not current_branch:
        current_branch = getattr(request, 'branch', None)

    from apps.tenant.invoicing.models import InvoiceLine
    latest_sale_lines = InvoiceLine.objects.filter(
        invoice__invoice_type='SALE',
        invoice__status='POSTED'
    ).order_by('-id').values('product_id', 'unit_price')[:1000]
    latest_sale_dict = {}
    for item in latest_sale_lines:
        pid = item['product_id']
        if pid not in latest_sale_dict:
            latest_sale_dict[pid] = item['unit_price']

    if wh and p_ids:
        batch_qs = InventoryBatch.objects.filter(
            product_id__in=p_ids,
            warehouse=wh,
            quantity_remaining__gt=0
        )
        stock_map = dict(
            batch_qs.values('product_id').annotate(total=Sum('quantity_remaining')).values_list('product_id', 'total')
        )
        cost_rows = batch_qs.values('product_id').annotate(
            total_qty=Sum('quantity_remaining'),
            total_val=Sum(F('quantity_remaining') * F('unit_cost'))
        )
        cost_map = {
            row['product_id']: (row['total_val'] / row['total_qty'])
            for row in cost_rows if row['total_qty'] and row['total_qty'] > 0
        }
    else:
        stock_map = {}
        cost_map = {}

    products_data = []
    for p in products_list:
        sp = p.get_price_for_branch(current_branch)
        avail_stock = stock_map.get(p.id, Decimal('0'))
        avg_cost = cost_map.get(p.id, p.last_purchase_price or Decimal('0'))
        lp = p.last_purchase_price if p.last_purchase_price else avg_cost
        ls = latest_sale_dict.get(p.id, sp)
        products_data.append({
            'id': p.id,
            'name': p.name,
            'barcode': p.barcode or '',
            'product_type': p.product_type or 'PRODUCT',
            'is_open_price': getattr(p, 'is_open_price', False),
            'allow_negative_stock': p.allow_negative_stock,
            'sale_price': float(sp),
            'avg_cost': float(avg_cost),
            'last_purchase_price': float(lp),
            'last_sale_price': float(ls),
            'available_stock': float(avail_stock),
            'unit_display': p.pos_unit_name,
            'tax_rate': float(p.tax_rate) if hasattr(p, 'tax_rate') and p.tax_rate else (float(p.category.tax_rate) if p.category and hasattr(p.category, 'tax_rate') and p.category.tax_rate else 14),
            'withholding_tax_rate': float(p.withholding_tax_rate) if hasattr(p, 'withholding_tax_rate') and p.withholding_tax_rate else 0,
            'image': p.image.url if p.image else None,
            'uoms': [{'id': 'base', 'name': p.pos_unit_name, 'factor': 1.0}] + [{'id': pu.id, 'name': pu.uom.name, 'factor': float(pu.conversion_factor)} for pu in p.uoms.all() if not pu.is_base]
        })

    return JsonResponse({'products': products_data})


# =============================================================================
# PRODUCT OPENING STOCK MANAGEMENT & JOURNAL ENTRY SYNC
# =============================================================================

@login_required
@custom_permission_required('inventory.change_product', redirect_url='core:main_screen')
def api_product_opening_stock(request, pk):
    """
    API endpoint for viewing and updating a product's opening balance and its journal entry.
    Used by AJAX modal from the product list and other inventory screens.
    """
    from apps.tenant.services.opening_stock_service import get_product_opening_info, update_or_create_product_opening_stock
    product = get_object_or_404(Product, pk=pk)

    if request.method == 'GET':
        warehouse_id = request.GET.get('warehouse_id')
        if warehouse_id:
            warehouse = Warehouse.objects.filter(id=warehouse_id, is_active=True).first()
        else:
            warehouse = Warehouse.objects.filter(is_active=True).first()

        info = get_product_opening_info(product, warehouse)
        warehouses = list(Warehouse.objects.filter(is_active=True).values('id', 'name'))

        def _fmt_dec2(val):
            if val is None:
                return '0.00'
            if not isinstance(val, Decimal):
                try:
                    val = Decimal(str(val))
                except Exception:
                    return '0.00'
            return str(val.quantize(Decimal('0.01')))

        return JsonResponse({
            'status': 'success',
            'product_id': product.id,
            'product_name': product.name,
            'barcode': product.barcode or '',
            'warehouses': warehouses,
            'selected_warehouse_id': warehouse.id if warehouse else None,
            'has_opening': info['has_opening'],
            'quantity': _fmt_dec2(info['quantity']),
            'remaining_quantity': _fmt_dec2(info['remaining_quantity']),
            'consumed_quantity': _fmt_dec2(info['consumed_quantity']),
            'unit_cost': _fmt_dec2(info['unit_cost']),
            'default_cost': _fmt_dec2(info.get('default_cost') or product.last_purchase_price or Decimal('0')),
            'last_purchase_price': _fmt_dec2(product.last_purchase_price or Decimal('0')),
            'total_cost': _fmt_dec2(info['total_cost']),
            'journal_entry_ref': info['journal_entry_ref'] or 'غير مسجل بعد',
            'current_stock': _fmt_dec2(product.get_stock(warehouse=warehouse)),
        })

    elif request.method == 'POST':
        warehouse_id = request.POST.get('warehouse_id')
        qty_str = request.POST.get('quantity', '').strip()
        cost_str = request.POST.get('unit_cost', '').strip()
        reason = request.POST.get('reason', '').strip()

        if not warehouse_id:
            return JsonResponse({'status': 'error', 'message': 'يرجى تحديد المستودع.'}, status=400)

        warehouse = Warehouse.objects.filter(id=warehouse_id, is_active=True).first()
        if not warehouse:
            return JsonResponse({'status': 'error', 'message': 'المستودع المحدد غير موجود.'}, status=404)

        def _fmt_dec2(val):
            if val is None:
                return '0.00'
            if not isinstance(val, Decimal):
                try:
                    val = Decimal(str(val))
                except Exception:
                    return '0.00'
            return str(val.quantize(Decimal('0.01')))

        try:
            qty = Decimal(qty_str).quantize(Decimal('0.01')) if qty_str else Decimal('0.00')
            cost = Decimal(cost_str).quantize(Decimal('0.01')) if cost_str else Decimal('0.00')
            if cost <= Decimal('0.00'):
                cost = (product.last_purchase_price or Decimal('0.00')).quantize(Decimal('0.01'))
            res = update_or_create_product_opening_stock(
                product=product,
                warehouse=warehouse,
                new_quantity=qty,
                unit_cost=cost,
                user=request.user,
                reason=reason
            )
            je_ref = res['journal_entry'].reference if res.get('journal_entry') else 'قيد مسجل'
            return JsonResponse({
                'status': 'success',
                'message': f'تم تحديث رصيد أول المدة لصنف "{product.name}" بنجاح ({_fmt_dec2(res["new_quantity"])} وحدة) وترحيل القيد المحاسبي ({je_ref}).',
                'action': res['action'],
                'new_quantity': _fmt_dec2(res['new_quantity']),
                'delta_quantity': _fmt_dec2(res.get('delta_quantity', Decimal('0.00'))),
                'delta_value': _fmt_dec2(res.get('delta_value', Decimal('0.00'))),
                'new_stock': _fmt_dec2(product.get_stock(warehouse=warehouse)),
                'journal_entry_ref': je_ref
            })
        except ValidationError as e:
            msg = e.message if hasattr(e, 'message') else str(e)
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': f'حدث خطأ غير متوقع: {str(e)}'}, status=500)


@login_required
@custom_permission_required('inventory.change_product', redirect_url='core:main_screen')
def product_opening_stock_view(request, pk):
    """
    Dedicated view for inspecting and editing a single product's opening balance and journal entry.
    """
    from apps.tenant.services.opening_stock_service import get_product_opening_info, update_or_create_product_opening_stock
    product = get_object_or_404(Product, pk=pk)
    warehouses = Warehouse.objects.filter(is_active=True)

    warehouse_id = request.GET.get('warehouse') or request.POST.get('warehouse')
    if warehouse_id:
        warehouse = Warehouse.objects.filter(id=warehouse_id, is_active=True).first()
    else:
        warehouse = warehouses.first()

    if request.method == 'POST':
        qty_str = request.POST.get('quantity', '').strip()
        cost_str = request.POST.get('unit_cost', '').strip()
        reason = request.POST.get('reason', '').strip()
        try:
            qty = Decimal(qty_str).quantize(Decimal('0.01')) if qty_str else Decimal('0.00')
            cost = Decimal(cost_str).quantize(Decimal('0.01')) if cost_str else Decimal('0.00')
            if cost <= Decimal('0.00'):
                cost = (product.last_purchase_price or Decimal('0.00')).quantize(Decimal('0.01'))
            res = update_or_create_product_opening_stock(
                product=product,
                warehouse=warehouse,
                new_quantity=qty,
                unit_cost=cost,
                user=request.user,
                reason=reason
            )
            je_ref = res['journal_entry'].reference if res.get('journal_entry') else 'قيد مسجل'
            messages.success(request, f'تم تحديث رصيد أول المدة لصنف "{product.name}" بنجاح إلى {res["new_quantity"]} وحدة، وتم ترحيل القيد المحاسبي ({je_ref}).')
            return redirect('inventory:product_opening_stock', pk=product.id)
        except ValidationError as e:
            messages.error(request, e.message if hasattr(e, 'message') else str(e))
        except Exception as e:
            messages.error(request, f'حدث خطأ: {str(e)}')

    opening_info = get_product_opening_info(product, warehouse)
    
    opening_movements = StockMovement.objects.filter(
        product=product,
        warehouse=warehouse,
        movement_type='OPENING_BALANCE'
    ).order_by('-created_at')[:20]

    context = {
        'product': product,
        'warehouse': warehouse,
        'warehouses': warehouses,
        'opening_info': opening_info,
        'opening_movements': opening_movements,
        'title': f'تعديل رصيد أول المدة - {product.name}',
    }
    return render(request, 'inventory/product_opening_stock.html', context)


@login_required
@custom_permission_required('inventory.view_inventorybatch', redirect_url='core:main_screen')
def opening_stock_list_view(request):
    """
    Dedicated dashboard view listing all products that have opening stock,
    allowing search, warehouse filtering, and quick editing of opening balances and entries.
    """
    from django.core.paginator import Paginator
    from django.db.models import Q

    warehouses = Warehouse.objects.filter(is_active=True)
    warehouse_id = request.GET.get('warehouse')
    query = request.GET.get('q', '').strip()

    selected_warehouse = None
    if warehouse_id:
        selected_warehouse = Warehouse.objects.filter(id=warehouse_id, is_active=True).first()

    batches_qs = InventoryBatch.objects.filter(
        Q(movements__movement_type='OPENING_BALANCE') |
        Q(batch_number__startswith='رصيد')
    ).distinct().select_related('product', 'warehouse').order_by('-created_at')

    if selected_warehouse:
        batches_qs = batches_qs.filter(warehouse=selected_warehouse)

    if query:
        batches_qs = batches_qs.filter(
            Q(product__name__icontains=query) |
            Q(product__barcode__icontains=query)
        )

    paginator = Paginator(batches_qs, 30)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'batches': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
        'warehouses': warehouses,
        'selected_warehouse': selected_warehouse,
        'search_query': query,
        'title': 'أرصدة أول المدة للمنتجات',
    }
    return render(request, 'inventory/opening_stock_list.html', context)

