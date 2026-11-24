"""
E-payment services dashboard (لوحة خدمات الدفع الإلكتروني).

Fawry / Aman / bank POS machines: today's activity, month commissions vs agent
fees, machine balances with low-balance alerts, 30-day net commission and
volume by operation type, recent operations and every e-service action.

Branch rule (core.access): operations follow their treasury's branch, machines
their own branch (admins: header branch / "all branches"; users: own branch).

Also home of ``eservice_cash_effect_expression()`` - the SQL twin of
``EServiceTransaction.cash_effect`` used by shift reconciliation.
"""
import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse

from apps.tenant.core.access import filter_by_branch, get_branch_scope

from .models import EServiceTransaction, JournalEntry, JournalItem, POSMachine

ZERO = Decimal('0.00')
CHART_DAYS = 30
_DEC = DecimalField(max_digits=18, decimal_places=4)


# ---------------------------------------------------------------------------
# Drawer cash effect in SQL (mirror of EServiceTransaction.cash_effect)
# ---------------------------------------------------------------------------

def eservice_cash_effect_expression():
    """
    Signed cash effect on the treasury / drawer, per row, in SQL. Must stay
    identical to the ``EServiceTransaction.cash_effect`` property:
      CASH_OUT                      -> -(principal - commission_revenue)
      CASH_IN                       -> +(principal + commission_revenue)
      MERCHANT_RECHARGE (paid now)  -> -(principal + commission_expense)
      MERCHANT_RECHARGE (on credit) -> 0
      anything not POSTED           -> 0
    """
    T = EServiceTransaction
    posted = Q(status=T.POSTED)
    return Case(
        When(posted & Q(transaction_type=T.CASH_OUT),
             then=F('commission_revenue') - F('principal_amount')),
        When(posted & Q(transaction_type=T.CASH_IN),
             then=F('principal_amount') + F('commission_revenue')),
        When(posted & Q(transaction_type=T.MERCHANT_RECHARGE, paid_on_credit=False),
             then=Value(Decimal('0')) - F('principal_amount') - F('commission_expense')),
        default=Value(Decimal('0')),
        output_field=_DEC,
    )


def eservice_cash_for_session(session):
    """Net drawer cash of the POSTED e-service operations recorded on a POS shift."""
    if session is None or not getattr(session, 'pk', None):
        return ZERO
    agg = EServiceTransaction.objects.filter(pos_session=session, status=EServiceTransaction.POSTED).aggregate(
        s=Coalesce(Sum(eservice_cash_effect_expression()), Value(Decimal('0')), output_field=_DEC))
    return agg['s'] or ZERO


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _can_view(user):
    from apps.tenant.core.decorators import is_admin_user
    return is_admin_user(user) or user.has_perm('accounting.view_eservicetransaction')


