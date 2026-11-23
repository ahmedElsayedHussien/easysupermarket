"""
Partners dashboard (لوحة الشركاء).

Replaces the side-menu entries (suppliers, customers, customer sectors,
partner ledger/report) with one page: receivables & payables, top debtors and
creditors, credit-limit usage, dormant debtors, receivables by sector, 6-month
credit sales vs collections (and credit purchases vs supplier payments),
recent receipts/payments, and every partner action.

Partners are company-wide (not branch-bound), so this page is not branch-scoped.
Balances use the same rule as statements (partners.services.batch_get_partners_balances):
final_balance > 0 -> the partner owes us; < 0 -> we owe the partner.
"""
import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Case, DecimalField, F, Max, Sum, Value, When
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import redirect, render

from apps.tenant.core.settings_helpers import setting

from .models import Partner

ZERO = Decimal('0')
MONTHS = 6
TOP_N = 8
MONEY = DecimalField(max_digits=20, decimal_places=4)
PARTNER_VIEW_PERMS = ('partners.view_partner',)
ARABIC_MONTHS = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']


def _can_view(user):
    from apps.tenant.core.decorators import is_admin_user
    return is_admin_user(user) or any(user.has_perm(p) for p in PARTNER_VIEW_PERMS)


def _credit_portion():
    """Credit (on-account) part of an invoice — same rule as the partner balance service."""
    immediate = F('paid_cash') + F('paid_card') + F('paid_ewallet') + F('paid_bank')
    return Case(
        When(payment_type='CREDIT', then=F('total_amount')),
        When(payment_type='SPLIT', then=F('total_amount') - immediate),
        default=Value(ZERO), output_field=MONEY,
    )


def _month_starts(today, n):
    first = today.replace(day=1)
    months = []
    for _ in range(n):
        months.append(first)
        first = (first - datetime.timedelta(days=1)).replace(day=1)
    return list(reversed(months))


def _monthly(qs, date_field, value_expr, months):
    rows = (qs.filter(**{f'{date_field}__gte': months[0]})
            .annotate(m=TruncMonth(date_field)).values('m').annotate(v=Sum(value_expr)))
    by_month = {}
    for r in rows:
        m = r['m'].date() if hasattr(r['m'], 'date') else r['m']
        by_month[m] = float(r['v'] or 0)
    return [round(by_month.get(m, 0.0), 2) for m in months]


def _hbars(items):
    peak = max([i['value'] for i in items] + [ZERO])
    for i in items:
        i['pct'] = float(i['value'] / peak * 100) if peak > 0 else 0
    return items


