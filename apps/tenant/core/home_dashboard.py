"""
Home page (main screen) building blocks.

Everything respects the branch rule (core.access): admins see the header branch
("all branches" = everything), other users their own branch. Money figures are for
admins only; alerts, quick actions and modules follow the user's permissions, so a
link is only shown when its screen would open for that user.
"""
import datetime
from decimal import Decimal

from django.db.models import Case, Count, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.urls import reverse

from .access import filter_by_branch
from .decorators import is_admin_user

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=20, decimal_places=4)
CHART_DAYS = 7
TOP_ITEMS = 5
AR_DAYS = ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد']


def can(user, *perms):
    """Admin, or has any of ``perms`` (no perms = everyone logged in)."""
    return is_admin_user(user) or not perms or any(user.has_perm(p) for p in perms)


def greeting(now):
    return 'صباح الخير' if now.hour < 12 else 'مساء الخير'


# ---------------------------------------------------------------- numbers of the day
def _sales_scope(request):
    from apps.tenant.invoicing.models import Invoice
    return filter_by_branch(Invoice.objects.filter(status=Invoice.POSTED,
                                                   invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE]), request)


def day_figures(request, today):
    """Net sales today/yesterday, invoice count, gross profit, cash in open drawers."""
    from apps.tenant.invoicing.models import Invoice, InvoiceLine, POSSession
    from apps.tenant.invoicing.shift_services import shift_cash_summary

    S, R = Invoice.SALE, Invoice.RETURN_SALE
    yesterday = today - datetime.timedelta(days=1)
    rows = (_sales_scope(request).filter(date__in=[today, yesterday]).values('date')
            .annotate(net=Coalesce(Sum(Case(When(invoice_type=S, then=F('total_amount')),
                                             When(invoice_type=R, then=-F('total_amount')),
                                             default=ZERO, output_field=MONEY)), Value(ZERO), output_field=MONEY),
                      n=Count('id', filter=Q(invoice_type=S))))
    by_day = {r['date']: r for r in rows}
    t, y = by_day.get(today, {}), by_day.get(yesterday, {})
    sales, sales_y = t.get('net') or ZERO, y.get('net') or ZERO
    change = None
    if sales_y > 0:
        change = round(float((sales - sales_y) / sales_y * 100), 1)

    lines = filter_by_branch(InvoiceLine.objects.filter(
        invoice__status=Invoice.POSTED, invoice__date=today, invoice__invoice_type__in=[S, R]),
        request, field='invoice__branch')
    agg = lines.aggregate(
        rev=Coalesce(Sum(Case(When(invoice__invoice_type=S, then=F('subtotal')),
                              When(invoice__invoice_type=R, then=-F('subtotal')),
                              default=ZERO, output_field=MONEY)), Value(ZERO), output_field=MONEY),
        cogs=Coalesce(Sum(Case(When(invoice__invoice_type=S, then=F('cogs_amount')),
                               When(invoice__invoice_type=R, then=-F('cogs_amount')),
                               default=ZERO, output_field=MONEY)), Value(ZERO), output_field=MONEY),
    )
    profit = (agg['rev'] or ZERO) - (agg['cogs'] or ZERO)
    margin = round(float(profit / agg['rev'] * 100), 1) if agg['rev'] and agg['rev'] > 0 else None

    sessions = list(filter_by_branch(POSSession.objects.filter(status=POSSession.OPEN)
                                     .select_related('treasury'), request))
    drawer_cash = ZERO
    for s in sessions:
        try:
            drawer_cash += shift_cash_summary(s)['expected_balance'] or ZERO
        except Exception:
            pass

    return {
        'sales': sales,
        'sales_yesterday': sales_y,
        'change': change,
        'change_abs': abs(change) if change is not None else None,
        'invoices': t.get('n') or 0,
        'profit': profit,
        'margin': margin,
        'drawer_cash': drawer_cash,
        'open_shifts': len(sessions),
    }


def week_chart(request, today):
    """Net sales of the last CHART_DAYS days (oldest first) with bar heights in %."""
    from apps.tenant.invoicing.models import Invoice
    start = today - datetime.timedelta(days=CHART_DAYS - 1)
    rows = dict(_sales_scope(request).filter(date__gte=start, date__lte=today).values('date')
                .annotate(net=Sum(Case(When(invoice_type=Invoice.SALE, then=F('total_amount')),
                                       When(invoice_type=Invoice.RETURN_SALE, then=-F('total_amount')),
                                       default=ZERO, output_field=MONEY)))
                .values_list('date', 'net'))
    days = []
    for i in range(CHART_DAYS):
        d = start + datetime.timedelta(days=i)
        days.append({'date': d, 'name': 'اليوم' if d == today else AR_DAYS[d.weekday()],
                     'value': rows.get(d) or ZERO, 'is_today': d == today})
    peak = max([d['value'] for d in days] + [ZERO])
    for d in days:
        d['pct'] = float(d['value'] / peak * 100) if peak > 0 and d['value'] > 0 else 0
    total = sum((d['value'] for d in days), ZERO)
    return {'days': days, 'total': total, 'has_data': peak > 0}


