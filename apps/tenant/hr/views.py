import math
import json
import calendar
from decimal import ROUND_HALF_UP, Decimal
from datetime import datetime, date, timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from .models import Attendance, Payroll
from apps.tenant.core.models import Employee
from apps.tenant.core.decorators import custom_permission_required, is_admin_user
from .services import post_payroll
from .attendance_services import compute_hours
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
            attendance.source = Attendance.SOURCE_GPS
            compute_hours(attendance)   # delay against the shift start (same rule as manual entry)
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
            compute_hours(attendance)   # overtime against the shift end of the record's own day
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


def _period(request, default=None):
    """(year, month) from ?year=&month= (validated), else ``default`` or the current month."""
    today = timezone.localdate()
    year, month = default or (today.year, today.month)
    try:
        y, m = int(request.GET.get('year') or year), int(request.GET.get('month') or month)
        if 2000 <= y <= 2100 and 1 <= m <= 12:
            year, month = y, m
    except (TypeError, ValueError):
        pass
    return year, month


def _period_choices(year):
    from .attendance_services import AR_MONTHS
    this_year = timezone.localdate().year
    return {
        'month_choices': list(enumerate(AR_MONTHS, start=1)),
        'year_choices': list(range(min(year, this_year - 3), this_year + 1)),
    }


@custom_permission_required('hr.view_payroll', redirect_url='core:main_screen')
def payroll_list(request):
    """The payroll of ONE month: totals on top and, per employee, the attendance behind
    the figures (working / present / leave / absent days, absence and delay deductions)."""
    from django.db.models import Count, Q
    from .attendance_services import AR_MONTHS
    scoped = _scope_by_branch(Payroll.objects.all(), request.user, 'employee__branch')
    today = timezone.localdate()
    default = (today.year, today.month)
    if 'month' not in request.GET and not scoped.filter(year=today.year, month=today.month).exists():
        latest = scoped.order_by('-year', '-month').values_list('year', 'month').first()
        default = latest or default
    year, month = _period(request, default)

    payrolls = list(scoped.filter(year=year, month=month)
                    .select_related('employee__user', 'employee__branch', 'journal_entry')
                    .order_by('employee__branch__name', 'employee__user__first_name', 'employee__user__username'))
    totals = scoped.filter(year=year, month=month).aggregate(
        n=Count('id'),
        paid=Count('id', filter=Q(is_paid=True)),
        base=Coalesce(Sum('base_pay'), Decimal('0')),
        absence=Coalesce(Sum('absence_deduction'), Decimal('0')),
        delay=Coalesce(Sum('delay_deduction'), Decimal('0')),
        deductions=Coalesce(Sum('deductions'), Decimal('0')),
        overtime=Coalesce(Sum('overtime_pay'), Decimal('0')),
        loans=Coalesce(Sum('loan_deduction'), Decimal('0')),
        insurance=Coalesce(Sum('social_insurance'), Decimal('0')),
        employer_insurance=Coalesce(Sum('employer_insurance'), Decimal('0')),
        tax=Coalesce(Sum('income_tax'), Decimal('0')),
        net=Coalesce(Sum('net_salary'), Decimal('0')),
        absent_days=Coalesce(Sum('absent_days'), 0),
    )
    periods = list(scoped.order_by('-year', '-month').values_list('year', 'month').distinct()[:24])
    from apps.tenant.accounting.forms import scoped_treasuries
    from apps.tenant.accounting.models import BankAccount
    from . import payroll_payments as pay
    from .models import PayrollPayment
    month_qs = scoped.filter(year=year, month=month)
    unpaid = pay.unpaid_salaries(month_qs).aggregate(n=Count('id'), s=Coalesce(Sum('net_salary'), Decimal('0')))
    _, gov_insurance, gov_tax = pay.government_due(month_qs)
    paid_ids = set(PayrollPayment.objects.filter(kind=PayrollPayment.SALARIES, payrolls__in=month_qs)
                   .values_list('payrolls__id', flat=True))
    for p in payrolls:
        p.salary_paid = p.id in paid_ids
    context = {
        'title': 'مسير الرواتب',
        'year': year, 'month': month, 'month_name': AR_MONTHS[month - 1],
        'payrolls': payrolls,
        'totals': totals,
        'periods': [{'year': y, 'month': m, 'label': f'{AR_MONTHS[m - 1]} {y}'} for y, m in periods],
        'is_future': (year, month) > (today.year, today.month),
        'unpaid': unpaid, 'gov_insurance': gov_insurance, 'gov_tax': gov_tax,
        'gov_total': gov_insurance + gov_tax,
        'payments': PayrollPayment.objects.filter(year=year, month=month).select_related(
            'treasury', 'bank_account', 'journal_entry', 'created_by').distinct(),
        'treasuries': scoped_treasuries(request.user), 'banks': BankAccount.objects.all(),
        'can_pay': request.user.has_perm('hr.add_payrollpayment'),
        **_period_choices(year),
    }
    return render(request, 'hr/payroll_list.html', context)


