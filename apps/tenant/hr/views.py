import math
import json
import calendar
from decimal import Decimal
from datetime import datetime, date, timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from .models import Attendance, Payroll
from apps.tenant.core.models import Employee
from apps.tenant.core.decorators import custom_permission_required, is_admin_user
from .services import post_payroll
from apps.tenant.core.usernames import display_username

# An open attendance (checked-in, not checked-out) is matched for check-out as long as it
# started within this window, so overnight shifts work; older forgotten ones are ignored.
OPEN_ATTENDANCE_WINDOW_HOURS = 24

# Payroll defaults (company-wide, from SystemSetting via settings_helpers):
#   weekly_off_days    -> weekly off weekdays (default Friday)
#   payroll_day_basis  -> CALENDAR_30: daily rate = base_salary / 30 (default)
#                         WORKING_DAYS: daily rate = base_salary / working days of that month
#   late_grace_minutes -> minutes of each day's delay that are not deducted (default 0)
PAYROLL_DAYS_PER_MONTH = 30


def _weekly_off():
    from apps.tenant.core.settings_helpers import weekly_off_weekdays
    return weekly_off_weekdays()


def _payroll_settings():
    from apps.tenant.core.settings_helpers import setting
    try:
        grace = max(0, int(setting('late_grace_minutes', 0) or 0))
    except (TypeError, ValueError):
        grace = 0
    return {
        'weekly_off': _weekly_off(),
        'day_basis': setting('payroll_day_basis', 'CALENDAR_30') or 'CALENDAR_30',
        'grace_hours': Decimal(grace) / Decimal(60),
    }


def _daily_rate(base_salary, year, month, day_basis, weekly_off):
    if day_basis == 'WORKING_DAYS':
        days = len(_working_days(year, month, weekly_off=weekly_off))
        if days > 0:
            return base_salary / Decimal(days)
    return base_salary / Decimal(PAYROLL_DAYS_PER_MONTH)


def _deductible_delay(delays, grace_hours):
    """Sum of each day's delay minus the grace (a delay within the grace costs nothing)."""
    total = Decimal('0.00')
    for d in delays:
        d = d or Decimal('0')
        if d > grace_hours:
            total += d - grace_hours
    return total.quantize(Decimal('0.01'))


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


