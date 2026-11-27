"""
Accounting dashboard (لوحة المحاسبة).

Replaces 9 side-menu links (expenses, receipts, payments, money transfers,
cheques, fixed assets, loans, journal entries, chart of accounts) with one page:
month P&L, 6-month revenue vs expenses, expense breakdown, things that need
action (drafts, cheques / loan installments due, depreciation not run, ledger
balance check), period-lock status, recent journal entries, every action.

Ledger figures (P&L) are company-wide — journal lines have no branch. Drafts
follow the usual branch rule (accounting.views scope_* helpers).
"""
import datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import redirect, render

from apps.tenant.inventory.models import InventoryBatch

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=20, decimal_places=4)
MONTHS = 6
DUE_SOON_DAYS = 7
TOP_EXPENSES = 7
ARABIC_MONTHS = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']
VIEW_PERMS = ('accounting.view_journalentry', 'accounting.view_account', 'accounting.view_expense',
              'accounting.view_voucher')


def _can_view(user):
    from apps.tenant.core.decorators import is_admin_user
    return is_admin_user(user) or any(user.has_perm(p) for p in VIEW_PERMS)


def _month_starts(today, n):
    first = today.replace(day=1)
    out = []
    for _ in range(n):
        out.append(first)
        first = (first - datetime.timedelta(days=1)).replace(day=1)
    return list(reversed(out))


def _pl_by_month(items, since):
    """{month_start: {'revenue': D, 'expenses': D}} from posted journal lines."""
    from .models import Account
    rows = (items.filter(entry__date__gte=since,
                         account__account_type__in=[Account.REVENUE, Account.EXPENSE])
            .annotate(m=TruncMonth('entry__date'))
            .values('m', 'account__account_type')
            .annotate(d=Coalesce(Sum('debit'), Value(ZERO), output_field=MONEY),
                      c=Coalesce(Sum('credit'), Value(ZERO), output_field=MONEY)))
    out = {}
    for r in rows:
        m = r['m'].date() if hasattr(r['m'], 'date') else r['m']
        bucket = out.setdefault(m, {'revenue': ZERO, 'expenses': ZERO})
        if r['account__account_type'] == Account.REVENUE:
            bucket['revenue'] += r['c'] - r['d']      # contra revenue (returns) nets itself
        else:
            bucket['expenses'] += r['d'] - r['c']     # incl. COGS; contra expense nets itself
    return out


