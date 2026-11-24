from django.shortcuts import render, redirect, get_object_or_404
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from .models import Partner, Payment
from apps.tenant.core.decorators import custom_permission_required

@login_required
@custom_permission_required('partners.view_partner', redirect_url='core:main_screen')
def supplier_list(request):
    from django.core.paginator import Paginator
    q = request.GET.get('q', '').strip()
    suppliers = Partner.objects.filter(partner_type__in=['SUPPLIER', 'BOTH']).select_related('account', 'receivable_account', 'payable_account').order_by('name')
    
    if q:
        suppliers = suppliers.filter(name__icontains=q)
        
    paginator = Paginator(suppliers, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    from apps.tenant.partners.services import batch_get_partners_balances
    balances = batch_get_partners_balances(partner_qs=list(page_obj.object_list))
    for supplier in page_obj:
        data = balances.get(supplier.id)
        # batch final_balance is debit - credit (customer perspective); suppliers show what we owe
        supplier.balance = -data['final_balance'] if data else Decimal('0')
        
    context = {
        'partners': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
        'title': 'الموردين',
        'type': 'supplier',
        'search_query': q,
    }
    return render(request, 'partners/list.html', context)

@login_required
@custom_permission_required('partners.view_partner', redirect_url='core:main_screen')
def customer_list(request):
    from django.core.paginator import Paginator
    q = request.GET.get('q', '').strip()
    customers = Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH']).select_related('account', 'receivable_account', 'payable_account', 'sector').order_by('name')
    
    if q:
        customers = customers.filter(name__icontains=q)
        
    paginator = Paginator(customers, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    from apps.tenant.partners.services import batch_get_partners_balances
    balances = batch_get_partners_balances(partner_qs=list(page_obj.object_list))
    for customer in page_obj:
        data = balances.get(customer.id)
        customer.balance = data['final_balance'] if data else Decimal('0')
        
    context = {
        'partners': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
        'title': 'العملاء',
        'type': 'customer',
        'search_query': q,
    }
    return render(request, 'partners/list.html', context)

from django.contrib import messages
from apps.tenant.pricing.models import CustomerSector

@login_required
@custom_permission_required('partners.add_partner', redirect_url='core:main_screen')
def partner_create(request):
    from decimal import Decimal, InvalidOperation
    from django.utils import timezone
    import datetime

    today_date = timezone.now().date().strftime('%Y-%m-%d')
    sectors = CustomerSector.objects.filter(is_active=True).order_by('name')

    if request.method == 'POST':
        name = request.POST.get('name')
        phone = request.POST.get('phone')
        mobile = request.POST.get('mobile')
        email = request.POST.get('email')
        national_id = request.POST.get('national_id')
        tax_id = request.POST.get('tax_id')
        partner_type = request.POST.get('partner_type')
        sector_id = request.POST.get('sector')
        opening_balance = request.POST.get('opening_balance', '0').strip()
        opening_balance_type = request.POST.get('opening_balance_type', 'DEBIT')
        opening_balance_date_str = request.POST.get('opening_balance_date', '').strip()

        try:
            op_balance = Decimal(opening_balance) if opening_balance else Decimal('0')
            if not op_balance.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            op_balance = None
        if op_balance is None or op_balance < 0:
            messages.error(request, 'رصيد أول المدة يجب أن يكون رقماً موجباً أو صفراً. استخدم نوع الرصيد (مدين/دائن) لتحديد الاتجاه.')
            return redirect(request.path)
        if opening_balance_type not in (Partner.OPENING_DEBIT, Partner.OPENING_CREDIT):
            messages.error(request, 'نوع رصيد أول المدة غير صالح.')
            return redirect(request.path)

        op_date = None
        if opening_balance_date_str:
            try:
                op_date = datetime.datetime.strptime(opening_balance_date_str, '%Y-%m-%d').date()
            except ValueError:
                op_date = timezone.now().date()
        elif op_balance > 0:
            op_date = timezone.now().date()

        sector = None
        if partner_type in ['CUSTOMER', 'BOTH'] and sector_id:
            try:
                sector = CustomerSector.objects.get(id=sector_id)
            except (CustomerSector.DoesNotExist, ValueError):
                sector = None
        
        Partner.objects.create(
            name=name,
            phone=phone,
            mobile=mobile,
            email=email,
            national_id=national_id,
            tax_id=tax_id,
            partner_type=partner_type,
            sector=sector,
            opening_balance=op_balance,
            opening_balance_type=opening_balance_type,
            opening_balance_date=op_date
        )
        messages.success(request, 'تم إضافة الشريك وتسجيل رصيد أول مدة بنجاح!')
        if partner_type == 'SUPPLIER':
            return redirect('partners:supplier_list')
        else:
            return redirect('partners:customer_list')
            
    return render(request, 'partners/form.html', {
        'title': 'إضافة شريك جديد',
        'today_date': today_date,
        'sectors': sectors
    })

@login_required
@custom_permission_required('partners.change_partner', redirect_url='core:main_screen')
def partner_edit(request, pk):
    from decimal import Decimal, InvalidOperation
    from django.utils import timezone
    from django.db.models import Q
    import datetime

    partner = get_object_or_404(Partner, pk=pk)
    today_date = timezone.now().date().strftime('%Y-%m-%d')
    sectors = CustomerSector.objects.filter(Q(is_active=True) | Q(id=partner.sector_id if partner.sector_id else 0)).order_by('name')

    if request.method == 'POST':
        opening_balance = request.POST.get('opening_balance', '0').strip()
        opening_balance_type = request.POST.get('opening_balance_type', 'DEBIT')
        opening_balance_date_str = request.POST.get('opening_balance_date', '').strip()

        try:
            op_balance = Decimal(opening_balance) if opening_balance else Decimal('0')
            if not op_balance.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            op_balance = None
        if op_balance is None or op_balance < 0:
            messages.error(request, 'رصيد أول المدة يجب أن يكون رقماً موجباً أو صفراً. استخدم نوع الرصيد (مدين/دائن) لتحديد الاتجاه.')
            return redirect(request.path)
        if opening_balance_type not in (Partner.OPENING_DEBIT, Partner.OPENING_CREDIT):
            messages.error(request, 'نوع رصيد أول المدة غير صالح.')
            return redirect(request.path)

        op_date = None
        if opening_balance_date_str:
            try:
                op_date = datetime.datetime.strptime(opening_balance_date_str, '%Y-%m-%d').date()
            except ValueError:
                op_date = timezone.now().date()
        elif op_balance > 0:
            op_date = partner.opening_balance_date or timezone.now().date()

        partner.name = request.POST.get('name')
        partner.phone = request.POST.get('phone')
        partner.mobile = request.POST.get('mobile')
        partner.email = request.POST.get('email')
        partner.national_id = request.POST.get('national_id')
        partner.tax_id = request.POST.get('tax_id')
        partner.partner_type = request.POST.get('partner_type')

        sector_id = request.POST.get('sector')
        if partner.partner_type in ['CUSTOMER', 'BOTH'] and sector_id:
            try:
                partner.sector = CustomerSector.objects.get(id=sector_id)
            except (CustomerSector.DoesNotExist, ValueError):
                partner.sector = None
        else:
            partner.sector = None

        partner.opening_balance = op_balance
        partner.opening_balance_type = opening_balance_type
        partner.opening_balance_date = op_date
        partner.save()
        messages.success(request, 'تم تحديث بيانات الشريك والرصيد الافتتاحي بنجاح!')
        if partner.partner_type == 'SUPPLIER':
            return redirect('partners:supplier_list')
        else:
            return redirect('partners:customer_list')
            
    return render(request, 'partners/form.html', {
        'partner': partner,
        'title': 'تعديل الشريك',
        'today_date': today_date,
        'sectors': sectors
    })

@login_required
def post_payment_view(request):
    if request.method == 'POST':
        # Logic to create and post payment using journal_service
        pass
    return redirect('core:main_screen')

def _detail_lines_with_adjustment(t):
    """
    Yields (line_dict, debit, credit) for an invoice transaction of the statement in detailed
    mode. Line totals do not include invoice-level discounts / withholding tax / rounding, so an
    adjustment pseudo-line is appended whenever they differ from the invoice amount used by the
    statement. This keeps the detailed running balance equal to the statement final balance.
    """
    from decimal import Decimal
    lines = t.get('lines') or []
    t_dr = Decimal(str(t.get('debit') or 0))
    t_cr = Decimal(str(t.get('credit') or 0))
    is_debit = t.get('badge_type') in ['sale', 'return_purchase'] or t_dr > 0
    lines_total = Decimal('0')
    for l in lines:
        amt = Decimal(str(l.get('total_amount') or 0))
        lines_total += amt
        yield (l, amt, Decimal('0')) if is_debit else (l, Decimal('0'), amt)

    target = t_dr if is_debit else t_cr
    diff = target - lines_total
    if abs(diff) >= Decimal('0.005'):
        signed = diff if is_debit else -diff  # expressed as debit
        adj = {
            'product_name': 'تسوية إجمالي الفاتورة (خصم إجمالي / خصم وإضافة / تقريب)',
            'quantity': None,
            'uom_name': '—',
            'unit_price': None,
            'discount_amount': Decimal('0'),
            'tax_amount': Decimal('0'),
            'total_amount': diff,
            'is_adjustment': True,
        }
        yield (adj, signed, Decimal('0')) if signed > 0 else (adj, Decimal('0'), -signed)


@login_required
@custom_permission_required('partners.view_partner', redirect_url='core:main_screen')
def partner_ledger(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    from apps.tenant.partners.services import get_partner_full_statement
    from django.core.paginator import Paginator
    from decimal import Decimal

    start_date = request.GET.get('start_date', '').strip()
    end_date = request.GET.get('end_date', '').strip()
    show_items = request.GET.get('show_items', '').strip().lower() in ['1', 'true', 'on', 'yes']

    stmt = get_partner_full_statement(
        partner,
        start_date=start_date if start_date else None,
        end_date=end_date if end_date else None
    )

    page_number = request.GET.get('page', 1)
    print_all = request.GET.get('print_all', '').strip() == '1'
    page_size = 100000 if print_all else 50

    if show_items:
        detailed_rows = []
        row_counter = 1
        running_bal = Decimal(str(stmt['opening_balance']))

        for t in stmt['transactions']:
            lines = t.get('lines', [])
            date_val = t['date']
            ref_val = t.get('reference') or ''
            doc_type_val = t.get('doc_type') or ''
            badge_type_val = t.get('badge_type') or ''
            desc_val = t.get('description') or ''
            url_val = t.get('url')

            if lines:
                for l, line_dr, line_cr in _detail_lines_with_adjustment(t):
                    tot_amount = Decimal(str(l.get('total_amount') or 0))
                    running_bal += line_dr - line_cr

                    detailed_rows.append({
                        'counter': row_counter,
                        'date': date_val,
                        'reference': ref_val,
                        'url': url_val,
                        'badge_type': badge_type_val,
                        'doc_type': doc_type_val,
                        'description': desc_val,
                        'product_name': l.get('product_name') or '',
                        'quantity': l.get('quantity') or 0,
                        'uom_name': l.get('uom_name') or 'قطعة',
                        'unit_price': l.get('unit_price') or 0,
                        'discount_amount': l.get('discount_amount') or Decimal('0'),
                        'tax_amount': l.get('tax_amount') or Decimal('0'),
                        'total_amount': tot_amount,
                        'debit': line_dr,
                        'credit': line_cr,
                        'running_balance': running_bal,
                        'is_line': True,
                    })
                    row_counter += 1
            else:
                dr = Decimal(str(t.get('debit') or 0))
                cr = Decimal(str(t.get('credit') or 0))
                running_bal += (dr - cr)

                detailed_rows.append({
                    'counter': row_counter,
                    'date': date_val,
                    'reference': ref_val,
                    'url': url_val,
                    'badge_type': badge_type_val,
                    'doc_type': doc_type_val,
                    'description': desc_val,
                    'product_name': '—',
                    'quantity': '—',
                    'uom_name': '—',
                    'unit_price': '—',
                    'discount_amount': '—',
                    'tax_amount': '—',
                    'total_amount': '—',
                    'debit': dr,
                    'credit': cr,
                    'running_balance': running_bal,
                    'is_line': False,
                })
                row_counter += 1

        paginator = Paginator(detailed_rows, page_size)
    else:
        for idx, t in enumerate(stmt['transactions'], start=1):
            t['counter'] = idx
        paginator = Paginator(stmt['transactions'], page_size)

    page_obj = paginator.get_page(1 if print_all else page_number)

    # Organisation name from Settings (store_name), else the company (tenant) name
    from apps.tenant.core.company import company_profile
    company_name = company_profile(request)['name']

    context = {
        'partner': partner,
        'items': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages() and not print_all,
        'start_date': start_date,
        'end_date': end_date,
        'opening_balance': stmt['opening_balance'],
        'period_debit': stmt['period_debit'],
        'period_credit': stmt['period_credit'],
        'final_balance': stmt['final_balance'],
        'all_partners': Partner.objects.filter(is_active=True).order_by('name'),
        'show_items': show_items,
        'title': f'كشف حساب: {partner.name}',
        'company_name': company_name,
        'print_all': print_all,
    }
    return render(request, 'partners/ledger.html', context)

@login_required
@custom_permission_required('partners.view_partner', redirect_url='core:main_screen')
def partner_ledger_index(request):
    return redirect('core:report_partners')



@login_required
@custom_permission_required('partners.view_partner', redirect_url='core:main_screen')
def partner_ledger_export_excel(request, pk):
    """
    تصدير كشف حساب الشريك بصيغة إكسيل احترافية:
    - يراعي بدقة تامة المدة الزمنية المحددة (start_date و end_date) ويحسب رصيد ما قبل الفترة بدقة.
    - يراعي الفلتر المختار (show_items):
        * إذا كان مفعلاً: يصدر كشفاً تحليلياً تفصيلياً شاملاً بالأصناف والكميات والأسعار (15 عموداً).
        * إذا كان غير مفعل (إجمالي الفواتير): يصدر كشفاً مالياً مدمجاً للحركات والفواتير (سطر لكل فاتورة/سند - 8 أعمدة).
    - يدعم تثبيت وتكرار صف رأس الجدول عند التمرير والطباعة (Freeze Panes & Print Title Rows).
    """
    import io
    from decimal import Decimal
    from urllib.parse import quote
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from django.http import HttpResponse
    from django.utils import timezone
    from apps.tenant.partners.services import get_partner_full_statement
    from apps.tenant.core.models import SystemSetting

    partner = get_object_or_404(Partner, pk=pk)

    start_date = request.GET.get('start_date', '').strip()
    end_date = request.GET.get('end_date', '').strip()
    show_items = request.GET.get('show_items', '').strip().lower() in ['1', 'true', 'on', 'yes']

    stmt = get_partner_full_statement(
        partner,
        start_date=start_date if start_date else None,
        end_date=end_date if end_date else None
    )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "كشف حساب تفصيلي" if show_items else "كشف حساب إجمالي"
    ws.views.sheetView[0].rightToLeft = True
    ws.views.sheetView[0].showGridLines = True

    c_primary = "1F4E79"       # Deep Navy
    c_primary_light = "D9E1F2" # Soft Blue
    c_header_text = "FFFFFF"
    c_text_dark = "1C2833"
    c_green = "006100"         # Dark green for debit
    c_red = "B74700"           # Dark amber/orange for credit
    c_gray_bg = "F9FAFB"       # Soft alternating row
    c_border = "D9D9D9"

    f_title = Font(name="Calibri", size=16, bold=True, color=c_primary)
    f_subtitle = Font(name="Calibri", size=11, bold=True, color="555555")
    f_tbl_header = Font(name="Calibri", size=11, bold=True, color=c_header_text)
    f_meta_label = Font(name="Calibri", size=10, bold=True, color="222222")
    f_meta_val = Font(name="Calibri", size=10, bold=False, color="111111")
    f_meta_val_bold = Font(name="Calibri", size=10, bold=True, color=c_primary)
    f_data = Font(name="Calibri", size=10, color=c_text_dark)
    f_data_bold = Font(name="Calibri", size=10, bold=True, color=c_text_dark)
    f_debit = Font(name="Calibri", size=10, bold=True, color=c_green)
    f_credit = Font(name="Calibri", size=10, bold=True, color=c_red)
    f_summary = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    fill_tbl_header = PatternFill(start_color=c_primary, end_color=c_primary, fill_type="solid")
    fill_meta_header = PatternFill(start_color=c_primary_light, end_color=c_primary_light, fill_type="solid")
    fill_zebra = PatternFill(start_color=c_gray_bg, end_color=c_gray_bg, fill_type="solid")
    fill_summary = PatternFill(start_color=c_primary, end_color=c_primary, fill_type="solid")
    fill_opening = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")

    b_thin = Side(style="thin", color=c_border)
    b_medium = Side(style="medium", color=c_primary)
    b_double = Side(style="double", color=c_primary)

    border_cell = Border(left=b_thin, right=b_thin, top=b_thin, bottom=b_thin)
    border_summary = Border(left=b_thin, right=b_thin, top=b_medium, bottom=b_double)

    al_center = Alignment(horizontal="center", vertical="center")
    al_right = Alignment(horizontal="right", vertical="center")
    al_left = Alignment(horizontal="left", vertical="center")

    # 1. Company Name Header
    # Organisation name from Settings (store_name), else the company (tenant) name
    from apps.tenant.core.company import company_profile
    company_name = company_profile(request)['name']

    total_cols = 15 if show_items else 8
    last_col_letter = openpyxl.utils.get_column_letter(total_cols)

    ws.merge_cells(f"A1:{last_col_letter}1")
    c1 = ws["A1"]
    c1.value = company_name
    c1.font = f_title
    c1.alignment = al_center
    ws.row_dimensions[1].height = 28

    ws.merge_cells(f"A2:{last_col_letter}2")
    c2 = ws["A2"]
    mode_text = "تفصيلي بالأصناف والكميات" if show_items else "إجمالي الفواتير والعمليات المالية"
    c2.value = f"كشف حساب مالي ({mode_text}) - {partner.name}"
    c2.font = f_subtitle
    c2.alignment = al_center
    ws.row_dimensions[2].height = 20

    # 2. Metadata Cards (Rows 4-6)
    partner_type_str = "عميل ومورد" if partner.partner_type == 'BOTH' else ("مورد" if partner.partner_type == 'SUPPLIER' else "عميل")
    period_str = f"من {start_date} إلى {end_date}" if (start_date and end_date) else (f"من {start_date} حتى الآن" if start_date else (f"حتى تاريخ {end_date}" if end_date else "كافة الحركات المسجلة"))
    now_str = timezone.now().strftime("%Y-%m-%d %H:%M")

    meta_rows = [
        [("اسم الشريك:", partner.name, True), ("فترة كشف الحساب:", period_str, True)],
        [("نوع الشريك:", partner_type_str, False), ("تاريخ وتوقيت الاستخراج:", now_str, False)],
        [("رقم الهاتف:", partner.phone or partner.mobile or "—", False), ("رصيد ما قبل الفترة (المنقول):", f"{float(stmt['opening_balance']):,.2f} ج.م", True)],
    ]

    curr_row = 4
    if show_items:
        left_val_end = 6
        right_lbl_start = 8
        right_lbl_end = 9
        right_val_start = 10
        right_val_end = 15
    else:
        left_val_end = 4
        right_lbl_start = 5
        right_lbl_end = 5
        right_val_start = 6
        right_val_end = 8

    for r in meta_rows:
        ws.row_dimensions[curr_row].height = 20
        # Left card
        ws.merge_cells(start_row=curr_row, start_column=1, end_row=curr_row, end_column=2 if show_items else 1)
        l1 = ws.cell(row=curr_row, column=1, value=r[0][0])
        l1.font = f_meta_label
        l1.alignment = al_right
        l1.fill = fill_meta_header
        for c_idx in range(1, (3 if show_items else 2)):
            ws.cell(row=curr_row, column=c_idx).border = border_cell
            ws.cell(row=curr_row, column=c_idx).fill = fill_meta_header

        ws.merge_cells(start_row=curr_row, start_column=(3 if show_items else 2), end_row=curr_row, end_column=left_val_end)
        v1 = ws.cell(row=curr_row, column=(3 if show_items else 2), value=r[0][1])
        v1.font = f_meta_val_bold if r[0][2] else f_meta_val
        v1.alignment = al_right
        for c_idx in range((3 if show_items else 2), left_val_end + 1):
            ws.cell(row=curr_row, column=c_idx).border = border_cell

        # Right card
        ws.merge_cells(start_row=curr_row, start_column=right_lbl_start, end_row=curr_row, end_column=right_lbl_end)
        l2 = ws.cell(row=curr_row, column=right_lbl_start, value=r[1][0])
        l2.font = f_meta_label
        l2.alignment = al_right
        l2.fill = fill_meta_header
        for c_idx in range(right_lbl_start, right_lbl_end + 1):
            ws.cell(row=curr_row, column=c_idx).border = border_cell
            ws.cell(row=curr_row, column=c_idx).fill = fill_meta_header

        ws.merge_cells(start_row=curr_row, start_column=right_val_start, end_row=curr_row, end_column=right_val_end)
        v2 = ws.cell(row=curr_row, column=right_val_start, value=r[1][1])
        v2.font = f_meta_val_bold if r[1][2] else f_meta_val
        v2.alignment = al_right
        for c_idx in range(right_val_start, right_val_end + 1):
            ws.cell(row=curr_row, column=c_idx).border = border_cell

        curr_row += 1

    # 3. Table Headers (Row 8)
    table_header_row = 8
    if show_items:
        headers = [
            "#",
            "التاريخ",
            "رقم الفاتورة / المستند",
            "نوع الحركة",
            "البيان / الشرح",
            "اسم الصنف",
            "الكمية",
            "الوحدة",
            "سعر الوحدة",
            "الخصم",
            "الضريبة",
            "إجمالي الصنف",
            "مدين (لنا)",
            "دائن (له)",
            "الرصيد التراكمي"
        ]
    else:
        headers = [
            "#",
            "التاريخ",
            "رقم المستند / المرجع",
            "نوع الحركة",
            "البيان / الشرح",
            "مدين (لنا)",
            "دائن (له)",
            "الرصيد التراكمي"
        ]

    ws.row_dimensions[table_header_row].height = 26
    for col_idx, h in enumerate(headers, start=1):
        cell_h = ws.cell(row=table_header_row, column=col_idx, value=h)
        cell_h.font = f_tbl_header
        cell_h.fill = fill_tbl_header
        cell_h.alignment = al_center
        cell_h.border = border_cell

    curr_row = 9
    row_counter = 1

    # 4. Opening Balance Row (if any)
    if stmt['opening_balance'] != Decimal('0'):
        op_val = float(stmt['opening_balance'])
        ws.row_dimensions[curr_row].height = 22
        if show_items:
            vals = [
                row_counter,
                start_date or "رصيد سابق",
                f"OPN-{partner.id}",
                "رصيد مرحل (سابق)",
                "رصيد ما قبل الفترة المنقول للشريك",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                op_val if op_val > 0 else 0.0,
                abs(op_val) if op_val < 0 else 0.0,
                op_val
            ]
        else:
            vals = [
                row_counter,
                start_date or "رصيد سابق",
                f"OPN-{partner.id}",
                "رصيد مرحل (سابق)",
                "رصيد ما قبل الفترة المنقول للشريك",
                op_val if op_val > 0 else 0.0,
                abs(op_val) if op_val < 0 else 0.0,
                op_val
            ]

        dr_col_idx = 13 if show_items else 6
        cr_col_idx = 14 if show_items else 7
        bal_col_idx = 15 if show_items else 8

        for c_idx, val in enumerate(vals, start=1):
            c = ws.cell(row=curr_row, column=c_idx, value=val)
            c.font = f_data_bold
            c.fill = fill_opening
            c.border = border_cell
            if c_idx in [dr_col_idx, cr_col_idx, bal_col_idx]:
                c.number_format = '#,##0.00'
                c.alignment = al_right
            elif c_idx in [1, 2, 3, 4]:
                c.alignment = al_center
            else:
                c.alignment = al_right

        curr_row += 1
        row_counter += 1

    # 5. Data Rows
    running_bal = float(stmt['opening_balance'])

    if show_items:
        # Detailed items mode
        for t in stmt['transactions']:
            date_val = t['date'].strftime('%Y-%m-%d') if hasattr(t['date'], 'strftime') else str(t['date'])
            ref_val = t.get('reference') or ''
            doc_type_val = t.get('doc_type') or ''
            desc_val = t.get('description') or ''
            lines = t.get('lines', [])

            if lines:
                for l, d_dr, d_cr in _detail_lines_with_adjustment(t):
                    ws.row_dimensions[curr_row].height = 22
                    q_num = float(l.get('quantity') or 0)
                    u_name = l.get('uom_name') or 'قطعة'
                    p_num = float(l.get('unit_price') or 0)
                    disc_num = float(l.get('discount_amount') or 0)
                    tax_num = float(l.get('tax_amount') or 0)
                    tot_num = float(l.get('total_amount') or 0)

                    line_dr = float(d_dr)
                    line_cr = float(d_cr)
                    running_bal += line_dr - line_cr

                    is_even = (row_counter % 2 == 0)
                    row_vals = [
                        row_counter,
                        date_val,
                        ref_val,
                        doc_type_val,
                        desc_val,
                        l.get('product_name') or '',
                        q_num,
                        u_name,
                        p_num,
                        disc_num,
                        tax_num,
                        tot_num,
                        line_dr if line_dr > 0 else 0.0,
                        line_cr if line_cr > 0 else 0.0,
                        running_bal
                    ]

                    for c_idx, val in enumerate(row_vals, start=1):
                        c = ws.cell(row=curr_row, column=c_idx, value=val)
                        c.font = f_data
                        c.border = border_cell
                        if is_even:
                            c.fill = fill_zebra

                        if c_idx in [1, 2, 4, 8]:
                            c.alignment = al_center
                        elif c_idx == 3:
                            c.alignment = al_center
                            c.font = f_data_bold
                        elif c_idx in [5, 6]:
                            c.alignment = al_right
                            if c_idx == 6:
                                c.font = f_data_bold
                        elif c_idx == 7:
                            c.alignment = al_center
                            c.number_format = '#,##0.##'
                        elif c_idx in [9, 10, 11, 12, 13, 14, 15]:
                            c.number_format = '#,##0.00'
                            c.alignment = al_right
                            if c_idx == 13 and line_dr > 0:
                                c.font = f_debit
                            elif c_idx == 14 and line_cr > 0:
                                c.font = f_credit
                            elif c_idx in [12, 15]:
                                c.font = f_data_bold

                    curr_row += 1
                    row_counter += 1
            else:
                ws.row_dimensions[curr_row].height = 22
                dr_num = float(t.get('debit') or 0)
                cr_num = float(t.get('credit') or 0)
                running_bal += (dr_num - cr_num)

                is_even = (row_counter % 2 == 0)
                row_vals = [
                    row_counter,
                    date_val,
                    ref_val,
                    doc_type_val,
                    desc_val,
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    dr_num if dr_num > 0 else 0.0,
                    cr_num if cr_num > 0 else 0.0,
                    running_bal
                ]

                for c_idx, val in enumerate(row_vals, start=1):
                    c = ws.cell(row=curr_row, column=c_idx, value=val)
                    c.font = f_data
                    c.border = border_cell
                    if is_even:
                        c.fill = fill_zebra

                    if c_idx in [1, 2, 4, 7, 8]:
                        c.alignment = al_center
                    elif c_idx == 3:
                        c.alignment = al_center
                        c.font = f_data_bold
                    elif c_idx in [5, 6]:
                        c.alignment = al_right
                    elif c_idx in [13, 14, 15]:
                        c.number_format = '#,##0.00'
                        c.alignment = al_right
                        if c_idx == 13 and dr_num > 0:
                            c.font = f_debit
                        elif c_idx == 14 and cr_num > 0:
                            c.font = f_credit
                        elif c_idx == 15:
                            c.font = f_data_bold

                curr_row += 1
                row_counter += 1
    else:
        # Summary (Invoice totals) mode - One row per transaction
        for t in stmt['transactions']:
            ws.row_dimensions[curr_row].height = 22
            date_val = t['date'].strftime('%Y-%m-%d') if hasattr(t['date'], 'strftime') else str(t['date'])
            ref_val = t.get('reference') or ''
            doc_type_val = t.get('doc_type') or ''
            desc_val = t.get('description') or ''
            dr_num = float(t.get('debit') or 0)
            cr_num = float(t.get('credit') or 0)
            running_bal += (dr_num - cr_num)

            is_even = (row_counter % 2 == 0)
            row_vals = [
                row_counter,
                date_val,
                ref_val,
                doc_type_val,
                desc_val,
                dr_num if dr_num > 0 else 0.0,
                cr_num if cr_num > 0 else 0.0,
                running_bal
            ]

            for c_idx, val in enumerate(row_vals, start=1):
                c = ws.cell(row=curr_row, column=c_idx, value=val)
                c.font = f_data
                c.border = border_cell
                if is_even:
                    c.fill = fill_zebra

                if c_idx in [1, 2, 4]:
                    c.alignment = al_center
                elif c_idx == 3:
                    c.alignment = al_center
                    c.font = f_data_bold
                elif c_idx == 5:
                    c.alignment = al_right
                elif c_idx in [6, 7, 8]:
                    c.number_format = '#,##0.00'
                    c.alignment = al_right
                    if c_idx == 6 and dr_num > 0:
                        c.font = f_debit
                    elif c_idx == 7 and cr_num > 0:
                        c.font = f_credit
                    elif c_idx == 8:
                        c.font = f_data_bold

            curr_row += 1
            row_counter += 1

    # 6. Summary Totals Row
    ws.row_dimensions[curr_row].height = 26
    merge_end = 12 if show_items else 5
    ws.merge_cells(start_row=curr_row, start_column=1, end_row=curr_row, end_column=merge_end)
    sum_lbl = ws.cell(row=curr_row, column=1, value="إجماليات الفترة والرصيد الختامي:")
    sum_lbl.font = f_summary
    sum_lbl.fill = fill_summary
    sum_lbl.alignment = al_right
    for c_i in range(1, merge_end + 1):
        ws.cell(row=curr_row, column=c_i).fill = fill_summary
        ws.cell(row=curr_row, column=c_i).border = border_summary

    tot_dr = float(stmt['period_debit'] or 0)
    dr_c_idx = 13 if show_items else 6
    c_dr = ws.cell(row=curr_row, column=dr_c_idx, value=tot_dr)
    c_dr.font = f_summary
    c_dr.fill = fill_summary
    c_dr.alignment = al_right
    c_dr.number_format = '#,##0.00'
    c_dr.border = border_summary

    tot_cr = float(stmt['period_credit'] or 0)
    cr_c_idx = 14 if show_items else 7
    c_cr = ws.cell(row=curr_row, column=cr_c_idx, value=tot_cr)
    c_cr.font = f_summary
    c_cr.fill = fill_summary
    c_cr.alignment = al_right
    c_cr.number_format = '#,##0.00'
    c_cr.border = border_summary

    tot_bal = float(stmt['final_balance'] or 0)
    bal_c_idx = 15 if show_items else 8
    c_bal = ws.cell(row=curr_row, column=bal_c_idx, value=tot_bal)
    c_bal.font = f_summary
    c_bal.fill = fill_summary
    c_bal.alignment = al_right
    c_bal.number_format = '#,##0.00'
    c_bal.border = border_summary

    curr_row += 1

    # 7. Final Balance Status Note
    ws.row_dimensions[curr_row].height = 24
    ws.merge_cells(start_row=curr_row, start_column=1, end_row=curr_row, end_column=total_cols)
    note_cell = ws.cell(row=curr_row, column=1)
    if tot_bal > 0:
        note_cell.value = f"📌 حالة الرصيد النهائي: رصيد مدين مستحق لنا طرف الشريك بقيمة {tot_bal:,.2f} ج.م"
        note_cell.fill = PatternFill(start_color="D4EDDA", end_color="D4EDDA", fill_type="solid")
        note_cell.font = Font(name="Calibri", size=11, bold=True, color="155724")
    elif tot_bal < 0:
        note_cell.value = f"📌 حالة الرصيد النهائي: رصيد دائن مستحق للشريك علينا بقيمة {abs(tot_bal):,.2f} ج.م"
        note_cell.fill = PatternFill(start_color="F8D7DA", end_color="F8D7DA", fill_type="solid")
        note_cell.font = Font(name="Calibri", size=11, bold=True, color="721C24")
    else:
        note_cell.value = "📌 حالة الرصيد النهائي: الحساب مسدد ومتوازن بالكامل (0.00 ج.م)"
        note_cell.fill = PatternFill(start_color="D1ECF1", end_color="D1ECF1", fill_type="solid")
        note_cell.font = Font(name="Calibri", size=11, bold=True, color="0C5460")
    note_cell.alignment = al_right
    for c_i in range(1, total_cols + 1):
        ws.cell(row=curr_row, column=c_i).border = border_cell

    # Column Widths
    if show_items:
        col_widths = {
            'A': 6,   # #
            'B': 13,  # Date
            'C': 22,  # Invoice No
            'D': 18,  # Movement Type
            'E': 26,  # Description
            'F': 32,  # Product Name
            'G': 11,  # Qty
            'H': 10,  # Unit
            'I': 13,  # Unit Price
            'J': 11,  # Discount
            'K': 11,  # Tax
            'L': 14,  # Line Total
            'M': 15,  # Debit
            'N': 15,  # Credit
            'O': 17,  # Running Balance
        }
    else:
        col_widths = {
            'A': 6,   # #
            'B': 13,  # Date
            'C': 24,  # Doc No
            'D': 20,  # Movement Type
            'E': 36,  # Description
            'F': 16,  # Debit
            'G': 16,  # Credit
            'H': 18,  # Running Balance
        }

    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    # Freeze panes: تثبيت رأس الجدول عند التمرير على الشاشة
    ws.freeze_panes = 'A9'

    # Print setup: تكرار صف رأس الجدول تلقائياً في كل صفحة عند الطباعة أو التصدير PDF
    ws.print_title_rows = '8:8'
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    # Header & Footer للطباعة: رقم الصفحة وتاريخ الاستخراج
    ws.oddFooter.center.text = "صفحة &P من &N"
    ws.oddFooter.right.text = f"{company_name} - كشف حساب ({partner.name})"
    ws.oddFooter.left.text = "تاريخ الاستخراج: &D"

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename_prefix = "كشف_حساب_تفصيلي" if show_items else "كشف_حساب_إجمالي"
    filename = f"{filename_prefix}_{partner.name}_{timezone.now().strftime('%Y%m%d')}.xlsx"
    encoded_filename = quote(filename)

    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
    return response
