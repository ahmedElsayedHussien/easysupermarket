"""
Commission calculation service.
Calculates commission per cashier per period based on CommissionRule milestones.
Called from the commission report view.
"""
from decimal import Decimal
from datetime import date, timedelta
from django.db.models import Sum, F
from django.contrib.auth import get_user_model

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

    # Get all POSTED sale invoices in the period
    qs = Invoice.objects.filter(
        invoice_type=Invoice.SALE,
        status=Invoice.POSTED,
        date__gte=start_date,
        date__lte=end_date,
        cashier__isnull=False,
    )
    
    if branch:
        qs = qs.filter(branch=branch)
        
    invoices = qs.values_list('id', 'cashier_id')

    # Map invoice_id → cashier_id
    invoice_cashier = {inv_id: cashier_id for inv_id, cashier_id in invoices}
    invoice_ids = list(invoice_cashier.keys())

    if not invoice_ids:
        return [], start_date, end_date

    # Aggregate sales per (cashier, category)
    lines_agg = (
        InvoiceLine.objects
        .filter(invoice_id__in=invoice_ids)
        .select_related('product__category')
        .values('invoice__cashier_id', 'product__category_id')
        .annotate(sales_total=Sum(F('quantity') * F('unit_price')))
    )

    # Group by cashier
    from collections import defaultdict
    cashier_data = defaultdict(list)  # cashier_id → [(category_id, sales_total)]

    for row in lines_agg:
        cashier_id  = row['invoice__cashier_id']
        category_id = row['product__category_id']
        sales       = row['sales_total'] or Decimal('0')
        if category_id in rules:
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