@custom_permission_required('hr.add_payrollpayment', redirect_url='core:main_screen')
@require_POST
def payroll_pay(request):
    """صرف مرتبات الشهر المعتمدة / سداد التأمينات والضرائب عن الشهر."""
    from django.core.exceptions import ValidationError
    from . import payroll_payments as pay
    try:
        year, month = int(request.POST.get('year')), int(request.POST.get('month'))
    except (TypeError, ValueError):
        messages.error(request, 'الشهر غير صالح.')
        return redirect('hr:payroll_list')
    scoped = _scope_by_branch(Payroll.objects.all(), request.user, 'employee__branch')
    try:
        treasury, bank = pay.parse_source(request.POST.get('source'), request.user)
        if request.POST.get('kind') == 'government':
            p = pay.pay_government(scoped, year, month, treasury, bank, user=request.user)
            messages.success(request, f'اتسددت التأمينات والضرايب عن {month}/{year}: {p.amount} (تأمينات {p.insurance_amount} + ضريبة {p.tax_amount}).')
        else:
            p = pay.pay_salaries(scoped, year, month, treasury, bank, user=request.user)
            messages.success(request, f'اتصرفت مرتبات {month}/{year}: {p.amount} لـ {p.payrolls.count()} موظف.')
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
    return redirect(f"{reverse('hr:payroll_list')}?month={month}&year={year}")


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

    employees = list(_scope_by_branch(Employee.objects.select_related('user', 'branch'), request.user, 'branch')
                     .filter(base_salary__gt=0))
    cfg = _payroll_settings()
    from .attendance_services import month_summary
    # attendance of every employee in 2 queries (shared with the attendance report)
    summary = month_summary(employees, year, month, until=today, weekly_off=cfg['weekly_off'],
                            grace_hours=cfg['grace_hours'])
    from .loan_services import apply_to_payroll, due_installments
    month_payroll_ids = list(Payroll.objects.filter(employee__in=employees, year=year, month=month)
                             .values_list('id', flat=True))
    loan_dues = due_installments(employees, year, month, exclude_payroll_ids=month_payroll_ids)
    from . import payroll_tax
    from .models import PayrollTaxSettings
    from apps.tenant.invoicing.rep_services import commissions_for_payroll
    tax_settings = PayrollTaxSettings.get()
    commissions = commissions_for_payroll(employees, year, month)

    count = 0
    skipped = []
    clamped = []
    for emp in employees:
        with transaction.atomic():
            existing = Payroll.objects.select_for_update(of=('self',)).filter(employee=emp, month=month, year=year).first()
            if existing and (existing.is_paid or existing.journal_entry_id):
                skipped.append(display_username(emp.user))
                continue

            att = summary[emp.id]
            total_delay = att['deductible_delay']
            total_overtime = att['overtime_hours']
            daily_rate = _daily_rate(emp.base_salary, year, month, cfg['day_basis'], cfg['weekly_off'])
            absence_deduction = (daily_rate * att['absent_days']).quantize(Decimal('0.01'))
            delay_deduction = (total_delay * emp.deduction_per_hour).quantize(Decimal('0.01'))

            deductions = delay_deduction + absence_deduction
            overtime_pay = (total_overtime * emp.overtime_per_hour).quantize(Decimal('0.01'))

            commission_pay = commissions.get(emp.id) or Decimal('0.00')
            net_salary = emp.base_salary - deductions + overtime_pay + commission_pay
            if net_salary < 0:
                clamped.append(display_username(emp.user))
                net_salary = Decimal('0.00')
            # social insurance (employee share) and salary tax come off the gross
            tx = payroll_tax.compute(emp, net_salary, tax_settings)
            net_salary = max(net_salary - tx['social_insurance'] - tx['income_tax'], Decimal('0.00'))

            payroll, _ = Payroll.objects.update_or_create(
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
                    'working_days': att['working_days'],
                    'present_days': att['present_days'],
                    'leave_days': att['leave_days'],
                    'absent_days': att['absent_days'],
                    'absence_deduction': absence_deduction,
                    'delay_deduction': delay_deduction,
                    'social_insurance': tx['social_insurance'],
                    'employer_insurance': tx['employer_insurance'],
                    'income_tax': tx['income_tax'],
                    'commission_pay': commission_pay,
                }
            )
            # loan instalments of the month (never below a zero net; the rest carries over)
            loan_total = apply_to_payroll(payroll, loan_dues.get(emp.id, []), net_salary)
            payroll.loan_deduction = loan_total
            payroll.net_salary = net_salary - loan_total
            payroll.save(update_fields=['loan_deduction', 'net_salary'])
            count += 1

    messages.success(request, f'تم إصدار مسير الرواتب لعدد {count} موظفين.')
    if skipped:
        messages.warning(request, f'تم تجاهل {len(skipped)} مسير(ات) معتمدة/مصروفة مسبقاً: {", ".join(skipped)}')
    if clamped:
        messages.warning(request, f'الخصومات تجاوزت المستحق فتم تصفير صافي الراتب لـ: {", ".join(clamped)}')
    return redirect(f"{reverse('hr:payroll_list')}?month={month}&year={year}")


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
                from .loan_services import settle_paid_loans
                settle_paid_loans(payroll)      # loans fully repaid by this payroll are closed
                messages.success(request, f'تم الاعتماد المحاسبي (قيد استحقاق رواتب) برقم {entry.reference}.')
            else:
                messages.warning(request, 'فشل توليد القيد أو صافي الراتب صفر.')
    except Exception as e:
        messages.error(request, f'خطأ أثناء الاعتماد: {e}')

    return redirect(f"{reverse('hr:payroll_list')}?month={payroll.month}&year={payroll.year}")