@login_required
def accounting_dashboard(request):
    if not _can_view(request.user):
        from django.contrib import messages
        messages.error(request, 'عفواً، لا تملك صلاحية عرض لوحة المحاسبة.')
        return redirect('core:main_screen')

    from apps.tenant.core.settings_helpers import accounting_lock_date
    from .models import (Account, AssetDepreciationLog, Cheque, ChequeStatus, ChequeType, Expense,
                         FixedAsset, InternalTransfer, JournalEntry, JournalItem, LoanInstallment, Voucher)
    from .views import scope_expenses, scope_transfers, scope_vouchers

    today = datetime.date.today()
    month_start = today.replace(day=1)
    soon = today + datetime.timedelta(days=DUE_SOON_DAYS)
    posted_items = JournalItem.objects.filter(entry__status=JournalEntry.POSTED)

    # --- P&L: this month + 6-month trend ---
    months = _month_starts(today, MONTHS)
    pl = _pl_by_month(posted_items, months[0])
    this_month = pl.get(month_start, {'revenue': ZERO, 'expenses': ZERO})
    trend = {
        'labels': [f'{ARABIC_MONTHS[m.month - 1]} {m.year % 100:02d}' for m in months],
        'revenue': [float(pl.get(m, {}).get('revenue', 0)) for m in months],
        'expenses': [float(pl.get(m, {}).get('expenses', 0)) for m in months],
    }
    trend['net'] = [round(r - e, 2) for r, e in zip(trend['revenue'], trend['expenses'])]

    # --- expense breakdown this month (by account) ---
    exp_rows = list(posted_items.filter(entry__date__gte=month_start, account__account_type=Account.EXPENSE)
                    .values('account__code', 'account__name')
                    .annotate(v=Sum('debit') - Sum('credit')).order_by('-v'))
    exp_rows = [r for r in exp_rows if (r['v'] or 0) > 0]
    expense_bars = [{'label': f"{r['account__name']}", 'code': r['account__code'], 'value': r['v']} for r in exp_rows]
    if len(expense_bars) > TOP_EXPENSES:
        other = sum((b['value'] for b in expense_bars[TOP_EXPENSES - 1:]), ZERO)
        expense_bars = expense_bars[:TOP_EXPENSES - 1] + [{'label': 'أخرى', 'code': '', 'value': other, 'other': True}]
    peak = max([b['value'] for b in expense_bars] + [ZERO])
    total_exp = sum((b['value'] for b in expense_bars), ZERO)
    for b in expense_bars:
        b['pct'] = float(b['value'] / peak * 100) if peak > 0 else 0
        b['share'] = float(b['value'] / total_exp * 100) if total_exp > 0 else 0

    # --- needs action ---
    draft_vouchers = scope_vouchers(Voucher.objects.filter(status=Voucher.DRAFT), request.user).count()
    draft_expenses = scope_expenses(Expense.objects.filter(status=Expense.DRAFT), request.user).count()
    draft_transfers = scope_transfers(InternalTransfer.objects.filter(status=InternalTransfer.DRAFT), request.user).count()

    open_receivable = [ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO]
    cheques_in = Cheque.objects.filter(cheque_type=ChequeType.RECEIVABLE, status__in=open_receivable)
    cheques_out = Cheque.objects.filter(cheque_type=ChequeType.PAYABLE, status=ChequeStatus.ISSUED)

    def due_stats(qs):
        return qs.aggregate(
            soon_n=Count('id', filter=Q(due_date__gte=today, due_date__lte=soon)),
            soon_v=Coalesce(Sum('amount', filter=Q(due_date__gte=today, due_date__lte=soon)), Value(ZERO), output_field=MONEY),
            over_n=Count('id', filter=Q(due_date__lt=today)),
            over_v=Coalesce(Sum('amount', filter=Q(due_date__lt=today)), Value(ZERO), output_field=MONEY),
        )

    cin, cout = due_stats(cheques_in), due_stats(cheques_out)
    bounced_month = Cheque.objects.filter(status=ChequeStatus.BOUNCED, due_date__gte=month_start).count()
    upcoming_cheques = list(Cheque.objects.filter(
        Q(cheque_type=ChequeType.RECEIVABLE, status__in=open_receivable) | Q(cheque_type=ChequeType.PAYABLE, status=ChequeStatus.ISSUED),
        due_date__lte=soon,
    ).select_related('partner').order_by('due_date')[:8])

    installments = LoanInstallment.objects.filter(is_paid=False)
    inst = installments.aggregate(
        soon_n=Count('id', filter=Q(due_date__gte=today, due_date__lte=soon)),
        soon_v=Coalesce(Sum('total_amount', filter=Q(due_date__gte=today, due_date__lte=soon)), Value(ZERO), output_field=MONEY),
        over_n=Count('id', filter=Q(due_date__lt=today)),
        over_v=Coalesce(Sum('total_amount', filter=Q(due_date__lt=today)), Value(ZERO), output_field=MONEY),
    )
    upcoming_installments = list(installments.filter(due_date__lte=soon).select_related('loan').order_by('due_date')[:6])

    # depreciation of the previous month not run yet for active assets
    prev_month_end = month_start - datetime.timedelta(days=1)
    active_assets = FixedAsset.objects.filter(status=FixedAsset.AssetStatus.ACTIVE, purchase_date__lte=prev_month_end)
    depreciated_ids = AssetDepreciationLog.objects.filter(
        year=prev_month_end.year, month=prev_month_end.month).values_list('asset_id', flat=True)
    assets_pending_dep = active_assets.exclude(id__in=depreciated_ids).count()

    totals = posted_items.aggregate(d=Coalesce(Sum('debit'), Value(ZERO), output_field=MONEY),
                                    c=Coalesce(Sum('credit'), Value(ZERO), output_field=MONEY))
    ledger_diff = (totals['d'] or ZERO) - (totals['c'] or ZERO)

    inv_acc = Account.objects.filter(code='1140').first()
    inv_ledger_val = inv_acc.current_balance if inv_acc else ZERO
    inv_batches_val = InventoryBatch.objects.exclude(quantity_remaining=ZERO).aggregate(
        v=Sum(F('quantity_remaining') * F('unit_cost'), output_field=MONEY)
    )['v'] or ZERO
    inventory_diff = inv_batches_val - inv_ledger_val
    if abs(inventory_diff) < Decimal('0.01'):
        inventory_diff = ZERO

    recent_entries = list(JournalEntry.objects.filter(status=JournalEntry.POSTED)
                          .annotate(total=Sum('items__debit')).order_by('-date', '-id')[:10])

    context = {
        'title': 'لوحة المحاسبة',
        'today': today,
        'month_label': f'{ARABIC_MONTHS[today.month - 1]} {today.year}',
        'revenue_month': this_month['revenue'],
        'expenses_month': this_month['expenses'],
        'net_month': this_month['revenue'] - this_month['expenses'],
        'expenses_today': posted_items.filter(entry__date=today, account__account_type=Account.EXPENSE)
        .aggregate(v=Coalesce(Sum('debit'), Value(ZERO), output_field=MONEY))['v'],
        'trend': trend,
        'months': MONTHS,
        'expense_bars': expense_bars,
        'draft_vouchers': draft_vouchers,
        'draft_expenses': draft_expenses,
        'draft_transfers': draft_transfers,
        'drafts_total': draft_vouchers + draft_expenses + draft_transfers,
        'cheques_in': cin, 'cheques_out': cout, 'bounced_month': bounced_month,
        'upcoming_cheques': upcoming_cheques,
        'installments': inst, 'upcoming_installments': upcoming_installments,
        'assets_pending_dep': assets_pending_dep,
        'prev_month_label': f'{ARABIC_MONTHS[prev_month_end.month - 1]} {prev_month_end.year}',
        'ledger_diff': ledger_diff,
        'inventory_diff': inventory_diff,
        'lock_date': accounting_lock_date(),
        'due_days': DUE_SOON_DAYS,
        'recent_entries': recent_entries,
    }
    return render(request, 'accounting/accounting_dashboard.html', context)
