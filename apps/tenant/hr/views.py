import math
import json
from decimal import Decimal
from datetime import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.utils import timezone
from .models import Attendance, Payroll
from apps.tenant.core.models import Employee
from .services import post_payroll

def haversine(lat1, lon1, lat2, lon2):
    """Calculate distance in meters between two GPS points"""
    R = 6371000
    phi_1 = math.radians(float(lat1))
    phi_2 = math.radians(float(lat2))
    delta_phi = math.radians(float(lat2) - float(lat1))
    delta_lambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi_1) * math.cos(phi_2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return int(R * c)

@login_required
def attendance_dashboard(request):
    try:
        employee = request.user.employee_profile
    except Employee.DoesNotExist:
        messages.error(request, 'حسابك غير مرتبط بملف موظف.')
        return redirect('dashboard:index')

    today = timezone.now().date()
    attendance = Attendance.objects.filter(employee=employee, date=today).first()
    
    context = {
        'title': 'سجل الحضور والانصراف',
        'employee': employee,
        'attendance': attendance,
    }
    return render(request, 'hr/attendance.html', context)

@login_required
@require_POST
def api_attendance_check(request):
    try:
        employee = request.user.employee_profile
    except Employee.DoesNotExist:
        return JsonResponse({'error': 'حسابك غير مرتبط بملف موظف.'}, status=400)
        
    action = request.POST.get('action') # 'check_in' or 'check_out'
    user_lat = request.POST.get('latitude')
    user_lon = request.POST.get('longitude')
    
    if not user_lat or not user_lon:
        return JsonResponse({'error': 'لم يتم العثور على الإحداثيات.'}, status=400)
        
    branch = employee.branch
    if not branch:
        return JsonResponse({'error': 'غير مسجل في أي فرع.'}, status=400)
        
    if not branch.latitude or not branch.longitude:
        return JsonResponse({'error': f'الفرع {branch.name} غير محدد له إحداثيات GPS من الإدارة.'}, status=400)
        
    distance = haversine(user_lat, user_lon, branch.latitude, branch.longitude)
    allowed_radius = branch.allowed_radius or 50
    
    if distance > allowed_radius:
        return JsonResponse({'error': f'أنت بعيد جداً عن فرع {branch.name}. المسافة: {distance} متر (المسموح {allowed_radius} متر).'}, status=400)
        
    today = timezone.now().date()
    now_time = timezone.now()
    attendance, created = Attendance.objects.get_or_create(employee=employee, date=today)
    
    if action == 'check_in':
        if attendance.check_in:
            return JsonResponse({'error': 'لقد قمت بتسجيل الحضور مسبقاً.'}, status=400)
            
        attendance.check_in = now_time
        
        # Calculate delay
        if employee.shift_start_time:
            start_datetime = timezone.make_aware(datetime.combine(today, employee.shift_start_time))
            if now_time > start_datetime:
                delay_secs = (now_time - start_datetime).total_seconds()
                attendance.delay_hours = Decimal(delay_secs / 3600.0).quantize(Decimal('0.01'))
                
        attendance.save()
        return JsonResponse({'status': 'success', 'message': 'تم تسجيل الحضور بنجاح.'})
        
    elif action == 'check_out':
        if not attendance.check_in:
            return JsonResponse({'error': 'يجب تسجيل الحضور أولاً.'}, status=400)
        if attendance.check_out:
            return JsonResponse({'error': 'لقد قمت بتسجيل الانصراف مسبقاً.'}, status=400)
            
        attendance.check_out = now_time
        
        # Calculate overtime
        if employee.shift_end_time:
            end_datetime = timezone.make_aware(datetime.combine(today, employee.shift_end_time))
            if now_time > end_datetime:
                overtime_secs = (now_time - end_datetime).total_seconds()
                attendance.overtime_hours = Decimal(overtime_secs / 3600.0).quantize(Decimal('0.01'))
                
        attendance.save()
        return JsonResponse({'status': 'success', 'message': 'تم تسجيل الانصراف بنجاح.'})
        
    return JsonResponse({'error': 'عملية غير معروفة.'}, status=400)

@login_required
def payroll_list(request):
    if not request.user.employee_profile.is_admin() and not request.user.employee_profile.is_manager():
        messages.error(request, 'ليس لديك صلاحية الدخول.')
        return redirect('dashboard:index')

    payrolls = Payroll.objects.all().order_by('-year', '-month')
    context = {
        'title': 'إدارة مسيرات الرواتب',
        'payrolls': payrolls,
    }
    return render(request, 'hr/payroll_list.html', context)

@login_required
@require_POST
def payroll_process(request):
    if not request.user.employee_profile.is_admin():
        return redirect('hr:payroll_list')
        
    month = int(request.POST.get('month', timezone.now().month))
    year = int(request.POST.get('year', timezone.now().year))
    
    employees = Employee.objects.all()
    count = 0
    for emp in employees:
        if emp.base_salary <= 0:
            continue
            
        attendances = Attendance.objects.filter(employee=emp, date__year=year, date__month=month)
        
        total_delay = sum(a.delay_hours for a in attendances)
        total_overtime = sum(a.overtime_hours for a in attendances)
        
        deductions = total_delay * emp.deduction_per_hour
        overtime_pay = total_overtime * emp.overtime_per_hour
        
        net_salary = emp.base_salary - deductions + overtime_pay
        
        payroll, created = Payroll.objects.update_or_create(
            employee=emp,
            month=month,
            year=year,
            defaults={
                'total_delay_hours': total_delay,
                'total_overtime_hours': total_overtime,
                'base_pay': emp.base_salary,
                'overtime_pay': overtime_pay,
                'deductions': deductions,
                'net_salary': net_salary,
            }
        )
        count += 1
        
    messages.success(request, f'تم إصدار مسير الرواتب لعدد {count} موظفين.')
    return redirect('hr:payroll_list')

@login_required
def payroll_confirm(request, pk):
    if not request.user.employee_profile.is_admin():
        return redirect('hr:payroll_list')
        
    payroll = get_object_or_404(Payroll, pk=pk)
    if not payroll.is_paid:
        entry = post_payroll(payroll)
        if entry:
            payroll.is_paid = True
            payroll.paid_at = timezone.now()
            payroll.save()
            messages.success(request, f'تم الاعتماد المحاسبي (قيد استحقاق رواتب) برقم {entry.reference}.')
        else:
            messages.warning(request, 'فشل توليد القيد أو صافي الراتب صفر.')
    
    return redirect('hr:payroll_list')
