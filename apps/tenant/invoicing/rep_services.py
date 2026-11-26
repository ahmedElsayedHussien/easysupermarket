"""
مناديب المبيعات: أداء الشهر (صافي المبيعات، التارجت، العمولة) واعتماد العمولة.
Net sales = posted sales - posted sales returns tagged with the rep, summed by the database
for all reps at once. An approved commission is added to the rep's payroll of that month.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Case, Count, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from .models import Invoice, RepCommission, SalesRep

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=18, decimal_places=2)


def rep_for_user(user):
    emp = getattr(user, 'employee_profile', None) if getattr(user, 'is_authenticated', False) else None
    if emp is None:
        return None
    return SalesRep.objects.filter(employee=emp, is_active=True).first()


def month_performance(reps, year, month):
    reps = list(reps)
    rows = {r['sales_rep']: r for r in Invoice.objects.filter(
        sales_rep__in=reps, status=Invoice.POSTED, date__year=year, date__month=month,
        invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE]).order_by().values('sales_rep').annotate(
        sales=Coalesce(Sum('total_amount', filter=Q(invoice_type=Invoice.SALE)), Value(ZERO), output_field=MONEY),
        returns=Coalesce(Sum('total_amount', filter=Q(invoice_type=Invoice.RETURN_SALE)), Value(ZERO),
                         output_field=MONEY),
        count=Count('id', filter=Q(invoice_type=Invoice.SALE)),
        customers=Count('partner', filter=Q(invoice_type=Invoice.SALE), distinct=True),
    )}
    approved = {c.rep_id: c for c in RepCommission.objects.filter(rep__in=reps, year=year, month=month)}
    out = {}
    for rep in reps:
        r = rows.get(rep.id, {})
        net = (r.get('sales') or ZERO) - (r.get('returns') or ZERO)
        target = rep.monthly_target or ZERO
        out[rep.id] = {
            'rep': rep, 'sales': r.get('sales') or ZERO, 'returns': r.get('returns') or ZERO, 'net': net,
            'invoices': r.get('count') or 0, 'customers': r.get('customers') or 0, 'target': target,
            'achievement': round(net * 100 / target) if target > 0 else None,
            'commission': (max(net, ZERO) * (rep.commission_rate or ZERO) / 100).quantize(Decimal('0.01')),
            'approved': approved.get(rep.id),
        }
    return out


def approve_commission(rep, year, month, user=None):
    from apps.tenant.hr.attendance_services import payroll_locked
    import datetime
    if payroll_locked(rep.employee, datetime.date(year, month, 1)):
        raise ValidationError('مسير الشهر ده اتعمد — مينفعش تغيير العمولة.')
    perf = month_performance([rep], year, month)[rep.id]
    obj, _ = RepCommission.objects.update_or_create(rep=rep, year=year, month=month, defaults={
        'net_sales': perf['net'], 'rate': rep.commission_rate or ZERO, 'amount': perf['commission'],
        'approved_by': user})
    return obj


def commissions_for_payroll(employees, year, month):
    """{employee_id: approved commission amount} for the payroll of a month (one query)."""
    return dict(RepCommission.objects.filter(rep__employee__in=employees, year=year, month=month)
                .values_list('rep__employee_id', 'amount'))