@login_required
def partners_dashboard(request):
    if not _can_view(request.user):
        from django.contrib import messages
        messages.error(request, 'عفواً، لا تملك صلاحية عرض لوحة الشركاء.')
        return redirect('core:main_screen')

    from apps.tenant.accounting.models import Voucher
    from apps.tenant.invoicing.models import Invoice
    from .services import batch_get_partners_balances

    today = datetime.date.today()
    dormant_days = int(setting('dormant_debt_days', 60) or 60)
    warn_pct = float(setting('credit_limit_warning_percent', 80) or 80)
    partners_qs = Partner.objects.filter(is_active=True).select_related('sector')
    balances = batch_get_partners_balances(partner_qs=partners_qs)

    customers, suppliers = [], []
    receivables = payables = ZERO
    for data in balances.values():
        p, bal = data['partner'], data['final_balance'] or ZERO
        row = {'partner': p, 'balance': bal}
        if p.partner_type in (Partner.CUSTOMER, Partner.BOTH):
            customers.append(row)
        if p.partner_type in (Partner.SUPPLIER, Partner.BOTH):
            suppliers.append(row)
        if bal > 0:
            receivables += bal
        elif bal < 0:
            payables += -bal

    # Debtor / creditor by BALANCE, whatever the partner type (a customer who paid in
    # advance is a creditor; a supplier we overpaid is a debtor) -> tiles and lists agree.
    everyone = [{'partner': d['partner'], 'balance': d['final_balance'] or ZERO} for d in balances.values()]
    debtors = sorted([r for r in everyone if r['balance'] > 0], key=lambda r: r['balance'], reverse=True)
    creditors = sorted([r for r in everyone if r['balance'] < 0], key=lambda r: r['balance'])
    top_debtors = _hbars([{'label': r['partner'].name, 'value': r['balance'], 'pk': r['partner'].pk} for r in debtors[:TOP_N]])
    top_creditors = _hbars([{'label': r['partner'].name, 'value': -r['balance'], 'pk': r['partner'].pk} for r in creditors[:TOP_N]])

    # credit limit usage (customers with a limit)
    credit_rows = []
    for r in customers:
        limit = r['partner'].credit_limit or ZERO
        if limit > 0:
            usage = float(r['balance'] / limit * 100) if r['balance'] > 0 else 0.0
            credit_rows.append({'partner': r['partner'], 'balance': r['balance'], 'limit': limit, 'usage': usage,
                                'bar': min(usage, 100.0),
                                'level': 'critical' if usage >= 100 else ('warning' if usage >= warn_pct else 'good')})
    credit_rows.sort(key=lambda r: r['usage'], reverse=True)
    over_limit = [r for r in credit_rows if r['usage'] >= 100]

    # dormant debtors: owe us, no invoice / receipt for dormant_days (SystemSetting.dormant_debt_days)
    last_inv = dict(Invoice.objects.filter(status=Invoice.POSTED, partner__isnull=False)
                    .values('partner_id').annotate(d=Max('date')).values_list('partner_id', 'd'))
    last_vch = dict(Voucher.objects.filter(status=Voucher.POSTED, partner__isnull=False)
                    .values('partner_id').annotate(d=Max('date')).values_list('partner_id', 'd'))
    cutoff = today - datetime.timedelta(days=dormant_days)
    dormant = []
    for r in debtors:
        pid = r['partner'].pk
        last = max([d for d in (last_inv.get(pid), last_vch.get(pid)) if d] or [None]) if (last_inv.get(pid) or last_vch.get(pid)) else None
        if last is None or last < cutoff:
            dormant.append({'partner': r['partner'], 'balance': r['balance'], 'last': last,
                            'days': (today - last).days if last else None})
    dormant_value = sum((d['balance'] for d in dormant), ZERO)

    # receivables by sector
    sectors = {}
    for r in debtors:
        name = r['partner'].sector.name if r['partner'].sector_id else 'بدون قطاع'
        s = sectors.setdefault(name, {'label': name, 'value': ZERO, 'count': 0})
        s['value'] += r['balance']
        s['count'] += 1
    sector_rows = _hbars(sorted(sectors.values(), key=lambda s: s['value'], reverse=True))

    # 6-month trends
    months = _month_starts(today, MONTHS)
    posted = Invoice.objects.filter(status=Invoice.POSTED)
    vouchers = Voucher.objects.filter(status=Voucher.POSTED, partner__isnull=False)
    trend = {
        'labels': [f'{ARABIC_MONTHS[m.month - 1]} {m.year % 100:02d}' for m in months],
        'credit_sales': _monthly(posted.filter(invoice_type=Invoice.SALE), 'date', _credit_portion(), months),
        'collections': _monthly(vouchers.filter(voucher_type=Voucher.RECEIPT), 'date', 'amount', months),
        'credit_purchases': _monthly(posted.filter(invoice_type=Invoice.PURCHASE), 'date', _credit_portion(), months),
        'supplier_payments': _monthly(vouchers.filter(voucher_type=Voucher.PAYMENT), 'date', 'amount', months),
    }
    month_start = today.replace(day=1)
    collected_this_month = vouchers.filter(voucher_type=Voucher.RECEIPT, date__gte=month_start).aggregate(
        v=Coalesce(Sum('amount'), Value(ZERO), output_field=MONEY))['v']
    paid_this_month = vouchers.filter(voucher_type=Voucher.PAYMENT, date__gte=month_start).aggregate(
        v=Coalesce(Sum('amount'), Value(ZERO), output_field=MONEY))['v']

    recent_vouchers = list(vouchers.select_related('partner').order_by('-date', '-id')[:8])

    context = {
        'title': 'لوحة الشركاء',
        'customers_count': len(customers),
        'suppliers_count': len(suppliers),
        'debtors_count': len(debtors),
        'creditors_count': len(creditors),
        'receivables': receivables,
        'payables': payables,
        'net_position': receivables - payables,
        'collected_this_month': collected_this_month,
        'paid_this_month': paid_this_month,
        'top_debtors': top_debtors,
        'top_creditors': top_creditors,
        'credit_rows': credit_rows[:10],
        'over_limit_count': len(over_limit),
        'dormant': dormant[:8],
        'dormant_count': len(dormant),
        'dormant_value': dormant_value,
        'dormant_days': dormant_days,
        'credit_warning_percent': warn_pct,
        'sector_rows': sector_rows,
        'trend': trend,
        'months': MONTHS,
        'recent_vouchers': recent_vouchers,
        'new_customers_month': Partner.objects.filter(
            partner_type__in=[Partner.CUSTOMER, Partner.BOTH], created_at__date__gte=month_start).count()
        if hasattr(Partner, 'created_at') else None,
    }
    return render(request, 'partners/dashboard.html', context)