def top_items(request, today):
    from apps.tenant.invoicing.models import Invoice, InvoiceLine
    qs = filter_by_branch(InvoiceLine.objects.filter(
        invoice__status=Invoice.POSTED, invoice__date=today, invoice__invoice_type=Invoice.SALE),
        request, field='invoice__branch')
    rows = list(qs.values('product_id', 'product__name')
                .annotate(qty=Sum('quantity'), amount=Sum('subtotal'))
                .order_by('-amount')[:TOP_ITEMS])
    peak = max([r['amount'] or ZERO for r in rows] + [ZERO])
    for r in rows:
        r['pct'] = float((r['amount'] or ZERO) / peak * 100) if peak > 0 else 0
    return rows


# ---------------------------------------------------------------- needs attention
def alerts(request, today, low_stock_count):
    """Things to act on today. Each item: icon, text, count, link, level."""
    user = request.user
    items = []

    def add(icon, text, count, url, level='warning', action='اعرض'):
        if count:
            items.append({'icon': icon, 'text': text, 'count': count, 'url': url, 'level': level,
                          'action': action})

    from apps.tenant.inventory.dashboard_views import _can_view as can_inventory
    if can_inventory(user):
        add('📦', 'صنف وصل لحد إعادة الطلب أو خلص', low_stock_count, reverse('core:report_low_stock'))

    if can(user, 'accounting.change_internaltransfer'):
        try:
            from apps.tenant.accounting.views import incoming_transfers
            add('🔁', 'تحويل نقدية في الطريق مستني تستلمه', incoming_transfers(user).count(),
                reverse('accounting:transfer_list') + '?incoming=1', 'critical', 'استلم')
        except Exception:
            pass

    try:
        from apps.tenant.invoicing.models import LoadingOrder, UnloadingOrder
        if can(user, 'invoicing.approve_loadingorder'):
            add('⬆️', 'طلب تحميل مندوب مستني الاعتماد',
                filter_by_branch(LoadingOrder.objects.filter(status=LoadingOrder.PENDING), request,
                                 'rep__employee__branch').count(),
                reverse('invoicing:loading_list') + '?status=PENDING', 'warning', 'اعتمد')
        if can(user, 'invoicing.issue_loadingorder'):
            add('📦', 'طلب تحميل معتمد مستني الصرف من المخزن',
                filter_by_branch(LoadingOrder.objects.filter(status=LoadingOrder.APPROVED), request,
                                 'rep__employee__branch').count(),
                reverse('invoicing:loading_list') + '?status=APPROVED', 'warning', 'اصرف')
        if can(user, 'invoicing.receive_unloadingorder'):
            add('⬇️', 'طلب تفريغ مندوب مستني الاستلام',
                filter_by_branch(UnloadingOrder.objects.filter(status=UnloadingOrder.PENDING), request,
                                 'rep__employee__branch').count(),
                reverse('invoicing:unloading_list'), 'warning', 'استلم')
    except Exception:
        pass

    if is_admin_user(user):
        from apps.tenant.invoicing.models import POSSession
        midnight = datetime.datetime.combine(today, datetime.time.min)
        from django.utils import timezone
        if timezone.is_naive(midnight) and timezone.is_aware(timezone.now()):
            midnight = timezone.make_aware(midnight)
        stale = filter_by_branch(POSSession.objects.filter(status=POSSession.OPEN, start_time__lt=midnight),
                                 request).count()
        add('🕒', 'وردية مفتوحة من يوم سابق ولسه متقفلتش', stale, reverse('core:report_shifts'), 'critical', 'راجع')

    if can(user, 'einvoicing.view_einvoicelog'):
        try:
            from apps.tenant.einvoicing.models import EInvoiceLog
            logs = filter_by_branch(EInvoiceLog.objects.all(), request, field='invoice__branch')
            add('🧾', 'فاتورة إلكترونية مستنية تأكيدك قبل الإرسال',
                logs.filter(status='WAITING_APPROVAL').count(), reverse('einvoicing:invoice_approval_list'),
                'warning', 'أكّد')
            add('⛔', 'فاتورة إلكترونية مرفوضة أو فيها خطأ أو محتاجة إجراء',
                logs.filter(status__in=['INVALID', 'ERROR', 'REJECTED', 'CANCEL_PENDING', 'CANCEL_DECLINED',
                                        'REJECT_REQUESTED']).count(),
                reverse('einvoicing:invoice_history'), 'critical', 'راجع')
        except Exception:
            pass

    from apps.tenant.accounting.accounting_dashboard_views import _can_view as can_accounting
    if can_accounting(user):
        try:
            from apps.tenant.accounting.models import Cheque, ChequeStatus, ChequeType
            due = Cheque.objects.filter(
                Q(cheque_type=ChequeType.RECEIVABLE,
                  status__in=[ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO])
                | Q(cheque_type=ChequeType.PAYABLE, status=ChequeStatus.ISSUED),
                due_date__lte=today).count()
            add('📝', 'شيك استحق ولسه متسجلش تحصيله أو صرفه', due, reverse('accounting:accounting_dashboard'))
        except Exception:
            pass

    try:
        from apps.tenant.hr.models import EmployeeDocument, LeaveRequest, LoanRequest
        from apps.tenant.hr.views import _scope_by_branch
        from apps.tenant.core.models import Employee
        staff = _scope_by_branch(Employee.objects.filter(user__is_active=True), user, 'branch')
        if can(user, 'hr.view_employeeprofile'):
            add('📄', 'مستند موظف انتهى أو هينتهي خلال 30 يوم', EmployeeDocument.objects.filter(
                employee__in=staff, expiry_date__lte=today + datetime.timedelta(days=30)).count(),
                reverse('hr:employee_list'))
        if can(user, 'hr.change_leaverequest'):
            add('🌴', 'طلب إجازة مستني اعتمادك', LeaveRequest.objects.filter(
                employee__in=staff, status=LeaveRequest.PENDING).count(), reverse('hr:leave_list'), 'warning', 'اعتمد')
        if can(user, 'hr.change_loanrequest'):
            add('💵', 'طلب سلفة من موظف مستني قرارك', LoanRequest.objects.filter(
                employee__in=staff, status=LoanRequest.PENDING).count(), reverse('hr:loan_list'), 'warning', 'راجع')
    except Exception:
        pass

    order = {'critical': 0, 'warning': 1}
    items.sort(key=lambda i: order.get(i['level'], 2))
    return items


