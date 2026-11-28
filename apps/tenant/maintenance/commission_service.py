"""
Commission calculation service.
Calculates commission per cashier per period based on CommissionRule milestones.
Called from the commission report view.
"""
from decimal import Decimal
from datetime import date, timedelta
from django.contrib.auth import get_user_model
from apps.tenant.core.money import money

User = get_user_model()


def _get_period_range(period: str, target_date: date):
    """Return (start_date, end_date) for the given period."""
    if period == 'DAILY':
        return target_date, target_date
    elif period == 'MONTHLY':
        start = target_date.replace(day=1)
        if target_date.month == 12:
            end = target_date.replace(year=target_date.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = target_date.replace(month=target_date.month + 1, day=1) - timedelta(days=1)
        return start, end
    else:  # YEARLY
        return target_date.replace(month=1, day=1), target_date.replace(month=12, day=31)


def calculate_commissions(period: str, target_date: date, branch=None):
    """
    Calculate commission report for all cashiers in the given period.

    Returns a list of dicts:
      {
        'user': User,
        'total_sales': Decimal,
        'categories': [
            {'category_name': str, 'sales': Decimal, 'commission': Decimal, 'milestones': int},
            ...
        ],
        'total_commission': Decimal,
      }
    """
    from apps.tenant.invoicing.models import Invoice, InvoiceLine
    from apps.tenant.maintenance.commission_models import CommissionRule

    start_date, end_date = _get_period_range(period, target_date)

    # Load all active rules: {category_id: CommissionRule}
    rules = {
        r.category_id: r
        for r in CommissionRule.objects.filter(is_active=True).select_related('category')
    }

    if not rules:
        return [], start_date, end_date

    # Get all POSTED sale invoices and sale returns in the period.
    # Sales are measured at NET line value (InvoiceLine.subtotal = after line discount, before tax),
    # pro-rated by any extra invoice-level discount, and returns are subtracted.
    qs = Invoice.objects.filter(
        invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
        status=Invoice.POSTED,
        date__gte=start_date,
        date__lte=end_date,
    )

    if branch:
        qs = qs.filter(branch=branch)

    invoices = list(qs.values_list('id', 'invoice_type', 'cashier_id', 'parent_invoice__cashier_id', 'subtotal'))
    if not invoices:
        return [], start_date, end_date

    invoice_info = {}
    for inv_id, inv_type, cashier_id, parent_cashier_id, inv_subtotal in invoices:
        if inv_type == Invoice.RETURN_SALE:
            # Charge the return back to the cashier who made the original sale when known
            owner = parent_cashier_id or cashier_id
            sign = Decimal('-1')
        else:
            owner = cashier_id
            sign = Decimal('1')
        if owner:
            invoice_info[inv_id] = (owner, sign, inv_subtotal or Decimal('0'))

    if not invoice_info:
        return [], start_date, end_date

    from collections import defaultdict
    line_rows = list(
        InvoiceLine.objects
        .filter(invoice_id__in=list(invoice_info.keys()))
        .values_list('invoice_id', 'product__category_id', 'subtotal')
    )
    lines_sum = defaultdict(Decimal)
    for inv_id, _cat, sub in line_rows:
        lines_sum[inv_id] += sub or Decimal('0')

    totals = defaultdict(Decimal)  # (cashier_id, category_id) -> net sales
    for inv_id, category_id, sub in line_rows:
        if category_id not in rules:
            continue
        owner, sign, inv_subtotal = invoice_info[inv_id]
        net = sub or Decimal('0')
        ls = lines_sum[inv_id]
        # Extra invoice-level discount (invoice subtotal lower than the sum of its lines)
        if ls > 0 and Decimal('0') <= inv_subtotal < ls:
            net = net * inv_subtotal / ls
        totals[(owner, category_id)] += sign * net

    # Group by cashier
    cashier_data = defaultdict(list)  # cashier_id → [(category_id, sales_total)]
    for (cashier_id, category_id), sales in totals.items():
        sales = money(max(Decimal('0'), sales))
        cashier_data[cashier_id].append((category_id, sales))

    # Build report
    report = []
    cashier_ids = list(cashier_data.keys())
    users = {u.id: u for u in User.objects.filter(id__in=cashier_ids)}

    for cashier_id, cat_rows in cashier_data.items():
        user = users.get(cashier_id)
        if not user:
            continue

        categories = []
        total_sales = Decimal('0')
        total_commission = Decimal('0')

        for category_id, sales in cat_rows:
            rule = rules[category_id]
            commission = rule.calculate(sales)
            milestones = int(sales // rule.sales_milestone) if rule.sales_milestone > 0 else 0
            categories.append({
                'category_name': rule.category.name,
                'category_id': category_id,
                'sales': sales,
                'milestone': rule.sales_milestone,
                'commission_per_milestone': rule.commission_amount,
                'milestones_achieved': milestones,
                'commission': commission,
            })
            total_sales += sales
            total_commission += commission

        report.append({
            'user': user,
            'categories': categories,
            'total_sales': total_sales,
            'total_commission': total_commission,
        })

    report.sort(key=lambda x: x['total_commission'], reverse=True)
    return report, start_date, end_date
