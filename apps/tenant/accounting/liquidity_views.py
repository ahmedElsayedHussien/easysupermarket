"""
Liquidity management dashboard (لوحة إدارة السيولة).

One page for treasuries, bank accounts, e-wallets and e-payment (POS)
machines: balances, composition, 30-day cash in/out, inter-branch transfers in
transit, recent movements, and every liquidity action (create accounts,
transfers, receipts/payments, machine recharge).

Branch rule (core.access): treasuries and POS machines follow the header
branch for admins ("all branches" shows every branch) and the own branch for
users. Banks and e-wallets are company-wide (no branch).

30-day flow: lines on our liquidity accounts, excluding
  * internal transfers (money only moves between our own accounts), and
  * e-service operations (Fawry/Aman/... CASH_IN / CASH_OUT / recharge): their
    principal just moves between the drawer and a machine. Each e-service
    entry (and its cancellation reversal) instead contributes its NET effect
    on liquidity accounts (sum debit - credit of its liquidity lines): the
    shop commission for CASH_IN/OUT (inflow), the agent fee for a cash
    recharge (outflow), the full principal for a recharge on credit (inflow,
    the agent is owed). Positive nets count as inflow, negative as outflow.
"""
import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q, Sum
from django.shortcuts import redirect, render

from apps.tenant.core.access import filter_by_branch, get_branch_scope

from .models import (
    BankAccount, EWallet, InternalTransfer, JournalEntry, JournalItem, POSMachine, Treasury,
)

ZERO = Decimal('0.00')
FLOW_DAYS = 30
LIQUIDITY_VIEW_PERMS = ('accounting.view_treasury', 'accounting.view_bankaccount', 'accounting.view_ewallet')


def _can_view(user):
    from apps.tenant.core.decorators import is_admin_user
    return is_admin_user(user) or any(user.has_perm(p) for p in LIQUIDITY_VIEW_PERMS)


def _heal_accounts(*querysets):
    for qs in querysets:
        for obj in qs.filter(account__isnull=True):
            obj.ensure_account()


def _rows(objects, kind, balances, last_moves):
    rows = []
    for obj in objects:
        acc_id = obj.account_id
        rows.append({
            'obj': obj,
            'kind': kind,
            'balance': balances.get(acc_id, ZERO) if acc_id else ZERO,
            'last_move': last_moves.get(acc_id),
            'account': obj.account,
            'branch': getattr(obj, 'branch', None),
        })
    rows.sort(key=lambda r: r['balance'], reverse=True)
    return rows


def _internal_transfer_entry_filter():
    """Entries that only MOVE money between our own liquidity accounts (or their reversals)."""
    return (Q(entry__internal_transfer__isnull=False)
            | Q(entry__internal_transfer_send__isnull=False)
            | Q(entry__reversal_of__internal_transfer__isnull=False)
            | Q(entry__reversal_of__internal_transfer_send__isnull=False))


def _eservice_entry_filter():
    """Journal lines of e-service operations (EServiceTransaction.journal_entry) or their reversals."""
    return (Q(entry__eservice_transaction__isnull=False)
            | Q(entry__reversal_of__eservice_transaction__isnull=False))


