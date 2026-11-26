"""
Attendance rules shared by the employee's location check-in (GPS) and the manager's
manual attendance sheet, so both give payroll exactly the same figures.

Payroll (hr.views.payroll_process) reads, per day:
  - present  -> a record with check_in (delay_hours / overtime_hours are deducted / paid)
  - leave    -> status 'leave' (excused, no deduction)
  - anything else on a working day -> absent (one day's pay deducted)
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from .models import Attendance, Payroll

HOURS = Decimal('0.01')
PRESENT, ABSENT, LEAVE = 'present', 'absent', 'leave'


def _hours(seconds):
    return (Decimal(int(seconds)) / Decimal('3600')).quantize(HOURS, rounding=ROUND_HALF_UP)


def shift_bounds(employee, day):
    """(start, end) aware datetimes of the employee's shift on ``day`` (None if not set).
    A shift ending at / before its start time ends the next day (overnight)."""
    tz = timezone.get_current_timezone()
    start = end = None
    if employee.shift_start_time:
        start = timezone.make_aware(datetime.datetime.combine(day, employee.shift_start_time), tz)
    if employee.shift_end_time:
        end = timezone.make_aware(datetime.datetime.combine(day, employee.shift_end_time), tz)
        if employee.shift_start_time and employee.shift_end_time <= employee.shift_start_time:
            end += datetime.timedelta(days=1)
    return start, end


def compute_hours(attendance):
    """Set delay_hours (check-in after shift start) and overtime_hours (check-out after
    shift end) from the record's times and the employee's shift."""
    start, end = shift_bounds(attendance.employee, attendance.date)
    attendance.delay_hours = Decimal('0.00')
    attendance.overtime_hours = Decimal('0.00')
    if attendance.check_in and start and attendance.check_in > start:
        attendance.delay_hours = _hours((attendance.check_in - start).total_seconds())
    if attendance.check_out and end and attendance.check_out > end:
        attendance.overtime_hours = _hours((attendance.check_out - end).total_seconds())
    return attendance


def payroll_locked(employee, day):
    """True when the month's payroll of the employee is paid or posted: its attendance
    can no longer change (the salary would not match it any more)."""
    from django.db.models import Q
    return Payroll.objects.filter(employee=employee, month=day.month, year=day.year).filter(
        Q(is_paid=True) | Q(journal_entry__isnull=False)).exists()


def locked_months(employees, days):
    """{(employee_id, year, month)} of paid / posted payrolls touching ``days`` (one query)."""
    from django.db.models import Q
    months = {(d.year, d.month) for d in days}
    if not months:
        return set()
    q = Q()
    for y, m in months:
        q |= Q(year=y, month=m)
    return set(Payroll.objects.filter(q, employee__in=employees)
               .filter(Q(is_paid=True) | Q(journal_entry__isnull=False))
               .values_list('employee_id', 'year', 'month'))


class AttendanceError(ValueError):
    pass


def _at(day, clock):
    return timezone.make_aware(datetime.datetime.combine(day, clock), timezone.get_current_timezone())


def save_manual_day(employee, day, status, check_in=None, check_out=None, notes='', user=None):
    """Create / update the employee's attendance of ``day`` from the manual sheet.

    check_in / check_out are wall-clock ``datetime.time`` values; a check-out earlier than
    the check-in is taken as the next morning (overnight shift)."""
    if day > timezone.localdate():
        raise AttendanceError('لا يمكن تسجيل حضور ليوم لم يأت بعد.')
    if status not in (PRESENT, ABSENT, LEAVE):
        raise AttendanceError('حالة غير معروفة.')
    if status == PRESENT and not check_in:
        raise AttendanceError('اكتب وقت الحضور للموظف الحاضر.')
    if check_out and not check_in:
        raise AttendanceError('لا يمكن تسجيل انصراف بدون حضور.')

    with transaction.atomic():
        if payroll_locked(employee, day):
            raise AttendanceError('مسير رواتب هذا الشهر اتصرف / اتعمد — مينفعش تعديل الحضور.')
        att, _ = Attendance.objects.select_for_update(of=('self',)).get_or_create(employee=employee, date=day)
        att.status = status
        att.notes = (notes or '').strip()[:255]
        att.source = Attendance.SOURCE_MANUAL
        att.entered_by = user
        if status == PRESENT:
            att.check_in = _at(day, check_in)
            att.check_out = None
            if check_out:
                out = _at(day, check_out)
                if out <= att.check_in:
                    out += datetime.timedelta(days=1)
                att.check_out = out
            compute_hours(att)
        else:
            att.check_in = att.check_out = None
            att.delay_hours = att.overtime_hours = Decimal('0.00')
        att.save()
    return att


def fill_period_present(employees, date_from, date_to, user=None):
    """Mark every employee present at his shift times on each working day of the period
    that has NO attendance record yet (weekly off days and existing records are kept).
    For running without location check-in; exceptions are then edited day by day.
    Returns (records created, employees skipped for lack of shift times)."""
    from .views import _weekly_off
    today = timezone.localdate()
    date_to = min(date_to, today)
    if date_from > date_to:
        return 0, []
    weekly_off = _weekly_off()
    days = []
    d = date_from
    while d <= date_to:
        if d.weekday() not in weekly_off:
            days.append(d)
        d += datetime.timedelta(days=1)
    employees = list(employees)
    existing = set(Attendance.objects.filter(employee__in=employees, date__in=days)
                   .values_list('employee_id', 'date'))
    locked = locked_months(employees, days)
    created, no_shift = 0, []
    new_rows = []
    for emp in employees:
        if not (emp.shift_start_time and emp.shift_end_time):
            no_shift.append(emp)
            continue
        for day in days:
            if (emp.id, day) in existing or (emp.id, day.year, day.month) in locked:
                continue
            start, end = shift_bounds(emp, day)
            att = Attendance(employee=emp, date=day, status=PRESENT, check_in=start, check_out=end,
                             source=Attendance.SOURCE_MANUAL, entered_by=user, notes='تسجيل فترة بمواعيد الوردية')
            new_rows.append(compute_hours(att))
    with transaction.atomic():
        Attendance.objects.bulk_create(new_rows, ignore_conflicts=True)
    created = len(new_rows)
    return created, no_shift


AR_MONTHS = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر',
             'نوفمبر', 'ديسمبر']


def month_summary(employees, year, month, until=None, weekly_off=None, grace_hours=Decimal('0')):
    """Attendance of a month for many employees in 2 queries — THE figures used by both the
    payroll and the attendance report:

      working_days  days of the month that are not weekly off (only up to ``until`` and from
                    the employee's joining date)
      present_days  days with a check-in            leave_days  days with status 'leave'
      absent_days   working days with neither        delay_hours / overtime_hours  totals
      deductible_delay  sum over days of max(0, delay - grace) (summed by the database)
    """
    from django.db.models import Case, DecimalField, F, Sum, Value, When
    from django.db.models.functions import Coalesce
    from .views import _weekly_off, _working_days

    employees = list(employees)
    if weekly_off is None:
        weekly_off = _weekly_off()
    hours = DecimalField(max_digits=8, decimal_places=2)
    grace = Value(grace_hours, output_field=hours)
    rows = Attendance.objects.filter(employee__in=employees, date__year=year, date__month=month).order_by()
    totals = {r['employee_id']: r for r in rows.values('employee_id').annotate(
        delay=Coalesce(Sum('delay_hours'), Value(Decimal('0')), output_field=hours),
        overtime=Coalesce(Sum('overtime_hours'), Value(Decimal('0')), output_field=hours),
        deductible=Coalesce(Sum(Case(When(delay_hours__gt=grace, then=F('delay_hours') - grace),
                                     default=Value(Decimal('0')), output_field=hours)),
                            Value(Decimal('0')), output_field=hours),
    )}
    present, leave = {}, {}
    for emp_id, day, status, check_in in rows.values_list('employee_id', 'date', 'status', 'check_in'):
        if check_in is not None:
            present.setdefault(emp_id, set()).add(day)
        elif status == LEAVE:
            leave.setdefault(emp_id, set()).add(day)

    out = {}
    for emp in employees:
        joined = timezone.localdate(emp.user.date_joined) if emp.user.date_joined else None
        working = _working_days(year, month, until=until, since=joined, weekly_off=weekly_off)
        p, l = present.get(emp.id, set()), leave.get(emp.id, set())
        t = totals.get(emp.id, {})
        absent = sum(1 for d in working if d not in p and d not in l)
        out[emp.id] = {
            # share of the working days that were attended or excused (leave)
            'attendance_rate': round((len(working) - absent) * 100 / len(working)) if working else None,
            'working_days': len(working),
            'present_days': len(p),
            'leave_days': len(l),
            'absent_days': absent,
            'delay_hours': (t.get('delay') or Decimal('0')).quantize(HOURS),
            'overtime_hours': (t.get('overtime') or Decimal('0')).quantize(HOURS),
            'deductible_delay': (t.get('deductible') or Decimal('0')).quantize(HOURS),
        }
    return out