# ---------------------------------------------------------------------------
# Manual attendance sheet (managers): works without the location check-in, e.g. when the
# system runs locally. Same delay / overtime rules as the check-in (attendance_services).
# ---------------------------------------------------------------------------
def _parse_day(value, default):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date() if value else default
    except (TypeError, ValueError):
        return default


def _parse_clock(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:5], '%H:%M').time()
    except ValueError:
        raise ValueError(f'وقت غير صالح: {value}')


def _clock(dt):
    return timezone.localtime(dt).strftime('%H:%M') if dt else ''


@custom_permission_required('hr.change_attendance', redirect_url='core:main_screen')
def attendance_manage(request):
    from .attendance_services import AttendanceError, fill_period_present, locked_months, save_manual_day

    today = timezone.localdate()
    employees = list(_scope_by_branch(
        Employee.objects.filter(user__is_active=True).select_related('user', 'branch'), request.user, 'branch'
    ).order_by('branch__name', 'user__first_name', 'user__username'))
    by_id = {e.id: e for e in employees}

    if request.method == 'POST' and request.POST.get('action') == 'fill_period':
        date_from = _parse_day(request.POST.get('date_from'), None)
        date_to = _parse_day(request.POST.get('date_to'), None)
        if not date_from or not date_to:
            messages.error(request, 'حدد بداية ونهاية الفترة.')
            return redirect('hr:attendance_manage')
        chosen = [by_id[int(i)] for i in request.POST.getlist('employees') if i.isdigit() and int(i) in by_id]
        created, no_shift = fill_period_present(chosen or employees, date_from, date_to, user=request.user)
        messages.success(request, f'تم تسجيل {created} يوم حضور بمواعيد الوردية (الأيام اللي كان ليها تسجيل ماتغيرتش).')
        if no_shift:
            messages.warning(request, 'موظفين من غير مواعيد وردية ماتسجلوش: '
                             + '، '.join(display_username(e.user) for e in no_shift)
                             + ' — حدد مواعيد الحضور والانصراف من شاشة المستخدم.')
        return redirect(f"{request.path}?date={date_to.isoformat()}")

    day = _parse_day(request.POST.get('date') or request.GET.get('date'), today)
    if day > today:
        day = today

    if request.method == 'POST':
        saved, errors = 0, []
        for emp in employees:
            key = str(emp.id)
            status = request.POST.get(f'status_{key}', '')
            fields = (status, request.POST.get(f'in_{key}', ''), request.POST.get(f'out_{key}', ''),
                      request.POST.get(f'notes_{key}', '').strip())
            if not status or '|'.join(fields) == request.POST.get(f'orig_{key}', ''):
                continue   # row not touched: a location check-in stays exactly as it was
            try:
                save_manual_day(emp, day, status, check_in=_parse_clock(fields[1]), check_out=_parse_clock(fields[2]),
                                notes=fields[3], user=request.user)
                saved += 1
            except (AttendanceError, ValueError) as e:
                errors.append(f'{display_username(emp.user)}: {e}')
        if saved:
            messages.success(request, f'تم حفظ حضور {saved} موظف ليوم {day:%Y-%m-%d}.')
        for err in errors:
            messages.error(request, err)
        if not saved and not errors:
            messages.info(request, 'مفيش تغييرات للحفظ.')
        return redirect(f"{request.path}?date={day.isoformat()}")

    records = {a.employee_id: a for a in Attendance.objects.filter(employee__in=employees, date=day)
               .select_related('entered_by')}
    locked = locked_months(employees, [day])
    rows = []
    for emp in employees:
        att = records.get(emp.id)
        status = att.status if att else ''
        in_t, out_t = (_clock(att.check_in), _clock(att.check_out)) if att else ('', '')
        notes = att.notes if att else ''
        rows.append({
            'employee': emp, 'att': att, 'status': status, 'in': in_t, 'out': out_t, 'notes': notes,
            'orig': '|'.join((status, in_t, out_t, notes)),
            'shift_in': emp.shift_start_time.strftime('%H:%M') if emp.shift_start_time else '',
            'shift_out': emp.shift_end_time.strftime('%H:%M') if emp.shift_end_time else '',
            'locked': (emp.id, day.year, day.month) in locked,
        })
    is_off_day = day.weekday() in _weekly_off()
    context = {
        'title': 'إدخال الحضور اليدوي',
        'day': day,
        'prev_day': day - timedelta(days=1),
        'next_day': day + timedelta(days=1) if day < today else None,
        'today': today,
        'rows': rows,
        'is_off_day': is_off_day,
        'counts': {
            'present': sum(1 for r in rows if r['status'] == 'present'),
            'leave': sum(1 for r in rows if r['status'] == 'leave'),
            'absent': sum(1 for r in rows if r['status'] == 'absent'),
            'empty': sum(1 for r in rows if not r['status']),
        },
        'month_start': day.replace(day=1),
    }
    return render(request, 'hr/attendance_manage.html', context)