def _parse_coord(value, limit):
    """Parse a GPS coordinate; returns float or None when missing/invalid."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or abs(v) > limit:
        return None
    return v


def _open_attendance_qs(employee, now=None):
    now = now or timezone.now()
    return Attendance.objects.filter(
        employee=employee,
        check_in__isnull=False,
        check_out__isnull=True,
        check_in__gte=now - timedelta(hours=OPEN_ATTENDANCE_WINDOW_HOURS),
    ).order_by('-check_in')


def _scope_by_branch(qs, user, field):
    """Admins see everything; other users only their employee branch (nothing if none)."""
    if is_admin_user(user):
        return qs
    profile = getattr(user, 'employee_profile', None)
    branch = getattr(profile, 'branch', None)
    if not branch:
        return qs.none()
    return qs.filter(**{field: branch})


@login_required
def attendance_dashboard(request):
    try:
        employee = request.user.employee_profile
    except Employee.DoesNotExist:
        messages.error(request, 'حسابك غير مرتبط بملف موظف.')
        return redirect('dashboard:index')

    today = timezone.localdate()
    # Prefer an open attendance (e.g. overnight shift started yesterday) so check-out stays available
    attendance = (_open_attendance_qs(employee).first()
                  or Attendance.objects.filter(employee=employee, date=today).first())

    context = {
        'title': 'سجل الحضور والانصراف',
        'employee': employee,
        'attendance': attendance,
    }
    return render(request, 'hr/attendance.html', context)


@login_required
@require_POST
def api_attendance_check(request):
    """
    NOTE: the geofence relies on coordinates reported by the browser. They can be spoofed
    client-side (devtools / fake-GPS apps), so this is a convenience control, not a security boundary.
    """
    try:
        employee = request.user.employee_profile
    except Employee.DoesNotExist:
        return JsonResponse({'error': 'حسابك غير مرتبط بملف موظف.'}, status=400)

    action = request.POST.get('action')  # 'check_in' or 'check_out'
    user_lat = _parse_coord(request.POST.get('latitude'), 90)
    user_lon = _parse_coord(request.POST.get('longitude'), 180)

    if user_lat is None or user_lon is None:
        return JsonResponse({'error': 'لم يتم العثور على الإحداثيات أو أنها غير صالحة.'}, status=400)

    branch = employee.branch
    if not branch:
        return JsonResponse({'error': 'غير مسجل في أي فرع.'}, status=400)

    if branch.latitude is None or branch.longitude is None:
        return JsonResponse({'error': f'الفرع {branch.name} غير محدد له إحداثيات GPS من الإدارة.'}, status=400)

    distance = haversine(user_lat, user_lon, branch.latitude, branch.longitude)
    allowed_radius = branch.allowed_radius or 50

    if distance > allowed_radius:
        return JsonResponse({'error': f'أنت بعيد جداً عن فرع {branch.name}. المسافة: {distance} متر (المسموح {allowed_radius} متر).'}, status=400)

    now_time = timezone.now()
    today = timezone.localdate(now_time)

    if action == 'check_in':
        with transaction.atomic():
            if _open_attendance_qs(employee, now_time).select_for_update(of=('self',)).exists():
                return JsonResponse({'error': 'لديك حضور مفتوح، يجب تسجيل الانصراف أولاً.'}, status=400)
            attendance, created = Attendance.objects.select_for_update(of=('self',)).get_or_create(employee=employee, date=today)
            if attendance.check_in:
                return JsonResponse({'error': 'لقد قمت بتسجيل الحضور مسبقاً.'}, status=400)

            attendance.check_in = now_time
            attendance.status = 'present'

            # Calculate delay (shift start is local wall-clock time)
            if employee.shift_start_time:
                start_datetime = timezone.make_aware(datetime.combine(today, employee.shift_start_time))
                if now_time > start_datetime:
                    delay_secs = (now_time - start_datetime).total_seconds()
                    attendance.delay_hours = Decimal(delay_secs / 3600.0).quantize(Decimal('0.01'))

            attendance.save()
        return JsonResponse({'status': 'success', 'message': 'تم تسجيل الحضور بنجاح.'})

    elif action == 'check_out':
        with transaction.atomic():
            attendance = _open_attendance_qs(employee, now_time).select_for_update(of=('self',)).first()
            if attendance is None:
                today_att = Attendance.objects.filter(employee=employee, date=today).first()
                if today_att and today_att.check_out:
                    return JsonResponse({'error': 'لقد قمت بتسجيل الانصراف مسبقاً.'}, status=400)
                return JsonResponse({'error': 'يجب تسجيل الحضور أولاً.'}, status=400)

            attendance.check_out = now_time

            # Overtime is measured against the shift end of the attendance's own day
            if employee.shift_end_time:
                end_datetime = timezone.make_aware(datetime.combine(attendance.date, employee.shift_end_time))
                if employee.shift_start_time and employee.shift_end_time <= employee.shift_start_time:
                    end_datetime += timedelta(days=1)  # overnight shift ends the next day
                if now_time > end_datetime:
                    overtime_secs = (now_time - end_datetime).total_seconds()
                    attendance.overtime_hours = Decimal(overtime_secs / 3600.0).quantize(Decimal('0.01'))

            attendance.save()
        return JsonResponse({'status': 'success', 'message': 'تم تسجيل الانصراف بنجاح.'})

    return JsonResponse({'error': 'عملية غير معروفة.'}, status=400)


def _working_days(year, month, until=None, since=None, weekly_off=None):
    """Dates of the month that are working days (not weekly off), bounded by [since, until]."""
    if weekly_off is None:
        weekly_off = _weekly_off()
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    if until and until < last:
        last = until
    if since and since > first:
        first = since
    days = []
    d = first
    while d <= last:
        if d.weekday() not in weekly_off:
            days.append(d)
        d += timedelta(days=1)
    return days


@custom_permission_required('hr.view_payroll', redirect_url='core:main_screen')
def payroll_list(request):
    from django.core.paginator import Paginator
    payrolls = Payroll.objects.select_related('employee__user', 'employee__branch', 'journal_entry').order_by('-year', '-month')
    payrolls = _scope_by_branch(payrolls, request.user, 'employee__branch')
    paginator = Paginator(payrolls, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    context = {
        'title': 'إدارة مسيرات الرواتب',
        'payrolls': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_count': paginator.count,
    }
    return render(request, 'hr/payroll_list.html', context)


@custom_permission_required('hr.add_payroll', redirect_url='core:main_screen')
@require_POST
def payroll_process(request):
    """
    Generate the payroll of a month.

    Absence deduction (assumption - no per-employee calendar exists in the models):
      working days = days of the month (only up to today for the current month, and only
                     from the user's date_joined if later) excluding the weekly off days
                     (SystemSetting.weekly_off_days, default Friday)
      covered days = distinct dates with a check-in + dates whose status is 'leave' (excused)
      absent days  = working days - covered days
      absence deduction = absent days * daily rate, where daily rate = base_salary / 30
                          (payroll_day_basis CALENDAR_30) or / working days of the whole month
                          (WORKING_DAYS)
      delay deduction   = sum over days of max(0, delay - late_grace_minutes) * deduction_per_hour
    net = base - delay deductions - absence deduction + overtime, clamped at 0 (with a warning).
    Payrolls already paid / posted to a journal entry are never overwritten.
    """
    today = timezone.localdate()
    try:
        month = int(request.POST.get('month') or today.month)
        year = int(request.POST.get('year') or today.year)
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            raise ValueError
    except (TypeError, ValueError):
        messages.error(request, 'الشهر أو السنة غير صالحة.')
        return redirect('hr:payroll_list')

    if date(year, month, 1) > today:
        messages.error(request, 'لا يمكن إصدار مسير رواتب لشهر مستقبلي.')
        return redirect('hr:payroll_list')

    employees = _scope_by_branch(Employee.objects.select_related('user', 'branch'), request.user, 'branch')
    cfg = _payroll_settings()

    count = 0
    skipped = []
    clamped = []
    for emp in employees:
        if emp.base_salary <= 0:
            continue

        with transaction.atomic():
            existing = Payroll.objects.select_for_update(of=('self',)).filter(employee=emp, month=month, year=year).first()
            if existing and (existing.is_paid or existing.journal_entry_id):
                skipped.append(display_username(emp.user))
                continue

            attendances = Attendance.objects.filter(employee=emp, date__year=year, date__month=month)
            att_agg = attendances.aggregate(
                total_overtime=Coalesce(Sum('overtime_hours'), Decimal('0.00'))
            )
            # deductible delay: each day's delay minus the grace period
            total_delay = _deductible_delay(attendances.values_list('delay_hours', flat=True), cfg['grace_hours'])
            total_overtime = att_agg['total_overtime']

            joined = timezone.localdate(emp.user.date_joined) if emp.user.date_joined else None
            working_days = _working_days(year, month, until=today, since=joined, weekly_off=cfg['weekly_off'])
            present_dates = set(attendances.filter(check_in__isnull=False).values_list('date', flat=True))
            leave_dates = set(attendances.filter(status='leave').values_list('date', flat=True))
            absent_days = sum(1 for d in working_days if d not in present_dates and d not in leave_dates)
            daily_rate = _daily_rate(emp.base_salary, year, month, cfg['day_basis'], cfg['weekly_off'])
            absence_deduction = (daily_rate * absent_days).quantize(Decimal('0.01'))

            deductions = (total_delay * emp.deduction_per_hour + absence_deduction).quantize(Decimal('0.01'))
            overtime_pay = (total_overtime * emp.overtime_per_hour).quantize(Decimal('0.01'))

            net_salary = emp.base_salary - deductions + overtime_pay
            if net_salary < 0:
                clamped.append(display_username(emp.user))
                net_salary = Decimal('0.00')

            Payroll.objects.update_or_create(
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
    if skipped:
        messages.warning(request, f'تم تجاهل {len(skipped)} مسير(ات) معتمدة/مصروفة مسبقاً: {", ".join(skipped)}')
    if clamped:
        messages.warning(request, f'الخصومات تجاوزت المستحق فتم تصفير صافي الراتب لـ: {", ".join(clamped)}')
    return redirect('hr:payroll_list')


@custom_permission_required('hr.change_payroll', redirect_url='core:main_screen')
@require_POST
def payroll_confirm(request, pk):
    qs = _scope_by_branch(Payroll.objects.all(), request.user, 'employee__branch')
    payroll = get_object_or_404(qs, pk=pk)

    try:
        with transaction.atomic():
            payroll = Payroll.objects.select_for_update(of=('self',)).get(pk=payroll.pk)
            if payroll.is_paid or payroll.journal_entry_id:
                messages.info(request, 'هذا المسير معتمد مسبقاً.')
                return redirect('hr:payroll_list')
            entry = post_payroll(payroll)
            if entry:
                payroll.is_paid = True
                payroll.paid_at = timezone.now()
                payroll.save(update_fields=['is_paid', 'paid_at'])
                messages.success(request, f'تم الاعتماد المحاسبي (قيد استحقاق رواتب) برقم {entry.reference}.')
            else:
                messages.warning(request, 'فشل توليد القيد أو صافي الراتب صفر.')
    except Exception as e:
        messages.error(request, f'خطأ أثناء الاعتماد: {e}')

    return redirect('hr:payroll_list')