def _url_or_none(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return None


TYPE_SERIES = [  # fixed categorical order -> chart series slots 1..3
    (EServiceTransaction.CASH_IN, 'إيداع / تحويل', 1),
    (EServiceTransaction.CASH_OUT, 'سحب كاش', 2),
    (EServiceTransaction.MERCHANT_RECHARGE, 'شحن الماكينات', 3),
]


@login_required
def eservice_dashboard(request):
    if not _can_view(request.user):
        from django.contrib import messages
        messages.error(request, 'عفواً، لا تملك صلاحية عرض لوحة خدمات الدفع.')
        return redirect('core:main_screen')

    from apps.tenant.services.journal_service import batch_get_account_balances

    T = EServiceTransaction
    today = datetime.date.today()
    month_start = today.replace(day=1)
    chart_start = today - datetime.timedelta(days=CHART_DAYS - 1)

    ops = filter_by_branch(T.objects.all(), request, field='treasury__branch')
    posted = ops.filter(status=T.POSTED)
    machines = list(filter_by_branch(
        POSMachine.objects.select_related('branch', 'account'), request).order_by('-is_active', 'branch__name', 'name'))

    # --- KPIs ---
    today_agg = posted.filter(date=today).aggregate(
        n=Count('id'), volume=Coalesce(Sum('principal_amount'), Value(ZERO), output_field=_DEC))
    month_agg = posted.filter(date__gte=month_start, date__lte=today).aggregate(
        earned=Coalesce(Sum('commission_revenue', filter=Q(transaction_type__in=[T.CASH_IN, T.CASH_OUT])),
                        Value(ZERO), output_field=_DEC),
        fees=Coalesce(Sum('commission_expense', filter=Q(transaction_type=T.MERCHANT_RECHARGE)),
                     Value(ZERO), output_field=_DEC),
        n=Count('id'),
    )
    month_earned = month_agg['earned']
    month_fees = month_agg['fees']

    # --- machines ---
    account_ids = [m.account_id for m in machines if m.account_id]
    balances = batch_get_account_balances(account_ids=account_ids) if account_ids else {}
    last_moves = {}
    if account_ids:
        from django.db.models import Max
        last_moves = dict(JournalItem.objects.filter(account_id__in=account_ids, entry__status=JournalEntry.POSTED)
                          .values('account_id').annotate(d=Max('entry__date')).values_list('account_id', 'd'))
    per_machine_today = dict(posted.filter(date=today, pos_machine__isnull=False).order_by()
                             .values('pos_machine_id').annotate(v=Sum('principal_amount'))
                             .values_list('pos_machine_id', 'v'))
    per_machine_month = dict(posted.filter(date__gte=month_start, date__lte=today, pos_machine__isnull=False,
                                           transaction_type__in=[T.CASH_IN, T.CASH_OUT]).order_by()
                             .values('pos_machine_id').annotate(v=Sum('commission_revenue'))
                             .values_list('pos_machine_id', 'v'))
    machine_rows, low_balance = [], []
    for m in machines:
        bal = balances.get(m.account_id, ZERO) if m.account_id else ZERO
        alert = m.min_balance_alert or ZERO
        row = {
            'obj': m, 'balance': bal, 'branch': m.branch, 'account': m.account,
            'last_move': last_moves.get(m.account_id),
            'today_volume': per_machine_today.get(m.id) or ZERO,
            'month_commission': per_machine_month.get(m.id) or ZERO,
            'low': m.is_active and alert > 0 and bal < alert,
            'min_alert': alert,
        }
        machine_rows.append(row)
        if row['low']:
            low_balance.append(row)
    machines_total = sum((r['balance'] for r in machine_rows), ZERO)

    # --- 30-day series ---
    daily = {row['date']: row for row in posted.filter(date__gte=chart_start, date__lte=today).order_by()
             .values('date').annotate(
                 earned=Sum('commission_revenue', filter=Q(transaction_type__in=[T.CASH_IN, T.CASH_OUT])),
                 fees=Sum('commission_expense', filter=Q(transaction_type=T.MERCHANT_RECHARGE)))}
    by_type = {(row['date'], row['transaction_type']): row['v'] for row in
               posted.filter(date__gte=chart_start, date__lte=today).order_by()
               .values('date', 'transaction_type').annotate(v=Sum('principal_amount'))}
    labels, net_commission = [], []
    volumes = {code: [] for code, _, _ in TYPE_SERIES}
    for i in range(CHART_DAYS):
        day = chart_start + datetime.timedelta(days=i)
        labels.append(day.strftime('%m-%d'))
        d = daily.get(day, {})
        net_commission.append(float((d.get('earned') or 0) - (d.get('fees') or 0)))
        for code, _, _ in TYPE_SERIES:
            volumes[code].append(float(by_type.get((day, code)) or 0))
    type_totals = [{'code': code, 'label': label, 'series': s, 'total': sum(volumes[code])}
                   for code, label, s in TYPE_SERIES]
    chart_data = {
        'labels': labels,
        'net_commission': net_commission,
        'types': [{'code': t['code'], 'label': t['label'], 'series': t['series'], 'data': volumes[t['code']]}
                  for t in type_totals],
    }

    recent = ops.select_related('pos_machine', 'ewallet', 'treasury').order_by('-date', '-created_at', '-id')[:12]
    scope_branch = get_branch_scope(request)
    context = {
        'title': 'لوحة خدمات الدفع الإلكتروني',
        'scope_label': scope_branch.name if scope_branch else 'كل الفروع',
        'today': today,
        'today_count': today_agg['n'] or 0,
        'today_volume': today_agg['volume'],
        'month_earned': month_earned,
        'month_fees': month_fees,
        'month_net': month_earned - month_fees,
        'month_count': month_agg['n'] or 0,
        'machines_total': machines_total,
        'machine_rows': machine_rows,
        'active_machines': sum(1 for m in machines if m.is_active),
        'low_balance': low_balance,
        'chart_days': CHART_DAYS,
        'chart_data': chart_data,
        'chart_net_total': sum(net_commission),
        'type_totals': type_totals,
        'recent_ops': recent,
        'draft_count': ops.filter(status=T.DRAFT).count(),
        'commission_rules_url': _url_or_none('accounting:eservice_commission_rule_list'),
    }
    return render(request, 'accounting/eservice_dashboard.html', context)