# ---------------------------------------------------------------------------
# Attendance / absence / payroll report (one month). Salary columns only for users who
# may see payrolls. ?employee=<id> shows that employee's month day by day.
# ---------------------------------------------------------------------------
WEEKDAY_NAMES = ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد']


def _employee_name(emp):
    return emp.user.get_full_name() or display_username(emp.user)


@custom_permission_required('hr.view_attendance', redirect_url='core:main_screen')
def attendance_report(request):
    from .attendance_services import AR_MONTHS, month_summary

    today = timezone.localdate()
    year, month = _period(request)
    cfg = _payroll_settings()
    show_pay = is_admin_user(request.user) or request.user.has_perm('hr.view_payroll')
    employees = list(_scope_by_branch(
        Employee.objects.select_related('user', 'branch'), request.user, 'branch'
    ).order_by('branch__name', 'user__first_name', 'user__username'))
    month_name = f'{AR_MONTHS[month - 1]} {year}'

    emp_id = request.GET.get('employee') or ''
    if emp_id.isdigit():
        selected = next((e for e in employees if e.id == int(emp_id)), None)
        if selected is not None:
            return _attendance_employee_month(request, selected, year, month, month_name, cfg, show_pay)

    summary = month_summary(employees, year, month, until=today, weekly_off=cfg['weekly_off'],
                            grace_hours=cfg['grace_hours'])
    payrolls = ({p.employee_id: p for p in Payroll.objects.filter(employee__in=employees, year=year, month=month)}
                if show_pay else {})
    rows = []
    for emp in employees:
        s = summary[emp.id]
        # not employed in that month (hired later / deactivated) and nothing recorded: left out
        if (not s['working_days'] or not emp.user.is_active) and not s['present_days'] and not s['leave_days']:
            continue
        rows.append({'employee': emp, 'name': _employee_name(emp), **s, 'payroll': payrolls.get(emp.id)})

    totals = {k: sum((r[k] for r in rows), 0) for k in ('working_days', 'present_days', 'leave_days', 'absent_days')}
    totals.update({k: sum((r[k] for r in rows), Decimal('0'))
                   for k in ('delay_hours', 'deductible_delay', 'overtime_hours')})
    if show_pay:
        for k in ('absence_deduction', 'delay_deduction', 'overtime_pay', 'net_salary'):
            totals[k] = sum((getattr(r['payroll'], k) for r in rows if r['payroll']), Decimal('0'))
        totals['missing_payrolls'] = sum(1 for r in rows if not r['payroll'] and r['employee'].base_salary > 0)

    if request.GET.get('export') == 'excel':
        return _attendance_report_excel(rows, totals, month_name, show_pay)

    context = {
        'title': 'تقرير الحضور والغياب والرواتب',
        'year': year, 'month': month, 'month_name': month_name,
        'rows': rows, 'totals': totals, 'show_pay': show_pay,
        'grace_minutes': int(cfg['grace_hours'] * 60),
        'is_future': (year, month) > (today.year, today.month),
        **_period_choices(year),
    }
    return render(request, 'hr/attendance_report.html', context)