@login_required
def liquidity_dashboard(request):
    if not _can_view(request.user):
        from django.contrib import messages
        messages.error(request, 'عفواً، لا تملك صلاحية عرض لوحة السيولة.')
        return redirect('core:main_screen')

    from apps.tenant.services.journal_service import batch_get_account_balances
    from .views import incoming_transfers

    treasuries = filter_by_branch(Treasury.objects.select_related('branch', 'account'), request)
    banks = BankAccount.objects.select_related('account')
    ewallets = EWallet.objects.select_related('account')
    machines = filter_by_branch(POSMachine.objects.select_related('branch', 'account'), request)
    _heal_accounts(treasuries, banks, ewallets)

    account_ids = [a for a in (
        list(treasuries.values_list('account_id', flat=True))
        + list(banks.values_list('account_id', flat=True))
        + list(ewallets.values_list('account_id', flat=True))
        + list(machines.values_list('account_id', flat=True))
    ) if a]
    balances = batch_get_account_balances(account_ids=account_ids) if account_ids else {}

    posted_items = JournalItem.objects.filter(account_id__in=account_ids, entry__status=JournalEntry.POSTED)
    last_moves = dict(posted_items.values('account_id').annotate(d=Max('entry__date')).values_list('account_id', 'd'))

    treasury_rows = _rows(treasuries, 'TREASURY', balances, last_moves)
    bank_rows = _rows(banks, 'BANK', balances, last_moves)
    ewallet_rows = _rows(ewallets, 'EWALLET', balances, last_moves)
    machine_rows = _rows(machines, 'POS', balances, last_moves)
    for r in machine_rows:
        alert = r['obj'].min_balance_alert or ZERO
        r['low'] = r['obj'].is_active and alert > 0 and r['balance'] < alert
    low_machines = sum(1 for r in machine_rows if r['low'])
    all_rows = treasury_rows + bank_rows + ewallet_rows + machine_rows

    totals = {
        'TREASURY': sum((r['balance'] for r in treasury_rows), ZERO),
        'BANK': sum((r['balance'] for r in bank_rows), ZERO),
        'EWALLET': sum((r['balance'] for r in ewallet_rows), ZERO),
        'POS': sum((r['balance'] for r in machine_rows), ZERO),
    }
    grand_total = sum(totals.values(), ZERO)
    max_balance = max([r['balance'] for r in all_rows] + [ZERO])
    for r in all_rows:
        r['bar_pct'] = float(r['balance'] / max_balance * 100) if max_balance > 0 and r['balance'] > 0 else 0

    # --- 30-day cash in / out (excluding transfers between our own accounts; see module doc) ---
    today = datetime.date.today()
    start = today - datetime.timedelta(days=FLOW_DAYS - 1)
    period_items = posted_items.filter(entry__date__gte=start, entry__date__lte=today)
    external = period_items.exclude(_internal_transfer_entry_filter()).exclude(_eservice_entry_filter())
    by_day = {row['entry__date']: row for row in
              external.order_by().values('entry__date').annotate(inflow=Sum('debit'), outflow=Sum('credit'))}
    eservice_in, eservice_out = {}, {}
    for row in (period_items.filter(_eservice_entry_filter()).order_by()
                .values('entry_id', 'entry__date').annotate(d=Sum('debit'), c=Sum('credit'))):
        net = (row['d'] or ZERO) - (row['c'] or ZERO)
        bucket = eservice_in if net > 0 else eservice_out
        bucket[row['entry__date']] = bucket.get(row['entry__date'], ZERO) + abs(net)
    flow_labels, flow_in, flow_out = [], [], []
    for i in range(FLOW_DAYS):
        day = start + datetime.timedelta(days=i)
        row = by_day.get(day, {})
        flow_labels.append(day.strftime('%m-%d'))
        flow_in.append(float((row.get('inflow') or ZERO) + eservice_in.get(day, ZERO)))
        flow_out.append(float((row.get('outflow') or ZERO) + eservice_out.get(day, ZERO)))
    total_in = sum(flow_in)
    total_out = sum(flow_out)

    # --- inter-branch transfers in transit ---
    incoming = incoming_transfers(request.user)
    incoming_stats = incoming.aggregate(n=Count('id'), total=Sum('amount'))
    outgoing = InternalTransfer.objects.filter(
        status=InternalTransfer.IN_TRANSIT, from_account_type=InternalTransfer.TREASURY,
        from_treasury__in=treasuries,
    ).select_related('from_treasury__branch', 'to_treasury__branch')
    outgoing_stats = outgoing.aggregate(n=Count('id'), total=Sum('amount'))
    draft_transfers = InternalTransfer.objects.filter(status=InternalTransfer.DRAFT).filter(
        Q(from_treasury__in=treasuries) | Q(from_account_type__in=[InternalTransfer.BANK, InternalTransfer.EWALLET])
    ).count()

    recent = (posted_items.select_related('entry', 'account').order_by('-entry__date', '-entry_id', '-id')[:12])
    account_names = {}
    for r in all_rows:
        if r['obj'].account_id:
            account_names[r['obj'].account_id] = r['obj'].name

    composition = [
        {'key': 'TREASURY', 'label': 'نقدية بالخزائن', 'value': totals['TREASURY'], 'series': 1},
        {'key': 'BANK', 'label': 'حسابات البنوك', 'value': totals['BANK'], 'series': 2},
        {'key': 'EWALLET', 'label': 'المحافظ الإلكترونية', 'value': totals['EWALLET'], 'series': 3},
        {'key': 'POS', 'label': 'ماكينات الدفع', 'value': totals['POS'], 'series': 4},
    ]
    positive_total = sum((c['value'] for c in composition if c['value'] > 0), ZERO)
    for c in composition:
        c['pct'] = float(c['value'] / positive_total * 100) if positive_total > 0 and c['value'] > 0 else 0

    scope_branch = get_branch_scope(request)
    context = {
        'title': 'لوحة إدارة السيولة',
        'scope_label': scope_branch.name if scope_branch else 'كل الفروع',
        'treasury_rows': treasury_rows,
        'bank_rows': bank_rows,
        'ewallet_rows': ewallet_rows,
        'machine_rows': machine_rows,
        'low_machines': low_machines,
        'totals': totals,
        'grand_total': grand_total,
        'composition': composition,
        'total_in': total_in,
        'total_out': total_out,
        'net_flow': total_in - total_out,
        'flow_days': FLOW_DAYS,
        'flow_data': {'labels': flow_labels, 'inflow': flow_in, 'outflow': flow_out},
        'incoming_transfers': incoming[:5],
        'incoming_count': incoming_stats['n'] or 0,
        'incoming_total': incoming_stats['total'] or ZERO,
        'outgoing_transfers': outgoing[:5],
        'outgoing_count': outgoing_stats['n'] or 0,
        'outgoing_total': outgoing_stats['total'] or ZERO,
        'draft_transfers': draft_transfers,
        'recent_rows': [{'item': it, 'name': account_names.get(it.account_id, it.account.name)} for it in recent],
    }
    return render(request, 'accounting/liquidity_dashboard.html', context)
