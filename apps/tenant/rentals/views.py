from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.urls import reverse_lazy
from django.contrib import messages
from django.views.generic import ListView, CreateView, UpdateView, View, DetailView
from django.contrib.auth.decorators import login_required
from apps.tenant.core.mixins import CustomPermissionRequiredMixin
from apps.tenant.core.decorators import custom_permission_required
from .models import Equipment, Rental
from .forms import EquipmentForm, RentalForm

class RentalPrintView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'rentals.view_rental'
    model = Rental
    template_name = 'rentals/rental_print.html'
    context_object_name = 'rental'
    
    def get_queryset(self):
        return Rental.objects.select_related('equipment', 'customer', 'created_by')

class EquipmentListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'rentals.view_equipment'
    model = Equipment
    template_name = 'rentals/equipment_list.html'
    context_object_name = 'equipments'
    paginate_by = 25
    ordering = ['name']

class EquipmentCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'rentals.add_equipment'
    model = Equipment
    form_class = EquipmentForm
    template_name = 'rentals/equipment_form.html'
    success_url = reverse_lazy('rentals:equipment_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        try:
            self.object.post_opening_balance(user=self.request.user)
            messages.success(self.request, 'تمت إضافة المعدة وتسجيل الرصيد الافتتاحي (إن وُجد) بنجاح.')
        except Exception as e:
            messages.warning(self.request, f'تمت إضافة المعدة ولكن حدث خطأ في القيد الافتتاحي: {e}')
        return response

class EquipmentUpdateView(CustomPermissionRequiredMixin, UpdateView):
    permission_required = 'rentals.change_equipment'
    model = Equipment
    form_class = EquipmentForm
    template_name = 'rentals/equipment_form.html'
    success_url = reverse_lazy('rentals:equipment_list')

    def form_valid(self, form):
        messages.success(self.request, 'تم تحديث المعدة بنجاح.')
        return super().form_valid(form)

class RentalListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'rentals.view_rental'
    model = Rental
    template_name = 'rentals/rental_list.html'
    context_object_name = 'rentals'
    paginate_by = 25
    
    def get_queryset(self):
        return Rental.objects.select_related('equipment', 'customer').order_by('-created_at')

class RentalCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'rentals.add_rental'
    model = Rental
    form_class = RentalForm
    template_name = 'rentals/rental_form.html'
    success_url = reverse_lazy('rentals:rental_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        from django.db import transaction
        from apps.tenant.invoicing.models import POSSession
        rental = form.save(commit=False)
        rental.created_by = self.request.user
        
        open_session = POSSession.objects.filter(user=self.request.user, status=POSSession.OPEN).first()
        if open_session:
            rental.pos_session = open_session

        try:
            with transaction.atomic():
                rental.save()
                rental.post_rental()
            messages.success(self.request, 'تم إنشاء عقد الإيجار وتسجيل القيود المحاسبية بنجاح.')
            return redirect(f"{self.success_url}?print_contract={rental.id}")
        except Exception as e:
            form.add_error(None, str(e))
            return self.form_invalid(form)

class RentalDetailView(CustomPermissionRequiredMixin, View):
    permission_required = 'rentals.view_rental'
    def get(self, request, pk):
        rental = get_object_or_404(Rental.objects.select_related('equipment', 'customer', 'accrual_journal_entry', 'payment_journal_entry'), pk=pk)
        return render(request, 'rentals/rental_detail.html', {'rental': rental})

class RentalReturnView(CustomPermissionRequiredMixin, View):
    permission_required = 'rentals.change_rental'
    def get(self, request, pk):
        rental = get_object_or_404(Rental, pk=pk)
        from apps.tenant.accounting.models import Treasury, EWallet
        treasuries = Treasury.objects.all()
        ewallets = EWallet.objects.all()
        
        return render(request, 'rentals/rental_return.html', {
            'rental': rental,
            'treasuries': treasuries,
            'ewallets': ewallets
        })

    def post(self, request, pk):
        rental = get_object_or_404(Rental, pk=pk)
        refund_amount = request.POST.get('refund_amount', 0)
        penalty_amount = request.POST.get('penalty_amount', 0)
        refund_method = request.POST.get('refund_method', 'none')
        treasury_id = request.POST.get('treasury')
        ewallet_id = request.POST.get('ewallet')
        
        from apps.tenant.accounting.models import Treasury, EWallet
        treasury = Treasury.objects.filter(pk=treasury_id).first() if treasury_id else None
        ewallet = EWallet.objects.filter(pk=ewallet_id).first() if ewallet_id else None
        
        try:
            rental.return_equipment(
                refund_amount=refund_amount,
                refund_method=refund_method,
                treasury=treasury,
                ewallet=ewallet,
                penalty_amount=penalty_amount
            )
            messages.success(request, 'تم تسجيل عودة المعدة بنجاح وأصبحت متاحة للإيجار.')
            return redirect('rentals:rental_list')
        except Exception as e:
            messages.error(request, f'خطأ: {str(e)}')
            return redirect('rentals:rental_return', pk=pk)

from django.db.models import Sum, Count

class RentalReportsView(CustomPermissionRequiredMixin, View):
    permission_required = 'rentals.view_rental'
    def get(self, request):
        equipment_status = Equipment.objects.values('status').annotate(count=Count('id'))
        active_rentals = Rental.objects.filter(status=Rental.STATUS_ACTIVE).select_related('equipment', 'customer').order_by('end_date')
        
        from django.db.models import F
        equipment_profitability = Equipment.objects.annotate(
            total_revenue=Sum(F('rentals__total_amount') - F('rentals__refund_amount'))
        ).order_by('-total_revenue')

        total_profits = Rental.objects.aggregate(
            total=Sum(F('total_amount') - F('refund_amount'))
        )['total'] or 0
        
        context = {
            'equipment_status': equipment_status,
            'active_rentals': active_rentals,
            'equipment_profitability': equipment_profitability,
            'total_profits': total_profits,
            'title': 'تقارير تأجير المعدات'
        }
        return render(request, 'rentals/reports.html', context)

class EquipmentUtilizationReportView(CustomPermissionRequiredMixin, View):
    permission_required = 'rentals.view_equipment'
    def get(self, request):
        import datetime
        today = datetime.date.today()
        equipments = Equipment.objects.prefetch_related('rentals').all()
        
        report_data = []
        for eq in equipments:
            total_days_since_added = max(1, (today - eq.created_at.date()).days)
            
            actual_rented_days = 0
            for r in eq.rentals.all():
                if r.status == Rental.STATUS_RETURNED:
                    days = (r.updated_at.date() - r.start_date).days
                    actual_rented_days += max(1, days)
                else:
                    days = (today - r.start_date).days
                    actual_rented_days += max(0, days)
                    
            utilization_pct = min(100, round((actual_rented_days / total_days_since_added) * 100, 1))
            
            report_data.append({
                'equipment': eq,
                'total_days_available': total_days_since_added,
                'actual_rented_days': actual_rented_days,
                'utilization_pct': utilization_pct,
            })
            
        report_data = sorted(report_data, key=lambda x: x['utilization_pct'], reverse=True)
        
        return render(request, 'rentals/report_utilization.html', {
            'report_data': report_data,
            'title': 'معدل تشغيل المعدات'
        })

class OverdueRentalsReportView(CustomPermissionRequiredMixin, View):
    permission_required = 'rentals.view_rental'
    def get(self, request):
        import datetime
        today = datetime.date.today()
        
        overdue_rentals = Rental.objects.filter(
            status=Rental.STATUS_ACTIVE,
            end_date__lt=today
        ).select_related('equipment', 'customer').order_by('end_date')
        
        report_data = []
        for r in overdue_rentals:
            days_overdue = (today - r.end_date).days
            report_data.append({
                'rental': r,
                'days_overdue': days_overdue
            })
            
        return render(request, 'rentals/report_overdue.html', {
            'report_data': report_data,
            'title': 'المتأخرات'
        })

from decimal import Decimal
from apps.tenant.partners.models import Partner

class DetailedRentalsReportView(CustomPermissionRequiredMixin, View):
    permission_required = 'rentals.view_rental'

    def get(self, request):
        import datetime
        today = datetime.date.today()
        
        start_date_str = request.GET.get('start_date', '').strip()
        end_date_str = request.GET.get('end_date', '').strip()
        equipment_id = request.GET.get('equipment_id', '').strip()
        customer_id = request.GET.get('customer_id', '').strip()
        status_filter = request.GET.get('status', '').strip()
        payment_method = request.GET.get('payment_method', '').strip()

        qs = Rental.objects.select_related('equipment', 'customer', 'created_by').all()

        if start_date_str:
            try:
                s_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
                qs = qs.filter(start_date__gte=s_date)
            except ValueError:
                pass

        if end_date_str:
            try:
                e_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').date()
                qs = qs.filter(start_date__lte=e_date)
            except ValueError:
                pass

        if equipment_id:
            qs = qs.filter(equipment_id=equipment_id)

        if customer_id:
            qs = qs.filter(customer_id=customer_id)

        if status_filter == 'ACTIVE':
            qs = qs.filter(status=Rental.STATUS_ACTIVE)
        elif status_filter == 'RETURNED':
            qs = qs.filter(status=Rental.STATUS_RETURNED)
        elif status_filter == 'OVERDUE':
            qs = qs.filter(status=Rental.STATUS_ACTIVE, end_date__lt=today)

        if payment_method in ['CASH', 'CREDIT']:
            qs = qs.filter(payment_method=payment_method)

        rentals_list = []
        total_gross = Decimal('0')
        total_refunds = Decimal('0')
        total_penalties = Decimal('0')
        total_net_profit = Decimal('0')
        total_days = 0

        for r in qs.order_by('-start_date', '-id'):
            gross = r.total_amount or Decimal('0')
            refund = r.refund_amount or Decimal('0')
            penalty = r.penalty_amount or Decimal('0')
            net = gross - refund + penalty
            days = r.days_rented or 1
            is_overdue = (r.status == Rental.STATUS_ACTIVE and r.end_date and r.end_date < today)

            rentals_list.append({
                'rental': r,
                'gross': gross,
                'refund': refund,
                'penalty': penalty,
                'net_profit': net,
                'days': days,
                'is_overdue': is_overdue,
            })

            total_gross += gross
            total_refunds += refund
            total_penalties += penalty
            total_net_profit += net
            total_days += days

        context = {
            'title': 'تقرير المعدات المؤجرة التفصيلي',
            'rentals_list': rentals_list,
            'summary': {
                'count': len(rentals_list),
                'total_days': total_days,
                'total_gross': total_gross,
                'total_refunds': total_refunds,
                'total_penalties': total_penalties,
                'total_net_profit': total_net_profit,
            },
            'equipments': Equipment.objects.filter(is_active=True).order_by('name'),
            'customers': Partner.objects.filter(partner_type__in=['CUSTOMER', 'BOTH']).order_by('name'),
            'filters': {
                'start_date': start_date_str,
                'end_date': end_date_str,
                'equipment_id': equipment_id,
                'customer_id': customer_id,
                'status': status_filter,
                'payment_method': payment_method,
            }
        }
        return render(request, 'rentals/report_detailed_rentals.html', context)


@login_required
@custom_permission_required('rentals.view_rental')
def export_detailed_rentals_excel(request):
    import datetime
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    today = datetime.date.today()
    start_date_str = request.GET.get('start_date', '').strip()
    end_date_str = request.GET.get('end_date', '').strip()
    equipment_id = request.GET.get('equipment_id', '').strip()
    customer_id = request.GET.get('customer_id', '').strip()
    status_filter = request.GET.get('status', '').strip()
    payment_method = request.GET.get('payment_method', '').strip()

    qs = Rental.objects.select_related('equipment', 'customer', 'created_by').all()

    if start_date_str:
        try:
            s_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            qs = qs.filter(start_date__gte=s_date)
        except ValueError:
            pass

    if end_date_str:
        try:
            e_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            qs = qs.filter(start_date__lte=e_date)
        except ValueError:
            pass

    if equipment_id:
        qs = qs.filter(equipment_id=equipment_id)

    if customer_id:
        qs = qs.filter(customer_id=customer_id)

    if status_filter == 'ACTIVE':
        qs = qs.filter(status=Rental.STATUS_ACTIVE)
    elif status_filter == 'RETURNED':
        qs = qs.filter(status=Rental.STATUS_RETURNED)
    elif status_filter == 'OVERDUE':
        qs = qs.filter(status=Rental.STATUS_ACTIVE, end_date__lt=today)

    if payment_method in ['CASH', 'CREDIT']:
        qs = qs.filter(payment_method=payment_method)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "تقرير المعدات المؤجرة"
    ws.views.sheetView[0].rightToLeft = True

    title_font = Font(name='Segoe UI', size=16, bold=True, color='1F2937')
    header_font = Font(name='Segoe UI', size=11, bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='1E293B', end_color='1E293B', fill_type='solid')
    sub_font = Font(name='Segoe UI', size=10, italic=True, color='475569')
    data_font = Font(name='Segoe UI', size=10)
    bold_font = Font(name='Segoe UI', size=10, bold=True)
    border_thin = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    from apps.tenant.core.company import company_profile
    _org = company_profile(request)['name']
    ws.append([(f"🚜 {_org} — " if _org else "🚜 ") + "تقرير المعدات المؤجرة التفصيلي"])
    ws.cell(row=1, column=1).font = title_font
    ws.row_dimensions[1].height = 30

    date_label = f"تاريخ الاستخراج: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws.append([date_label])
    ws.cell(row=2, column=1).font = sub_font
    ws.append([])

    headers = [
        "رقم العقد", "اسم المعدة", "كود المعدة", "اسم العميل",
        "تاريخ البدء", "تاريخ العودة المتوقع", "عدد الأيام", "السعر اليومي",
        "إجمالي المبلغ", "المرتجع/المسترد", "الغرامات/التلفيات", "صافي المكسب/الإيراد",
        "طريقة الدفع", "حالة العقد"
    ]
    ws.append(headers)
    header_row_num = 4
    ws.row_dimensions[header_row_num].height = 25
    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=header_row_num, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')

    current_row = 5
    tot_days = 0
    tot_gross = Decimal('0')
    tot_refunds = Decimal('0')
    tot_penalties = Decimal('0')
    tot_net = Decimal('0')

    for r in qs.order_by('-start_date', '-id'):
        gross = r.total_amount or Decimal('0')
        refund = r.refund_amount or Decimal('0')
        penalty = r.penalty_amount or Decimal('0')
        net = gross - refund + penalty
        days = r.days_rented or 1

        if r.status == Rental.STATUS_ACTIVE:
            if r.end_date and r.end_date < today:
                st_display = "متأخر (تجاوز الموعد)"
            else:
                st_display = "نشط (مؤجرة)"
        else:
            st_display = "تم الإرجاع"

        pm_display = "كاش" if r.payment_method == Rental.PAYMENT_CASH else "آجل"

        row_data = [
            f"#{r.id}",
            r.equipment.name,
            r.equipment.code,
            r.customer.name,
            r.start_date.strftime('%Y-%m-%d') if r.start_date else '',
            r.end_date.strftime('%Y-%m-%d') if r.end_date else '',
            days,
            float(r.daily_rate or 0),
            float(gross),
            float(refund),
            float(penalty),
            float(net),
            pm_display,
            st_display
        ]
        ws.append(row_data)

        tot_days += days
        tot_gross += gross
        tot_refunds += refund
        tot_penalties += penalty
        tot_net += net

        ws.row_dimensions[current_row].height = 20
        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=current_row, column=col_num)
            cell.font = data_font
            cell.border = border_thin
            if col_num in [8, 9, 10, 11, 12]:
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal='right', vertical='center')
            elif col_num in [1, 5, 6, 7, 13, 14]:
                cell.alignment = Alignment(horizontal='center', vertical='center')
            else:
                cell.alignment = Alignment(horizontal='right', vertical='center')
        current_row += 1

    tot_row = [
        "الإجمالي الكلي", "", "", f"عدد العقود: {qs.count()}",
        "", "", tot_days, "",
        float(tot_gross), float(tot_refunds), float(tot_penalties), float(tot_net),
        "", ""
    ]
    ws.append(tot_row)
    ws.row_dimensions[current_row].height = 25
    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=current_row, column=col_num)
        cell.font = bold_font
        cell.fill = PatternFill(start_color='F1F5F9', end_color='F1F5F9', fill_type='solid')
        cell.border = border_thin
        if col_num in [9, 10, 11, 12]:
            cell.number_format = '#,##0.00'
            cell.alignment = Alignment(horizontal='right', vertical='center')
        else:
            cell.alignment = Alignment(horizontal='center', vertical='center')

    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or '')
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="Detailed_Rentals_Report_{today.strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response