def _attendance_employee_month(request, emp, year, month, month_name, cfg, show_pay):
    """One employee's month, day by day: exactly what payroll counts for every date."""
    from .attendance_services import month_summary
    today = timezone.localdate()
    last = calendar.monthrange(year, month)[1]
    joined = timezone.localdate(emp.user.date_joined) if emp.user.date_joined else None
    records = {a.date: a for a in Attendance.objects.filter(employee=emp, date__year=year, date__month=month)
               .select_related('entered_by')}
    days = []
    for n in range(1, last + 1):
        d = date(year, month, n)
        att = records.get(d)
        if att and att.check_in:
            state, label = 'present', 'حاضر'
        elif att and att.status == 'leave':
            state, label = 'leave', 'إجازة'
        elif d > today:
            state, label = 'future', '—'
        elif joined and d < joined:
            state, label = 'before', 'قبل التعيين'
        elif d.weekday() in cfg['weekly_off']:
            state, label = 'off', 'إجازة أسبوعية'
        else:
            state, label = 'absent', 'غائب'
        days.append({'date': d, 'weekday': WEEKDAY_NAMES[d.weekday()], 'state': state, 'label': label, 'att': att,
                     'in': _clock(att.check_in) if att else '', 'out': _clock(att.check_out) if att else ''})
    summary = month_summary([emp], year, month, until=today, weekly_off=cfg['weekly_off'],
                            grace_hours=cfg['grace_hours'])[emp.id]
    payroll = Payroll.objects.filter(employee=emp, year=year, month=month).first() if show_pay else None
    context = {
        'title': 'تقرير الحضور والغياب والرواتب',
        'employee': emp, 'name': _employee_name(emp), 'days': days, 'summary': summary,
        'payroll': payroll, 'show_pay': show_pay,
        'year': year, 'month': month, 'month_name': month_name,
        'can_edit': is_admin_user(request.user) or request.user.has_perm('hr.change_attendance'),
        **_period_choices(year),
    }
    return render(request, 'hr/attendance_employee_month.html', context)


def _attendance_report_excel(rows, totals, month_name, show_pay):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from django.http import HttpResponse

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'الحضور والرواتب'
    ws.sheet_view.rightToLeft = True
    ws.append([f'تقرير الحضور والغياب والرواتب — {month_name}'])
    ws['A1'].font = Font(bold=True, size=14)
    head = ['الموظف', 'الفرع', 'أيام العمل', 'حضور', 'إجازة', 'غياب', 'ساعات التأخير', 'تأخير بعد السماحية',
            'ساعات الإضافي']
    if show_pay:
        head += ['خصم الغياب', 'خصم التأخير', 'الإضافي', 'صافي الراتب', 'حالة المسير']
    ws.append(head)
    for c in ws[2]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='1F2937')
        c.alignment = Alignment(horizontal='center')
    for r in rows:
        line = [r['name'], r['employee'].branch.name if r['employee'].branch else '',
                r['working_days'], r['present_days'], r['leave_days'], r['absent_days'],
                r['delay_hours'], r['deductible_delay'], r['overtime_hours']]
        if show_pay:
            p = r['payroll']
            line += ([p.absence_deduction, p.delay_deduction, p.overtime_pay, p.net_salary,
                      'معتمد' if p.is_paid else 'معلق'] if p else ['', '', '', '', 'لم يصدر'])
        ws.append(line)
    foot = ['الإجمالي', '', totals['working_days'], totals['present_days'], totals['leave_days'],
            totals['absent_days'], totals['delay_hours'], totals['deductible_delay'], totals['overtime_hours']]
    if show_pay:
        foot += [totals['absence_deduction'], totals['delay_deduction'], totals['overtime_pay'],
                 totals['net_salary'], '']
    ws.append(foot)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for col in ws.iter_cols(min_row=2, max_row=2):
        ws.column_dimensions[col[0].column_letter].width = 16
    ws.column_dimensions['A'].width = 26
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = "attachment; filename*=UTF-8''attendance_payroll.xlsx"
    wb.save(response)
    return response


