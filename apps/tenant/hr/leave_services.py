"""
الإجازات: الرصيد، الطلب، الاعتماد / الرفض / الإلغاء.

Approving writes one attendance row per WORKING day of the leave (weekly off days are
skipped): status 'leave' for a paid type (payroll does not deduct), 'absent' for an unpaid
type (payroll deducts the day). Cancelling an approved leave removes those rows again.
A month whose payroll is paid / posted cannot change (attendance_services.payroll_locked).
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .attendance_services import payroll_locked
from .models import Attendance, LeaveAllocation, LeaveRequest, LeaveType

ZERO = Decimal('0')


def leave_days(start, end):
    """Working days (not weekly off) from start to end inclusive."""
    from .views import _weekly_off
    off = _weekly_off()
    days, d = [], start
    while d <= end:
        if d.weekday() not in off:
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def balances(employees, year):
    """{(employee_id, leave_type_id): {entitled, used, pending, remaining}} for the types that
    use a balance — 2 grouped queries for all employees."""
    employees = list(employees)
    types = list(LeaveType.objects.filter(is_active=True, uses_balance=True))
    special = {(a.employee_id, a.leave_type_id): a.days for a in
               LeaveAllocation.objects.filter(employee__in=employees, year=year)}
    days_field = DecimalField(max_digits=7, decimal_places=1)
    taken = {(r['employee_id'], r['leave_type_id']): r for r in LeaveRequest.objects.filter(
        employee__in=employees, start_date__year=year, status__in=[LeaveRequest.APPROVED, LeaveRequest.PENDING],
    ).order_by().values('employee_id', 'leave_type_id').annotate(
        used=Coalesce(Sum('days', filter=Q(status=LeaveRequest.APPROVED)), Value(ZERO), output_field=days_field),
        pending=Coalesce(Sum('days', filter=Q(status=LeaveRequest.PENDING)), Value(ZERO), output_field=days_field),
    )}
    out = {}
    for emp in employees:
        for t in types:
            entitled = special.get((emp.id, t.id), t.days_per_year)
            row = taken.get((emp.id, t.id), {})
            used, pending = row.get('used') or ZERO, row.get('pending') or ZERO
            out[(emp.id, t.id)] = {'type': t, 'entitled': entitled, 'used': used, 'pending': pending,
                                   'remaining': entitled - used - pending}
    return out


def create_request(employee, leave_type, start, end, reason='', user=None):
    if end < start:
        raise ValidationError('تاريخ النهاية قبل البداية.')
    days = leave_days(start, end)
    if not days:
        raise ValidationError('الفترة دي كلها إجازة أسبوعية — مفيش أيام عمل.')
    clash = LeaveRequest.objects.filter(employee=employee, status__in=[LeaveRequest.PENDING, LeaveRequest.APPROVED],
                                        start_date__lte=end, end_date__gte=start).first()
    if clash:
        raise ValidationError(f'فيه طلب إجازة تاني في نفس الفترة ({clash.start_date} → {clash.end_date}).')
    count = Decimal(len(days))
    if leave_type.uses_balance:
        bal = balances([employee], start.year)[(employee.id, leave_type.id)]
        if count > bal['remaining']:
            raise ValidationError(f'رصيد {leave_type} المتبقي {bal["remaining"]} يوم بس، والطلب {count} يوم.')
    return LeaveRequest.objects.create(employee=employee, leave_type=leave_type, start_date=start, end_date=end,
                                       days=count, reason=(reason or '')[:255], created_by=user)


def approve(request_obj, user=None):
    with transaction.atomic():
        req = LeaveRequest.objects.select_for_update(of=('self',)).select_related('leave_type', 'employee').get(
            pk=request_obj.pk)
        if req.status != LeaveRequest.PENDING:
            raise ValidationError('الطلب مش مستني اعتماد.')
        days = leave_days(req.start_date, req.end_date)
        for d in {(x.year, x.month): x for x in days}.values():
            if payroll_locked(req.employee, d):
                raise ValidationError(f'مسير شهر {d.month}/{d.year} اتصرف / اتعمد — مينفعش تسجيل إجازة فيه.')
        worked = list(Attendance.objects.filter(employee=req.employee, date__in=days, check_in__isnull=False)
                      .values_list('date', flat=True))
        if worked:
            raise ValidationError('الموظف مسجل حضور في: ' + '، '.join(str(d) for d in sorted(worked)) +
                                  ' — عدّل الفترة أو الحضور الأول.')
        status = 'leave' if req.leave_type.is_paid else 'absent'
        note = f'{req.leave_type.name} — طلب #{req.pk}'
        existing = {a.date: a for a in Attendance.objects.filter(employee=req.employee, date__in=days)}
        new_rows = []
        for d in days:
            a = existing.get(d)
            if a is None:
                new_rows.append(Attendance(employee=req.employee, date=d, status=status, source=Attendance.SOURCE_MANUAL,
                                           entered_by=user, notes=note, leave_request=req))
            else:
                a.status, a.check_out, a.notes, a.leave_request, a.entered_by = status, None, note, req, user
                a.source = Attendance.SOURCE_MANUAL
                a.delay_hours = a.overtime_hours = ZERO
                a.save()
        Attendance.objects.bulk_create(new_rows)
        req.days = Decimal(len(days))
        req.status, req.decided_by, req.decided_at = LeaveRequest.APPROVED, user, timezone.now()
        req.save(update_fields=['days', 'status', 'decided_by', 'decided_at'])
    return req


def reject(request_obj, user=None):
    with transaction.atomic():
        req = LeaveRequest.objects.select_for_update(of=('self',)).get(pk=request_obj.pk)
        if req.status != LeaveRequest.PENDING:
            raise ValidationError('الطلب مش مستني اعتماد.')
        req.status, req.decided_by, req.decided_at = LeaveRequest.REJECTED, user, timezone.now()
        req.save(update_fields=['status', 'decided_by', 'decided_at'])
    return req


def cancel(request_obj, user=None):
    """Cancel a pending or approved leave (an approved one gives its days back to attendance)."""
    with transaction.atomic():
        req = LeaveRequest.objects.select_for_update(of=('self',)).select_related('employee').get(pk=request_obj.pk)
        if req.status not in (LeaveRequest.PENDING, LeaveRequest.APPROVED):
            raise ValidationError('الطلب مش قابل للإلغاء.')
        if req.status == LeaveRequest.APPROVED:
            rows = list(req.attendance_days.all())
            for d in {(a.date.year, a.date.month): a.date for a in rows}.values():
                if payroll_locked(req.employee, d):
                    raise ValidationError(f'مسير شهر {d.month}/{d.year} اتصرف / اتعمد — مينفعش إلغاء الإجازة.')
            req.attendance_days.filter(check_in__isnull=True).delete()
        req.status, req.decided_by, req.decided_at = LeaveRequest.CANCELLED, user, timezone.now()
        req.save(update_fields=['status', 'decided_by', 'decided_at'])
    return req
