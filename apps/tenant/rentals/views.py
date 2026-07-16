from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, CreateView, UpdateView, View, DetailView
from apps.tenant.core.mixins import CustomPermissionRequiredMixin
from .models import Equipment, Rental
from .forms import EquipmentForm, RentalForm

class RentalPrintView(LoginRequiredMixin, DetailView):
    model = Rental
    template_name = 'rentals/rental_print.html'
    context_object_name = 'rental'
    
    def get_queryset(self):
        return Rental.objects.select_related('equipment', 'customer', 'created_by')

class EquipmentListView(LoginRequiredMixin, ListView):
    model = Equipment
    template_name = 'rentals/equipment_list.html'
    context_object_name = 'equipments'

class EquipmentCreateView(LoginRequiredMixin, CreateView):
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

class EquipmentUpdateView(LoginRequiredMixin, UpdateView):
    model = Equipment
    form_class = EquipmentForm
    template_name = 'rentals/equipment_form.html'
    success_url = reverse_lazy('rentals:equipment_list')

    def form_valid(self, form):
        messages.success(self.request, 'تم تحديث المعدة بنجاح.')
        return super().form_valid(form)

class RentalListView(LoginRequiredMixin, ListView):
    model = Rental
    template_name = 'rentals/rental_list.html'
    context_object_name = 'rentals'
    
    def get_queryset(self):
        return Rental.objects.select_related('equipment', 'customer').order_by('-created_at')

class RentalCreateView(LoginRequiredMixin, CreateView):
    model = Rental
    form_class = RentalForm
    template_name = 'rentals/rental_form.html'
    success_url = reverse_lazy('rentals:rental_list')

    def form_valid(self, form):
        from django.db import transaction
        rental = form.save(commit=False)
        rental.created_by = self.request.user
        
        try:
            with transaction.atomic():
                rental.save()
                rental.post_rental()
            messages.success(self.request, 'تم إنشاء عقد الإيجار وتسجيل القيود المحاسبية بنجاح.')
            return redirect(f"{self.success_url}?print_contract={rental.id}")
        except Exception as e:
            form.add_error(None, str(e))
            return self.form_invalid(form)

class RentalDetailView(LoginRequiredMixin, View):
    def get(self, request, pk):
        rental = get_object_or_404(Rental.objects.select_related('equipment', 'customer', 'accrual_journal_entry', 'payment_journal_entry'), pk=pk)
        return render(request, 'rentals/rental_detail.html', {'rental': rental})

class RentalReturnView(LoginRequiredMixin, View):
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
                ewallet=ewallet
            )
            messages.success(request, 'تم تسجيل عودة المعدة بنجاح وأصبحت متاحة للإيجار.')
            return redirect('rentals:rental_list')
        except Exception as e:
            messages.error(request, f'خطأ: {str(e)}')
            return redirect('rentals:rental_return', pk=pk)

from django.db.models import Sum, Count

class RentalReportsView(LoginRequiredMixin, View):
    def get(self, request):
        # 1. حالة المعدات
        equipment_status = Equipment.objects.values('status').annotate(count=Count('id'))
        
        # 2. الإيجارات النشطة (مواعيد العودة)
        active_rentals = Rental.objects.filter(status=Rental.STATUS_ACTIVE).order_by('end_date')
        
        from django.db.models import F
        # 3. ربحية المعدات
        equipment_profitability = Equipment.objects.annotate(
            total_revenue=Sum(F('rentals__total_amount') - F('rentals__refund_amount'))
        ).order_by('-total_revenue')

        # 4. إجمالي أرباح الإيجارات
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

class EquipmentUtilizationReportView(LoginRequiredMixin, View):
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
                    # If returned early, the updated_at is the return date
                    days = (r.updated_at.date() - r.start_date).days
                    actual_rented_days += max(1, days)
                else:
                    # Still active
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

class OverdueRentalsReportView(LoginRequiredMixin, View):
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