# ---------------------------------------------------------------------------
# Employee loans (سلف الموظفين): paid from a treasury, deducted monthly in the payroll.
# A shift shortage charged to the cashier appears here automatically (source SHORTAGE).
# ---------------------------------------------------------------------------
@custom_permission_required('hr.view_employeeloan', redirect_url='core:main_screen')
def loan_list(request):
    from django.core.exceptions import ValidationError
    from django.core.paginator import Paginator
    from apps.tenant.accounting.forms import scoped_treasuries
    from .attendance_services import AR_MONTHS
    from .loan_services import create_cash_loan, with_balances
    from .models import EmployeeLoan, LoanRequest

    employees = list(_scope_by_branch(Employee.objects.filter(user__is_active=True).select_related('user', 'branch'),
                                      request.user, 'branch').order_by('user__first_name', 'user__username'))
    today = timezone.localdate()

    if request.method == 'POST':
        if not request.user.has_perm('hr.add_employeeloan'):
            messages.error(request, 'عفواً، لا تملك صلاحية صرف سلف.')
            return redirect('hr:loan_list')
        try:
            emp = next((e for e in employees if str(e.id) == request.POST.get('employee')), None)
            if emp is None:
                raise ValidationError('اختار الموظف.')
            treasury = scoped_treasuries(request.user).filter(id=request.POST.get('treasury') or 0).first()
            start_year, start_month = (int(x) for x in (request.POST.get('start') or f'{today.year}-{today.month}').split('-')[:2])
            loan = create_cash_loan(emp, Decimal(request.POST.get('amount') or '0'),
                                    int(request.POST.get('installments') or 1), start_year, start_month, treasury,
                                    reason=request.POST.get('reason', '').strip(), user=request.user)
            messages.success(request, f'تم صرف سلفة {loan.amount} لـ {_employee_name(emp)} — القسط {loan.monthly_installment} '
                                      f'شهرياً من {AR_MONTHS[loan.start_month - 1]} {loan.start_year}.')
        except (ValidationError, ValueError, ArithmeticError) as e:
            messages.error(request, ' '.join(e.messages) if hasattr(e, 'messages') else f'بيانات غير صالحة: {e}')
        return redirect('hr:loan_list')

    status = request.GET.get('status', 'ACTIVE')
    loans = with_balances(_scope_by_branch(
        EmployeeLoan.objects.select_related('employee__user', 'employee__branch', 'treasury', 'pos_session',
                                            'journal_entry'), request.user, 'employee__branch'))
    if status in (EmployeeLoan.ACTIVE, EmployeeLoan.SETTLED, EmployeeLoan.CANCELLED):
        loans = loans.filter(status=status)
    emp_filter = request.GET.get('employee') or ''
    if emp_filter.isdigit():
        loans = loans.filter(employee_id=int(emp_filter))
    from .models import PayrollLoanDeduction
    loan_ids = loans.values('id')
    total_amount = EmployeeLoan.objects.filter(id__in=loan_ids).aggregate(s=Coalesce(Sum('amount'), Decimal('0')))['s']
    total_deducted = PayrollLoanDeduction.objects.filter(loan_id__in=loan_ids).aggregate(
        s=Coalesce(Sum('amount'), Decimal('0')))['s']
    page = Paginator(loans.order_by('-date', '-id'), 25).get_page(request.GET.get('page'))
    months = [(f'{y}-{m}', f'{AR_MONTHS[m - 1]} {y}') for y, m in
              [((today.year * 12 + today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1) for k in range(0, 4)]]
    context = {
        'title': 'سلف الموظفين',
        'loans': page, 'page_obj': page,
        'status': status, 'employee_filter': emp_filter,
        'employees': employees,
        'treasuries': scoped_treasuries(request.user),
        'months': months,
        'total_amount': total_amount,
        'total_remaining': total_amount - total_deducted,
        'can_add': request.user.has_perm('hr.add_employeeloan'),
        'can_cancel': request.user.has_perm('hr.change_employeeloan'),
        'loan_requests': LoanRequest.objects.filter(status=LoanRequest.PENDING, employee__in=employees)
                         .select_related('employee__user'),
        'can_decide': request.user.has_perm('hr.change_loanrequest'),
    }
    return render(request, 'hr/loan_list.html', context)


@custom_permission_required('hr.change_employeeloan', redirect_url='core:main_screen')
@require_POST
def loan_cancel(request, pk):
    from django.core.exceptions import ValidationError
    from .loan_services import cancel_loan
    from .models import EmployeeLoan
    loan = get_object_or_404(_scope_by_branch(EmployeeLoan.objects.all(), request.user, 'employee__branch'), pk=pk)
    try:
        cancel_loan(loan, user=request.user)
        messages.success(request, 'تم إلغاء السلفة وعكس قيد صرفها.')
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
    return redirect('hr:loan_list')


# ---------------------------------------------------------------------------
# Leaves (الإجازات): balances per type, requests, approval writes the attendance.
# ---------------------------------------------------------------------------
@custom_permission_required('hr.view_leaverequest', redirect_url='core:main_screen')
def leave_list(request):
    from django.core.exceptions import ValidationError
    from django.core.paginator import Paginator
    from . import leave_services as svc
    from .models import LeaveRequest, LeaveType

    employees = list(_scope_by_branch(Employee.objects.filter(user__is_active=True).select_related('user', 'branch'),
                                      request.user, 'branch').order_by('user__first_name', 'user__username'))
    by_id = {e.id: e for e in employees}
    today = timezone.localdate()

    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'create':
                if not request.user.has_perm('hr.add_leaverequest'):
                    raise ValidationError('عفواً، لا تملك صلاحية تسجيل إجازات.')
                emp = by_id.get(int(request.POST.get('employee') or 0))
                ltype = LeaveType.objects.filter(id=request.POST.get('leave_type') or 0, is_active=True).first()
                if emp is None or ltype is None:
                    raise ValidationError('اختار الموظف ونوع الإجازة.')
                start = _parse_day(request.POST.get('start_date'), None)
                end = _parse_day(request.POST.get('end_date'), start)
                if start is None:
                    raise ValidationError('حدد تاريخ البداية.')
                req = svc.create_request(emp, ltype, start, end, request.POST.get('reason', ''), user=request.user)
                if request.POST.get('approve_now') and request.user.has_perm('hr.change_leaverequest'):
                    svc.approve(req, user=request.user)
                    messages.success(request, f'اتسجلت واتعمدت إجازة {req.days} يوم لـ {_employee_name(emp)}.')
                else:
                    messages.success(request, f'اتسجل طلب إجازة {req.days} يوم لـ {_employee_name(emp)} — مستني الاعتماد.')
            elif action in ('approve', 'reject', 'cancel'):
                if not request.user.has_perm('hr.change_leaverequest'):
                    raise ValidationError('عفواً، لا تملك صلاحية اعتماد الإجازات.')
                req = LeaveRequest.objects.filter(id=request.POST.get('request') or 0, employee__in=employees).first()
                if req is None:
                    raise ValidationError('الطلب مش موجود.')
                getattr(svc, action)(req, user=request.user)
                messages.success(request, {'approve': 'اتعمدت الإجازة واتسجلت في الحضور.', 'reject': 'اترفض الطلب.',
                                           'cancel': 'اتلغت الإجازة.'}[action])
            elif action == 'save_types':
                if not request.user.has_perm('hr.change_leavetype'):
                    raise ValidationError('عفواً، لا تملك صلاحية تعديل أنواع الإجازات.')
                for t in LeaveType.objects.all():
                    k = str(t.id)
                    t.days_per_year = Decimal(request.POST.get(f'days_{k}') or t.days_per_year)
                    t.is_paid = bool(request.POST.get(f'paid_{k}'))
                    t.uses_balance = bool(request.POST.get(f'balance_{k}'))
                    t.is_active = bool(request.POST.get(f'active_{k}'))
                    t.save()
                new_name = (request.POST.get('new_name') or '').strip()
                if new_name:
                    LeaveType.objects.get_or_create(name=new_name[:100], defaults={
                        'days_per_year': Decimal(request.POST.get('new_days') or '0'),
                        'is_paid': bool(request.POST.get('new_paid')), 'uses_balance': bool(request.POST.get('new_balance'))})
                messages.success(request, 'اتحفظت أنواع الإجازات.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (ValueError, ArithmeticError):
            messages.error(request, 'بيانات غير صالحة.')
        return redirect(request.get_full_path())

    year = today.year
    try:
        year = int(request.GET.get('year') or year)
    except ValueError:
        pass
    status = request.GET.get('status', 'PENDING')
    reqs = LeaveRequest.objects.filter(employee__in=employees).select_related(
        'employee__user', 'leave_type', 'decided_by').order_by('-start_date', '-id')
    if status in dict(LeaveRequest.STATUS_CHOICES):
        reqs = reqs.filter(status=status)
    emp_filter = request.GET.get('employee') or ''
    if emp_filter.isdigit():
        reqs = reqs.filter(employee_id=int(emp_filter))
    types = list(LeaveType.objects.all())
    bal = svc.balances(employees, year)
    balance_types = [t for t in types if t.is_active and t.uses_balance]
    balance_rows = [{'employee': e, 'cells': [bal[(e.id, t.id)] for t in balance_types]} for e in employees]
    page = Paginator(reqs, 25).get_page(request.GET.get('page'))
    context = {
        'title': 'الإجازات',
        'requests': page, 'page_obj': page, 'status': status, 'status_choices': LeaveRequest.STATUS_CHOICES,
        'employee_filter': emp_filter, 'employees': employees, 'types': types,
        'active_types': [t for t in types if t.is_active],
        'balance_types': balance_types, 'balance_rows': balance_rows, 'year': year,
        'pending_count': LeaveRequest.objects.filter(employee__in=employees, status=LeaveRequest.PENDING).count(),
        'can_add': request.user.has_perm('hr.add_leaverequest'),
        'can_decide': request.user.has_perm('hr.change_leaverequest'),
        'can_types': request.user.has_perm('hr.change_leavetype'),
        'today': today,
    }
    return render(request, 'hr/leave_list.html', context)


@custom_permission_required('hr.change_payrolltaxsettings', redirect_url='core:main_screen')
def payroll_tax_settings(request):
    """إعدادات ضريبة كسب العمل والتأمينات + حاسبة مثال للتأكد من الأرقام."""
    from . import payroll_tax
    from .models import PayrollTaxSettings
    s = PayrollTaxSettings.get()
    if request.method == 'POST':
        try:
            def dec(name, default):
                v = (request.POST.get(name) or '').strip()
                return Decimal(v) if v else default
            s.tax_enabled = bool(request.POST.get('tax_enabled'))
            s.insurance_enabled = bool(request.POST.get('insurance_enabled'))
            s.personal_exemption = dec('personal_exemption', s.personal_exemption)
            s.employee_insurance_rate = dec('employee_insurance_rate', s.employee_insurance_rate)
            s.employer_insurance_rate = dec('employer_insurance_rate', s.employer_insurance_rate)
            s.insurance_min_wage = dec('insurance_min_wage', s.insurance_min_wage)
            s.insurance_max_wage = dec('insurance_max_wage', s.insurance_max_wage)
            brackets = []
            for upper, rate in zip(request.POST.getlist('br_upper[]'), request.POST.getlist('br_rate[]')):
                if not (rate or '').strip():
                    continue
                brackets.append([float(Decimal(upper)) if (upper or '').strip() else None, float(Decimal(rate))])
            if not brackets or brackets[-1][0] is not None:
                brackets.append([None, brackets[-1][1] if brackets else 0])
            uppers = [b[0] for b in brackets[:-1]]
            if uppers != sorted(uppers) or any(u is None for u in uppers):
                raise ValueError('حدود الشرائح لازم تكون تصاعدية والأخيرة بس مفتوحة.')
            s.brackets = brackets
            if s.insurance_min_wage > s.insurance_max_wage:
                raise ValueError('الحد الأدنى للأجر التأميني أكبر من الأقصى.')
            s.save()
            messages.success(request, 'اتحفظت إعدادات الضرايب والتأمينات — هتتطبق على المسير اللي هيتحسب بعد كده.')
        except (ValueError, ArithmeticError) as e:
            messages.error(request, f'بيانات غير صالحة: {e}')
        return redirect('hr:payroll_tax_settings')

    sample = None
    try:
        gross = Decimal(request.GET.get('sample') or '0')
    except ArithmeticError:
        gross = Decimal('0')
    if gross > 0:
        class _Emp:   # an employee earning ``gross`` with default flags
            is_insured, tax_exempt, insurance_wage, base_salary = True, False, None, gross
        forced = PayrollTaxSettings(**{f.name: getattr(s, f.name) for f in PayrollTaxSettings._meta.concrete_fields})
        forced.tax_enabled = forced.insurance_enabled = True
        r = payroll_tax.compute(_Emp, gross, forced)
        sample = {'gross': gross, **r, 'net': gross - r['social_insurance'] - r['income_tax']}
    return render(request, 'hr/payroll_tax_settings.html', {
        'title': 'إعدادات ضرائب وتأمينات المرتبات', 's': s, 'sample': sample,
        'brackets': s.brackets, 'sample_value': request.GET.get('sample', ''),
    })