# ---------------------------------------------------------------- actions & modules
def quick_actions(user):
    acts = [
        ('🛒', 'نقطة البيع', 'invoicing:pos', ('invoicing.view_invoice',), 'F5'),
        ('📥', 'فاتورة شراء', 'invoicing:purchase_invoice', ('invoicing.add_purchase_invoice',), ''),
        ('💵', 'سند قبض', 'accounting:receipt_create', ('accounting.add_voucher',), ''),
        ('📤', 'سند صرف', 'accounting:payment_create', ('accounting.add_voucher',), ''),
        ('🔁', 'تحويل مخزني', 'inventory:transfer_stock', ('inventory.change_inventorybatch',), ''),
        ('➕', 'صنف جديد', 'inventory:product_create', ('inventory.add_product',), ''),
        ('👤', 'عميل / مورد جديد', 'partners:partner_create', ('partners.add_partner',), ''),
        ('📋', 'عرض سعر', 'invoicing:quotation_create', ('invoicing.add_quotation',), ''),
    ]
    return [{'icon': i, 'label': l, 'url': reverse(u), 'key': k} for i, l, u, p, k in acts if can(user, *p)]


def modules(user):
    from apps.tenant.accounting.accounting_dashboard_views import _can_view as can_accounting
    from apps.tenant.accounting.liquidity_views import _can_view as can_liquidity
    from apps.tenant.inventory.dashboard_views import _can_view as can_inventory
    from apps.tenant.partners.dashboard_views import _can_view as can_partners
    admin = is_admin_user(user)
    view_inv = can(user, 'invoicing.view_invoice')
    mods = [
        ('orange', '🛒', 'نقطة البيع', 'POS', 'invoicing:pos', view_inv),
        ('green', '🧾', 'المبيعات', 'Sales', 'invoicing:sales_invoice_list', view_inv),
        ('red', '📥', 'المشتريات', 'Purchases', 'invoicing:purchase_invoice_list', view_inv),
        ('cyan', '📦', 'المخزون', 'Inventory', 'inventory:dashboard', can_inventory(user)),
        ('blue', '🤝', 'العملاء والموردين', 'Partners', 'partners:dashboard', can_partners(user)),
        ('gold', '📒', 'الحسابات', 'Accounting', 'accounting:accounting_dashboard', can_accounting(user)),
        ('gold', '💧', 'السيولة والخزائن', 'Liquidity', 'accounting:liquidity_dashboard', can_liquidity(user)),
        ('purple', '🧾', 'الفاتورة الإلكترونية', 'E-Invoice', 'einvoicing:invoice_history',
         can(user, 'einvoicing.view_einvoicelog')),
        ('orange', '💳', 'خدمات الشحن والدفع', 'E-Services', 'accounting:eservice_dashboard', admin),
        ('cyan', '📊', 'التقارير', 'Reports', 'core:reports_index', admin or view_inv),
        ('lime', '🏢', 'الفروع', 'Branches', 'core:branch_list', admin),
        ('gray', '⚙️', 'الإعدادات', 'Settings', 'core:settings_dashboard', admin),
    ]
    return [{'color': c, 'icon': i, 'title': t, 'subtitle': s, 'url': reverse(u)}
            for c, i, t, s, u, ok in mods if ok]
