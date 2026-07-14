from django.shortcuts import render
from django.views.generic import View, ListView, TemplateView
from django.db.models import Sum, Count, F, Q, FloatField
from django.db.models.functions import TruncDay, TruncMonth, Cast
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from apps.tenant.core.mixins import CustomPermissionRequiredMixin
from apps.tenant.invoicing.models import Invoice, InvoiceLine
from apps.tenant.inventory.models import Product
from apps.tenant.partners.models import Partner
import csv
from django.http import HttpResponse

class CsvExportMixin:
    """
    Mixin to allow exporting any ListView to a CSV file.
    The view must define `csv_filename`, `get_csv_headers()`, and `get_csv_row(obj)`.
    """
    csv_filename = "export.csv"
    
    def get_csv_headers(self):
        return []
        
    def get_csv_row(self, obj):
        return []

    def get(self, request, *args, **kwargs):
        if request.GET.get('export') == 'excel':
            # Need to disable pagination for export
            self.paginate_by = None
            
            # Allow the view to fetch and process the full unpaginated queryset
            # Some views compute data in get_context_data, so we'll just run 
            # get_context_data and fetch the data from the context.
            response = super().get(request, *args, **kwargs)
            context = response.context_data
            object_list = context.get(self.context_object_name) or context.get('object_list', [])
            
            # Build CSV Response
            csv_resp = HttpResponse(content_type='text/csv; charset=utf-8-sig')
            csv_resp['Content-Disposition'] = f'attachment; filename="{self.csv_filename}"'
            csv_resp.write('\ufeff') # BOM for Excel Arabic support
            
            writer = csv.writer(csv_resp)
            writer.writerow(self.get_csv_headers())
            
            for obj in object_list:
                writer.writerow(self.get_csv_row(obj))
                
            return csv_resp
            
        return super().get(request, *args, **kwargs)

# ---------------------------------------------------------
# Product Sales Report (مبيعات الأصناف)
# ---------------------------------------------------------
class ProductSalesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/sales_by_product.html'
    context_object_name = 'products_data'
    paginate_by = 20
    csv_filename = "product_sales_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'الكمية المباعة (بيع)', 'الكمية المسترجعة (مرتجع)', 'صافي الكمية المباعة', 'إجمالي الإيرادات']

    def get_csv_row(self, obj):
        return [
            obj['product__barcode'],
            obj['product__name'],
            obj['sold_qty'],
            obj['returned_qty'],
            obj['net_qty'],
            obj['net_total']
        ]

    def get_queryset(self):
        # Base query: Posted Sales Invoices
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.SALE,
            invoice__status=Invoice.POSTED
        )
        
        # Apply Filters
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(invoice__date__gte=start_date)
        if end_date:
            qs = qs.filter(invoice__date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__barcode__icontains=search_query)
            )
            
        # Aggregate by product
        aggregated = qs.values(
            'product__id', 
            'product__name', 
            'product__barcode'
        ).annotate(
            total_qty=Sum('quantity'),
            total_revenue=Sum('subtotal'),
            total_cogs=Sum('cogs_amount'),
        ).order_by('-total_revenue')
        
        # Calculate profit manually or via annotation
        # Due to DecimalField operations, sometimes it's better to calculate in python or use expression
        # We will calculate it directly in the annotation
        aggregated = aggregated.annotate(
            total_profit=F('total_revenue') - F('total_cogs')
        )
        
        return aggregated
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير مبيعات الأصناف'
        context['start_date'] = self.request.GET.get('start_date', '')
        context['end_date'] = self.request.GET.get('end_date', '')
        context['search_query'] = self.request.GET.get('search_query', '')
        
        # Top 10 for chart
        top_10 = list(self.get_queryset()[:10])
        context['chart_labels'] = [item['product__name'] for item in top_10]
        context['chart_data_qty'] = [float(item['total_qty'] or 0) for item in top_10]
        context['chart_data_revenue'] = [float(item['total_revenue'] or 0) for item in top_10]
        
        # Grand totals
        grand_totals = self.get_queryset().aggregate(
            gt_qty=Sum('total_qty'),
            gt_rev=Sum('total_revenue'),
            gt_profit=Sum('total_profit')
        )
        context['grand_totals'] = grand_totals
        
        return context

# ---------------------------------------------------------
# Period Sales Report (مبيعات فترة)
# ---------------------------------------------------------
class PeriodSalesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/sales_by_period.html'
    context_object_name = 'periods_data'
    paginate_by = 31
    csv_filename = "period_sales_report.csv"

    def get_csv_headers(self):
        return ['الفترة', 'إجمالي المبيعات', 'المرتجعات', 'صافي المبيعات', 'الضريبة']

    def get_csv_row(self, obj):
        return [
            obj['period'].strftime('%Y-%m-%d') if hasattr(obj['period'], 'strftime') else obj['period'],
            obj['total_sales'],
            obj['total_returns'],
            obj['net_sales'],
            obj['net_tax']
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.SALE,
            invoice__status=Invoice.POSTED
        )
        
        # Apply Filters
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        group_by = self.request.GET.get('group_by', 'day') # day or month
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(invoice__date__gte=start_date)
        if end_date:
            qs = qs.filter(invoice__date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(invoice__invoice_number__icontains=search_query) |
                Q(invoice__partner__name__icontains=search_query)
            )
            
        # Determine grouping
        if group_by == 'month':
            trunc_func = TruncMonth('invoice__date')
        else:
            trunc_func = TruncDay('invoice__date')
            
        # Aggregate
        # Note: Querying InvoiceLine directly avoids duplicate sums from JOINs!
        aggregated = qs.annotate(
            period=trunc_func
        ).values('period').annotate(
            invoice_count=Count('invoice_id', distinct=True),
            total_revenue=Sum('subtotal'),
            total_tax=Sum('tax_amount'),
            total_discount=Sum('discount_amount'),
            total_cogs=Sum('cogs_amount')
        ).order_by('-period')
        
        # Calculate profit
        aggregated = aggregated.annotate(
            total_profit=F('total_revenue') - F('total_cogs')
        )
        
        return aggregated
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير مبيعات الفترات الزمنية'
        context['start_date'] = self.request.GET.get('start_date', '')
        context['end_date'] = self.request.GET.get('end_date', '')
        context['group_by'] = self.request.GET.get('group_by', 'day')
        context['search_query'] = self.request.GET.get('search_query', '')
        
        # Prepare chart data (needs to be ascending for time series)
        chart_qs = list(self.get_queryset().order_by('period'))
        
        # Format dates based on grouping
        if context['group_by'] == 'month':
            context['chart_labels'] = [item['period'].strftime('%Y-%m') if item['period'] else 'N/A' for item in chart_qs]
        else:
            context['chart_labels'] = [item['period'].strftime('%Y-%m-%d') if item['period'] else 'N/A' for item in chart_qs]
            
        context['chart_data_revenue'] = [float(item['total_revenue'] or 0) for item in chart_qs]
        context['chart_data_profit'] = [float(item['total_profit'] or 0) for item in chart_qs]
        
        # Grand totals
        grand_totals = self.get_queryset().aggregate(
            gt_count=Sum('invoice_count'),
            gt_rev=Sum('total_revenue'),
            gt_profit=Sum('total_profit')
        )
        context['grand_totals'] = grand_totals
        
        return context

# ---------------------------------------------------------
# Detailed Sales Report (مبيعات تفصيلي)
# ---------------------------------------------------------
class DetailedSalesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/sales_detailed.html'
    context_object_name = 'invoice_lines'
    paginate_by = 50
    csv_filename = "detailed_sales_report.csv"

    def get_csv_headers(self):
        return ['التاريخ', 'رقم الفاتورة', 'نوع الحركة', 'العميل', 'الصنف', 'الكمية', 'السعر', 'الإجمالي']

    def get_csv_row(self, obj):
        return [
            obj.invoice.date.strftime('%Y-%m-%d') if hasattr(obj.invoice, 'date') and obj.invoice.date else '',
            obj.invoice.invoice_number,
            'مبيعات' if obj.invoice.invoice_type == Invoice.SALE else 'مرتجع',
            obj.invoice.partner.name if obj.invoice.partner else 'عميل نقدي/طياري',
            obj.product.name if obj.product else '',
            obj.quantity,
            obj.unit_price,
            obj.total_amount
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
            invoice__status=Invoice.POSTED
        ).select_related('invoice', 'invoice__partner', 'product')
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(invoice__date__gte=start_date)
        if end_date:
            qs = qs.filter(invoice__date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(invoice__partner__name__icontains=search_query) |
                Q(invoice__invoice_number__icontains=search_query) |
                Q(product__name__icontains=search_query)
            )
            
        qs = qs.annotate(
            calculated_total=F('subtotal') + F('tax_amount') - F('wht_amount')
        )
            
        return qs.order_by('-invoice__date', '-invoice__created_at', 'id')
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير المبيعات التفصيلي (حركة الأصناف)'
        context['start_date'] = self.request.GET.get('start_date', '')
        context['end_date'] = self.request.GET.get('end_date', '')
        context['search_query'] = self.request.GET.get('search_query', '')
        
        # Calculate grand totals over the entire filtered queryset
        from django.db.models import Case, When, DecimalField
        qs = self.get_queryset()
        
        # For returns, we might want to subtract from totals, but usually detailed reports 
        # just show the raw line amounts, and we subtract Returns if needed.
        # Here we'll sum them. If RETURN_SALE amounts are positive in DB, we should negate them for totals.
        grand_totals = qs.aggregate(
            total_sales=Sum(
                Case(
                    When(invoice__invoice_type=Invoice.SALE, then=F('subtotal') + F('tax_amount') - F('wht_amount')),
                    When(invoice__invoice_type=Invoice.RETURN_SALE, then=(F('subtotal') + F('tax_amount') - F('wht_amount')) * -1),
                    default=0,
                    output_field=DecimalField()
                )
            ),
            total_tax=Sum(
                Case(
                    When(invoice__invoice_type=Invoice.SALE, then=F('tax_amount')),
                    When(invoice__invoice_type=Invoice.RETURN_SALE, then=F('tax_amount') * -1),
                    default=0,
                    output_field=DecimalField()
                )
            ),
            total_qty=Sum(
                Case(
                    When(invoice__invoice_type=Invoice.SALE, then=F('quantity')),
                    When(invoice__invoice_type=Invoice.RETURN_SALE, then=F('quantity') * -1),
                    default=0,
                    output_field=DecimalField()
                )
            )
        )
        context['grand_totals'] = grand_totals
        
        return context

class CustomerBalancesReportView(CsvExportMixin, CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/customer_balances.html'
    context_object_name = 'transactions'
    csv_filename = "customer_statement.csv"

    def get_csv_headers(self):
        return ['التاريخ', 'رقم الحركة', 'نوع الحركة', 'مدين (مبيعات)', 'دائن (سداد)', 'الرصيد التراكمي']

    def get_csv_row(self, obj):
        return [
            obj['date'].strftime('%Y-%m-%d') if hasattr(obj['date'], 'strftime') else obj['date'],
            obj['reference'],
            obj['type'],
            obj['debit'],
            obj['credit'],
            obj['balance']
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.core.paginator import Paginator
        from decimal import Decimal
        from apps.tenant.partners.models import Partner
        
        partner_id = self.request.GET.get('partner_id')
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        
        context['partners'] = Partner.objects.filter(partner_type__in=[Partner.CUSTOMER, Partner.BOTH]).order_by('name')
        context['start_date'] = start_date
        context['end_date'] = end_date
        
        if partner_id and partner_id.isdigit():
            context['partner_id'] = int(partner_id)
            try:
                partner = Partner.objects.get(id=int(partner_id))
                context['selected_partner'] = partner
                
                invoices = partner.invoices.filter(status='POSTED')
                payments = partner.payments.filter(status='POSTED')
                
                transactions = []
                from datetime import timedelta
                
                for inv in invoices:
                    debit = Decimal('0')
                    credit = Decimal('0')
                    if inv.invoice_type in ['SALE', 'RETURN_PURCHASE']:
                        debit = inv.total_amount
                    else:
                        credit = inv.total_amount
                        
                    transactions.append({
                        'date': inv.date,
                        'created_at': inv.created_at,
                        'type': inv.get_invoice_type_display(),
                        'reference': inv.invoice_number,
                        'debit': debit,
                        'credit': credit,
                    })
                    
                    if inv.payment_type != 'CREDIT':
                        sim_debit = credit
                        sim_credit = debit
                        payment_display = inv.get_payment_type_display()
                        
                        transactions.append({
                            'date': inv.date,
                            'created_at': inv.created_at + timedelta(seconds=1),
                            'type': f'سداد فوري ({payment_display})',
                            'reference': f'تسوية {inv.invoice_number}',
                            'debit': sim_debit,
                            'credit': sim_credit,
                        })
                    
                for pay in payments:
                    debit = Decimal('0')
                    credit = Decimal('0')
                    if pay.payment_type == 'RECEIPT':
                        credit = pay.amount
                    else:
                        debit = pay.amount
                        
                    transactions.append({
                        'date': pay.date,
                        'created_at': pay.created_at,
                        'type': pay.get_payment_type_display(),
                        'reference': pay.reference,
                        'debit': debit,
                        'credit': credit,
                    })
                    
                # Sort transactions by date, then created_at
                transactions.sort(key=lambda x: (x['date'], x['created_at']))
                
                # Calculate running balance and filter
                running_balance = Decimal('0')
                opening_balance = Decimal('0')
                filtered_transactions = []
                
                for txn in transactions:
                    running_balance += txn['debit']
                    running_balance -= txn['credit']
                    txn['balance'] = running_balance
                    
                    include = True
                    if start_date and str(txn['date']) < start_date:
                        include = False
                        opening_balance = running_balance
                    if end_date and str(txn['date']) > end_date:
                        include = False
                        
                    if include:
                        filtered_transactions.append(txn)
                
                page_number = self.request.GET.get('page', 1)
                paginator = Paginator(filtered_transactions, 50)
                page_obj = paginator.get_page(page_number)
                
                context['transactions'] = page_obj
                context['page_obj'] = page_obj
                context['is_paginated'] = page_obj.has_other_pages()
                context['opening_balance'] = opening_balance
                context['final_balance'] = running_balance
                context['period_debit'] = sum(t['debit'] for t in filtered_transactions)
                context['period_credit'] = sum(t['credit'] for t in filtered_transactions)
            except Partner.DoesNotExist:
                pass
        return context

class ItemProfitabilityReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/item_profitability.html'
    context_object_name = 'products_data'
    paginate_by = 20
    csv_filename = "item_profitability_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'النوع', 'صافي الكمية المباعة', 'صافي قيمة المبيعات', 'متوسط التكلفة', 'إجمالي التكلفة', 'الربح', 'نسبة الهامش %']

    def get_csv_row(self, obj):
        return [
            obj['product__barcode'],
            obj['product__name'],
            'منتج' if obj['product__type'] == 'PRODUCT' else 'خدمة',
            obj['net_qty'],
            obj['net_sales'],
            obj['avg_cost'],
            obj['total_cost'],
            obj['profit'],
            obj['margin_percent']
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.SALE,
            invoice__status=Invoice.POSTED
        )
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        if start_date: qs = qs.filter(invoice__date__gte=start_date)
        if end_date: qs = qs.filter(invoice__date__lte=end_date)
        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__barcode__icontains=search_query)
            )

        aggregated = qs.values(
            'product__id',
            'product__name',
            'product__barcode',
            'product__product_type'
        ).annotate(
            total_qty=Sum('quantity'),
            total_revenue=Sum('subtotal'),
            total_cogs=Sum('cogs_amount')
        )
        
        from django.db.models import ExpressionWrapper, DecimalField
        aggregated = aggregated.annotate(
            total_profit=ExpressionWrapper(F('total_revenue') - F('total_cogs'), output_field=DecimalField())
        ).order_by('-total_profit')

        return aggregated

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['start_date'] = self.request.GET.get('start_date', '')
        context['end_date'] = self.request.GET.get('end_date', '')
        context['search_query'] = self.request.GET.get('search_query', '')
        
        from decimal import Decimal
        for item in context['products_data']:
            rev = item.get('total_revenue') or Decimal('0')
            prof = item.get('total_profit') or Decimal('0')
            item['profit_margin'] = (prof / rev * 100) if rev > 0 else Decimal('0')
            
        # Grand totals
        qs = self.get_queryset()
        if qs:
            gt_profit = sum(x['total_profit'] or 0 for x in qs)
            gt_revenue = sum(x['total_revenue'] or 0 for x in qs)
            context['grand_totals'] = {
                'gt_profit': gt_profit,
                'gt_margin': (gt_profit / gt_revenue * 100) if gt_revenue > 0 else 0,
            }
        else:
            context['grand_totals'] = {'gt_profit': 0, 'gt_margin': 0}
        return context

class UserSalesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/sales_by_user.html'
    context_object_name = 'users_data'
    paginate_by = 20
    csv_filename = "sales_by_user_report.csv"

    def get_csv_headers(self):
        return ['اسم المستخدم / الكاشير', 'يوزر نيم', 'عدد فواتير البيع', 'قيمة المبيعات', 'عدد المرتجعات', 'قيمة المرتجعات', 'صافي الضريبة', 'صافي المبيعات (الدخل)']

    def get_csv_row(self, obj):
        name = f"{obj['cashier__first_name']} {obj['cashier__last_name']}".strip()
        if not name:
            name = obj['cashier__username']
        return [
            name,
            obj['cashier__username'],
            obj['sales_count'],
            obj['total_sales'],
            obj['returns_count'],
            obj['total_returns'],
            obj['net_tax'],
            obj['net_sales']
        ]

    def get_queryset(self):
        qs = Invoice.objects.filter(
            invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
            status=Invoice.POSTED
        )
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(cashier__first_name__icontains=search_query) |
                Q(cashier__last_name__icontains=search_query) |
                Q(cashier__username__icontains=search_query)
            )
            
        aggregated = qs.values(
            'cashier__id',
            'cashier__username',
            'cashier__first_name',
            'cashier__last_name'
        ).annotate(
            sales_count=Count('id', filter=Q(invoice_type=Invoice.SALE)),
            returns_count=Count('id', filter=Q(invoice_type=Invoice.RETURN_SALE)),
            total_sales=Sum('total_amount', filter=Q(invoice_type=Invoice.SALE)),
            total_returns=Sum('total_amount', filter=Q(invoice_type=Invoice.RETURN_SALE)),
            total_tax_sales=Sum('tax_amount', filter=Q(invoice_type=Invoice.SALE)),
            total_tax_returns=Sum('tax_amount', filter=Q(invoice_type=Invoice.RETURN_SALE))
        )
        
        return aggregated

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['start_date'] = self.request.GET.get('start_date', '')
        context['end_date'] = self.request.GET.get('end_date', '')
        context['search_query'] = self.request.GET.get('search_query', '')
        
        from decimal import Decimal
        for item in context['users_data']:
            ts = item['total_sales'] or Decimal('0')
            tr = item['total_returns'] or Decimal('0')
            item['net_sales'] = ts - tr
            item['net_tax'] = (item['total_tax_sales'] or Decimal('0')) - (item['total_tax_returns'] or Decimal('0'))
            
        # Grand totals
        qs = self.get_queryset()
        if qs:
            gt_sales = sum((x['total_sales'] or 0) for x in qs)
            gt_returns = sum((x['total_returns'] or 0) for x in qs)
            context['grand_totals'] = {
                'gt_sales': gt_sales,
                'gt_returns': gt_returns,
                'gt_net': gt_sales - gt_returns
            }
        else:
            context['grand_totals'] = {'gt_sales': 0, 'gt_returns': 0, 'gt_net': 0}
            
        return context

class CustomerDebtsReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'partners.view_partner'
    template_name = 'reports/customer_debts.html'
    context_object_name = 'customers'
    paginate_by = 50
    csv_filename = "customer_debts_report.csv"

    def get_csv_headers(self):
        return ['اسم العميل', 'رقم الهاتف', 'الرقم الضريبي', 'الرصيد المستحق (ديون)']

    def get_csv_row(self, obj):
        return [
            obj.name,
            obj.phone,
            obj.tax_number,
            obj.outstanding_balance
        ]

    def get_queryset(self):
        qs = Partner.objects.filter(partner_type__in=[Partner.CUSTOMER, Partner.BOTH])
        search_query = self.request.GET.get('search_query')
        
        if search_query:
            qs = qs.filter(
                Q(name__icontains=search_query) |
                Q(phone__icontains=search_query)
            )
        return qs.order_by('name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['search_query'] = self.request.GET.get('search_query', '')
        
        # Calculate it for the paginated subset to avoid long load times:
        page_total = sum(c.outstanding_balance for c in context['customers'] if c.outstanding_balance > 0)
        context['page_total_debt'] = page_total
        
        return context

class SalesReturnsReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/sales_returns.html'
    context_object_name = 'returns'
    paginate_by = 50
    csv_filename = "sales_returns_report.csv"

    def get_csv_headers(self):
        return ['تاريخ المرتجع', 'رقم المرتجع', 'العميل', 'الكاشير', 'قيمة المرتجع', 'السبب / الملاحظات']

    def get_csv_row(self, obj):
        return [
            obj.date.strftime('%Y-%m-%d %H:%M') if hasattr(obj, 'date') and obj.date else '',
            obj.invoice_number,
            obj.partner.name if obj.partner else 'عميل نقدي/طياري',
            obj.cashier.first_name or obj.cashier.username if obj.cashier else '',
            obj.total_amount,
            obj.notes
        ]

    def get_queryset(self):
        qs = Invoice.objects.filter(
            invoice_type=Invoice.RETURN_SALE,
            status=Invoice.POSTED
        ).select_related('partner', 'cashier').order_by('-date', '-created_at')
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(invoice_number__icontains=search_query) |
                Q(partner__name__icontains=search_query) |
                Q(cashier__first_name__icontains=search_query) |
                Q(cashier__username__icontains=search_query) |
                Q(notes__icontains=search_query)
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['start_date'] = self.request.GET.get('start_date', '')
        context['end_date'] = self.request.GET.get('end_date', '')
        context['search_query'] = self.request.GET.get('search_query', '')
        
        # Calculate totals for the filtered queryset
        qs = self.get_queryset()
        totals = qs.aggregate(
            total_amount=Sum('total_amount'),
            total_tax=Sum('tax_amount'),
        )
        context['grand_totals'] = {
            'total_amount': totals['total_amount'] or 0,
            'total_tax': totals['total_tax'] or 0,
            'count': qs.count()
        }
        
        return context



# =========================================================
# PURCHASE REPORTS
# =========================================================


# ---------------------------------------------------------
# Product Purchases Report (مشتريات الأصناف)
# ---------------------------------------------------------
class ProductPurchasesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/purchases_by_product.html'
    context_object_name = 'products_data'
    paginate_by = 20
    csv_filename = "product_purchases_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'الكمية المشتراة (شراء)', 'الكمية المسترجعة (مرتجع)', 'صافي الكمية المشتراة', 'إجمالي التكلفة']

    def get_csv_row(self, obj):
        return [
            obj['product__barcode'],
            obj['product__name'],
            obj['purchased_qty'],
            obj['returned_qty'],
            obj['net_qty'],
            obj['net_total']
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.PURCHASE,
            invoice__status=Invoice.POSTED
        )
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(invoice__date__gte=start_date)
        if end_date:
            qs = qs.filter(invoice__date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__barcode__icontains=search_query)
            )
            
        aggregated = qs.values(
            'product__id', 
            'product__name', 
            'product__barcode'
        ).annotate(
            purchased_qty=Sum('quantity'),
            purchased_total=Sum('total_amount'),
            
            returned_qty=Sum(
                'quantity', 
                filter=Q(product__invoice_lines__invoice__invoice_type=Invoice.RETURN_PURCHASE, 
                         product__invoice_lines__invoice__status=Invoice.POSTED,
                         product__invoice_lines__invoice__date__gte=start_date if start_date else '2000-01-01',
                         product__invoice_lines__invoice__date__lte=end_date if end_date else '2100-01-01')
            ),
            returned_total=Sum(
                'total_amount', 
                filter=Q(product__invoice_lines__invoice__invoice_type=Invoice.RETURN_PURCHASE, 
                         product__invoice_lines__invoice__status=Invoice.POSTED,
                         product__invoice_lines__invoice__date__gte=start_date if start_date else '2000-01-01',
                         product__invoice_lines__invoice__date__lte=end_date if end_date else '2100-01-01')
            )
        )
        
        final_data = []
        for item in aggregated:
            p_qty = item['purchased_qty'] or 0
            p_tot = item['purchased_total'] or Decimal('0.00')
            r_qty = item['returned_qty'] or 0
            r_tot = item['returned_total'] or Decimal('0.00')
            
            net_qty = p_qty - r_qty
            net_total = p_tot - r_tot
            
            final_data.append({
                'product__barcode': item['product__barcode'],
                'product__name': item['product__name'],
                'purchased_qty': p_qty,
                'returned_qty': r_qty,
                'net_qty': net_qty,
                'net_total': net_total,
            })
            
        return sorted(final_data, key=lambda x: x['net_total'], reverse=True)


# ---------------------------------------------------------
# Detailed Purchases Report (تقرير المشتريات التفصيلي)
# ---------------------------------------------------------
class DetailedPurchasesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/purchases_detailed.html'
    context_object_name = 'invoice_lines'
    paginate_by = 50
    csv_filename = "detailed_purchases_report.csv"

    def get_csv_headers(self):
        return ['التاريخ', 'رقم الفاتورة', 'نوع الحركة', 'المورد', 'الصنف', 'الكمية', 'سعر الشراء', 'الإجمالي']

    def get_csv_row(self, obj):
        return [
            obj.invoice.date.strftime('%Y-%m-%d') if hasattr(obj.invoice, 'date') and obj.invoice.date else '',
            obj.invoice.invoice_number,
            'مشتريات' if obj.invoice.invoice_type == Invoice.PURCHASE else 'مرتجع',
            obj.invoice.partner.name if obj.invoice.partner else 'مورد نقدي',
            obj.product.name if obj.product else '',
            obj.quantity,
            obj.unit_price,
            obj.total_amount
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type__in=[Invoice.PURCHASE, Invoice.RETURN_PURCHASE],
            invoice__status=Invoice.POSTED
        ).select_related('invoice', 'invoice__partner', 'product')
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(invoice__date__gte=start_date)
        if end_date:
            qs = qs.filter(invoice__date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(invoice__invoice_number__icontains=search_query) |
                Q(product__name__icontains=search_query) |
                Q(invoice__partner__name__icontains=search_query)
            )
            
        return qs.order_by('-invoice__date', '-invoice__id')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = self.get_queryset()
        
        purchases = qs.filter(invoice__invoice_type=Invoice.PURCHASE)
        returns = qs.filter(invoice__invoice_type=Invoice.RETURN_PURCHASE)
        
        grand_totals = {
            'total_purchases': sum(line.total_amount for line in purchases) or Decimal('0.00'),
            'total_returns': sum(line.total_amount for line in returns) or Decimal('0.00'),
        }
        grand_totals['net_purchases'] = grand_totals['total_purchases'] - grand_totals['total_returns']
        context['grand_totals'] = grand_totals
        
        return context

# ---------------------------------------------------------
# Period Purchases Report (مشتريات فترة)
# ---------------------------------------------------------
class PeriodPurchasesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/purchases_by_period.html'
    context_object_name = 'periods_data'
    paginate_by = 31
    csv_filename = "period_purchases_report.csv"

    def get_csv_headers(self):
        return ['الفترة', 'إجمالي المشتريات', 'مرتجعات الشراء', 'صافي المشتريات', 'الضريبة']

    def get_csv_row(self, obj):
        return [
            obj['period'].strftime('%Y-%m-%d') if hasattr(obj['period'], 'strftime') else obj['period'],
            obj['total_purchases'],
            obj['total_returns'],
            obj['net_purchases'],
            obj['net_tax']
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.PURCHASE,
            invoice__status=Invoice.POSTED
        )
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        period_type = self.request.GET.get('period_type', 'daily')
        
        if start_date:
            qs = qs.filter(invoice__date__gte=start_date)
        if end_date:
            qs = qs.filter(invoice__date__lte=end_date)
            
        if period_type == 'monthly':
            trunc_func = TruncMonth('invoice__date')
        else:
            trunc_func = TruncDay('invoice__date')
            
        aggregated = qs.annotate(period=trunc_func).values('period').annotate(
            total_purchases=Sum('total_amount'),
            total_tax=Sum(F('tax_amount') * F('quantity'))
        ).order_by('-period')
        
        # Calculate Returns for the same periods
        returns_qs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.RETURN_PURCHASE,
            invoice__status=Invoice.POSTED
        )
        if start_date:
            returns_qs = returns_qs.filter(invoice__date__gte=start_date)
        if end_date:
            returns_qs = returns_qs.filter(invoice__date__lte=end_date)
            
        returns_agg = returns_qs.annotate(period=trunc_func).values('period').annotate(
            total_returns=Sum('total_amount'),
            total_ret_tax=Sum(F('tax_amount') * F('quantity'))
        )
        
        returns_dict = {r['period']: r for r in returns_agg}
        
        final_data = []
        for item in aggregated:
            period = item['period']
            ret = returns_dict.get(period, {})
            
            p_tot = item['total_purchases'] or Decimal('0.00')
            p_tax = item['total_tax'] or Decimal('0.00')
            
            r_tot = ret.get('total_returns', Decimal('0.00'))
            r_tax = ret.get('total_ret_tax', Decimal('0.00'))
            
            final_data.append({
                'period': period,
                'total_purchases': p_tot,
                'total_returns': r_tot,
                'net_purchases': p_tot - r_tot,
                'net_tax': p_tax - r_tax
            })
            
        return final_data

# ---------------------------------------------------------
# Purchase Price Variation Report (تغير أسعار الشراء)
# ---------------------------------------------------------
class PurchasePriceVariationReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/purchase_price_variation.html'
    context_object_name = 'variations'
    paginate_by = 30
    csv_filename = "purchase_price_variation_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'أدنى سعر شراء', 'أعلى سعر شراء', 'متوسط السعر', 'آخر سعر', 'نسبة التغير %']

    def get_csv_row(self, obj):
        return [
            obj['product__barcode'],
            obj['product__name'],
            obj['min_price'],
            obj['max_price'],
            obj['avg_price'],
            obj['last_price'],
            obj['variation_percent']
        ]

    def get_queryset(self):
        from django.db.models import Min, Max, Avg, Subquery, OuterRef
        
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.PURCHASE,
            invoice__status=Invoice.POSTED
        )
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(invoice__date__gte=start_date)
        if end_date:
            qs = qs.filter(invoice__date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__barcode__icontains=search_query)
            )

        # We need the last purchase price per product
        # Subquery to get the unit_price of the most recent purchase line for a product
        last_price_subquery = InvoiceLine.objects.filter(
            product_id=OuterRef('product__id'),
            invoice__invoice_type=Invoice.PURCHASE,
            invoice__status=Invoice.POSTED
        ).order_by('-invoice__date', '-id').values('unit_price')[:1]
            
        aggregated = qs.values(
            'product__id', 
            'product__name', 
            'product__barcode'
        ).annotate(
            min_price=Min('unit_price'),
            max_price=Max('unit_price'),
            avg_price=Avg('unit_price'),
            last_price=Subquery(last_price_subquery)
        )
        
        final_data = []
        for item in aggregated:
            min_p = item['min_price'] or Decimal('0.00')
            max_p = item['max_price'] or Decimal('0.00')
            avg_p = item['avg_price'] or Decimal('0.00')
            last_p = item['last_price'] or Decimal('0.00')
            
            var_percent = 0
            if min_p > 0:
                var_percent = ((max_p - min_p) / min_p) * 100
                
            final_data.append({
                'product__barcode': item['product__barcode'],
                'product__name': item['product__name'],
                'min_price': round(min_p, 2),
                'max_price': round(max_p, 2),
                'avg_price': round(avg_p, 2),
                'last_price': round(last_p, 2),
                'variation_percent': round(var_percent, 2),
            })
            
        # Sort by highest variation first
        return sorted(final_data, key=lambda x: x['variation_percent'], reverse=True)

# ---------------------------------------------------------
# Supplier Balances (كشف حساب مورد)
# ---------------------------------------------------------
class SupplierBalancesReportView(CsvExportMixin, CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'partners.view_partner'
    template_name = 'reports/supplier_balances.html'
    context_object_name = 'transactions'
    csv_filename = "supplier_statement.csv"

    def get_csv_headers(self):
        return ['التاريخ', 'رقم الحركة', 'نوع الحركة', 'مدين (سداد للمورد)', 'دائن (مشتريات من المورد)', 'الرصيد التراكمي']

    def get_csv_row(self, obj):
        return [
            obj['date'].strftime('%Y-%m-%d') if hasattr(obj['date'], 'strftime') else obj['date'],
            obj['reference'],
            obj['type'],
            obj['debit'],
            obj['credit'],
            obj['balance']
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        supplier_id = self.request.GET.get('supplier_id')
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        
        context['suppliers'] = Partner.objects.filter(partner_type__in=[Partner.SUPPLIER, Partner.BOTH]).order_by('name')
        context['transactions'] = []
        context['initial_balance'] = Decimal('0.00')
        context['final_balance'] = Decimal('0.00')
        context['period_debit'] = Decimal('0.00')
        context['period_credit'] = Decimal('0.00')
        
        if supplier_id:
            try:
                supplier = Partner.objects.get(id=supplier_id, partner_type__in=[Partner.SUPPLIER, Partner.BOTH])
                context['selected_supplier'] = supplier
                
                # Fetch transactions (Invoices and Vouchers)
                # For a supplier: Vouchers (Payments to them) are Debit (reducing their balance).
                # Purchases from them are Credit (increasing their balance).
                # Returns to them are Debit (reducing their balance).
                
                from apps.tenant.accounting.models import Voucher
                
                transactions = []
                
                # Purchases (Credit)
                purchases = Invoice.objects.filter(
                    partner=supplier,
                    invoice_type=Invoice.PURCHASE,
                    status=Invoice.POSTED
                )
                
                # Purchase Returns (Debit)
                returns = Invoice.objects.filter(
                    partner=supplier,
                    invoice_type=Invoice.RETURN_PURCHASE,
                    status=Invoice.POSTED
                )
                
                # Vouchers (Debit)
                vouchers = Voucher.objects.filter(
                    partner=supplier,
                    voucher_type=Voucher.PAYMENT_VOUCHER,
                    status=Voucher.POSTED
                )
                
                for p in purchases:
                    transactions.append({
                        'date': p.date,
                        'reference': p.invoice_number,
                        'type': 'فاتورة مشتريات',
                        'debit': Decimal('0.00'),
                        'credit': p.total_amount,
                    })
                    
                for r in returns:
                    transactions.append({
                        'date': r.date,
                        'reference': r.invoice_number,
                        'type': 'مرتجع مشتريات',
                        'debit': r.total_amount,
                        'credit': Decimal('0.00'),
                    })
                    
                for v in vouchers:
                    transactions.append({
                        'date': v.date,
                        'reference': v.voucher_number,
                        'type': 'سند صرف',
                        'debit': v.amount,
                        'credit': Decimal('0.00'),
                    })
                    
                transactions.sort(key=lambda x: x['date'])
                
                filtered_transactions = []
                initial_balance = Decimal('0.00')
                running_balance = Decimal('0.00')
                
                for t in transactions:
                    date_str = t['date'].strftime('%Y-%m-%d') if hasattr(t['date'], 'strftime') else str(t['date'])
                    
                    if start_date and date_str < start_date:
                        initial_balance -= t['debit']
                        initial_balance += t['credit']
                        running_balance = initial_balance
                    else:
                        if end_date and date_str > end_date:
                            continue
                        
                        running_balance -= t['debit']
                        running_balance += t['credit']
                        
                        t_copy = t.copy()
                        t_copy['balance'] = running_balance
                        filtered_transactions.append(t_copy)
                        
                context['transactions'] = filtered_transactions
                context['initial_balance'] = initial_balance
                context['final_balance'] = running_balance
                context['period_debit'] = sum(t['debit'] for t in filtered_transactions)
                context['period_credit'] = sum(t['credit'] for t in filtered_transactions)
            except Partner.DoesNotExist:
                pass
        return context

# ---------------------------------------------------------
# Supplier Debts Report (مستحقات الموردين الإجمالي)
# ---------------------------------------------------------
class SupplierDebtsReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'partners.view_partner'
    template_name = 'reports/supplier_debts.html'
    context_object_name = 'suppliers'
    paginate_by = 50
    csv_filename = "supplier_debts_report.csv"

    def get_csv_headers(self):
        return ['اسم المورد', 'رقم الهاتف', 'الرقم الضريبي', 'الرصيد المستحق (مطلوبات)']

    def get_csv_row(self, obj):
        return [
            obj.name,
            obj.phone,
            obj.tax_number,
            obj.outstanding_balance
        ]

    def get_queryset(self):
        qs = Partner.objects.filter(partner_type__in=[Partner.SUPPLIER, Partner.BOTH])
        
        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(
                Q(name__icontains=search_query) |
                Q(phone__icontains=search_query) |
                Q(mobile__icontains=search_query)
            )
            
        # We'll filter out those with exactly 0 balance in python or just show them.
        # Usually it's better to show only those who have debts.
        return qs.order_by('name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        suppliers = context[self.context_object_name]
        
        # Calculate total debt
        total_debts = sum(s.outstanding_balance for s in suppliers if s.outstanding_balance > 0)
        context['total_debts'] = total_debts
            
        return context

# ---------------------------------------------------------
# User Purchases Report (مشتريات المستخدمين)
# ---------------------------------------------------------
class UserPurchasesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/purchases_by_user.html'
    context_object_name = 'users_data'
    paginate_by = 20
    csv_filename = "purchases_by_user_report.csv"

    def get_csv_headers(self):
        return ['اسم المستخدم', 'يوزر نيم', 'عدد فواتير الشراء', 'قيمة المشتريات', 'عدد المرتجعات', 'قيمة المرتجعات', 'صافي المشتريات']

    def get_csv_row(self, obj):
        name = f"{obj['cashier__first_name']} {obj['cashier__last_name']}".strip()
        if not name:
            name = obj['cashier__username']
        return [
            name,
            obj['cashier__username'],
            obj['purchases_count'],
            obj['total_purchases'],
            obj['returns_count'],
            obj['total_returns'],
            obj['net_purchases']
        ]

    def get_queryset(self):
        qs = Invoice.objects.filter(
            invoice_type__in=[Invoice.PURCHASE, Invoice.RETURN_PURCHASE],
            status=Invoice.POSTED
        )
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
            
        aggregated = qs.values('cashier__username', 'cashier__first_name', 'cashier__last_name').annotate(
            purchases_count=Count('id', filter=Q(invoice_type=Invoice.PURCHASE)),
            total_purchases=Sum('total_amount', filter=Q(invoice_type=Invoice.PURCHASE)),
            
            returns_count=Count('id', filter=Q(invoice_type=Invoice.RETURN_PURCHASE)),
            total_returns=Sum('total_amount', filter=Q(invoice_type=Invoice.RETURN_PURCHASE)),
        )
        
        final_data = []
        for item in aggregated:
            p_count = item['purchases_count'] or 0
            p_tot = item['total_purchases'] or Decimal('0.00')
            r_count = item['returns_count'] or 0
            r_tot = item['total_returns'] or Decimal('0.00')
            
            final_data.append({
                'cashier__username': item['cashier__username'],
                'cashier__first_name': item['cashier__first_name'],
                'cashier__last_name': item['cashier__last_name'],
                'purchases_count': p_count,
                'total_purchases': p_tot,
                'returns_count': r_count,
                'total_returns': r_tot,
                'net_purchases': p_tot - r_tot,
            })
            
        return sorted(final_data, key=lambda x: x['net_purchases'], reverse=True)

# ---------------------------------------------------------
# Purchase Returns Report (مرتجعات المشتريات)
# ---------------------------------------------------------
class PurchaseReturnsReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'invoicing.view_invoice'
    template_name = 'reports/purchase_returns.html'
    context_object_name = 'returns'
    paginate_by = 50
    csv_filename = "purchase_returns_report.csv"

    def get_csv_headers(self):
        return ['تاريخ المرتجع', 'رقم المرتجع', 'المورد', 'المستخدم', 'قيمة المرتجع', 'السبب / الملاحظات']

    def get_csv_row(self, obj):
        return [
            obj.date.strftime('%Y-%m-%d %H:%M') if hasattr(obj, 'date') and obj.date else '',
            obj.invoice_number,
            obj.partner.name if obj.partner else 'مورد نقدي',
            obj.cashier.first_name or obj.cashier.username if obj.cashier else '',
            obj.total_amount,
            obj.notes
        ]

    def get_queryset(self):
        qs = Invoice.objects.filter(
            invoice_type=Invoice.RETURN_PURCHASE,
            status=Invoice.POSTED
        ).select_related('partner', 'cashier')
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
            
        if search_query:
            qs = qs.filter(
                Q(invoice_number__icontains=search_query) |
                Q(partner__name__icontains=search_query) |
                Q(cashier__username__icontains=search_query) |
                Q(notes__icontains=search_query)
            )
            
        return qs.order_by('-date', '-id')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = self.get_queryset()
        
        context['total_returns_amount'] = qs.aggregate(t=Sum('total_amount'))['t'] or Decimal('0.00')
        context['total_returns_count'] = qs.count()
        
        return context

# ---------------------------------------------------------
# Inventory Reports Section
# ---------------------------------------------------------
from apps.tenant.inventory.models import Product, InventoryBatch
from django.utils import timezone
from datetime import timedelta

class LowStockReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'inventory.view_product'
    template_name = 'reports/inventory_low_stock.html'
    context_object_name = 'products'
    paginate_by = 50
    csv_filename = "low_stock_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'الرصيد الحالي', 'حد الطلب', 'النقص']

    def get_csv_row(self, obj):
        return [
            obj.barcode or '-',
            obj.name,
            obj.current_stock,
            obj.min_stock_level,
            obj.min_stock_level - obj.current_stock if obj.current_stock < obj.min_stock_level else 0
        ]

    def get_queryset(self):
        # We need to calculate current stock for each product.
        # Since it's a method on Product, we can annotate it using a Subquery,
        # or we can fetch all active products with min_stock > 0 and calculate in python.
        # Using annotation for better DB performance.
        from django.db.models import OuterRef, Subquery, Value, DecimalField
        from django.db.models.functions import Coalesce

        stock_subquery = InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ).values('product_id').annotate(
            total=Sum('quantity_remaining')
        ).values('total')

        qs = Product.objects.filter(is_active=True, min_stock_level__gt=0).annotate(
            current_stock=Coalesce(Subquery(stock_subquery), Value(0), output_field=DecimalField())
        ).filter(current_stock__lte=F('min_stock_level')).order_by('current_stock')

        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(Q(name__icontains=search_query) | Q(barcode__icontains=search_query))
        
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير نواقص المخزون'
        context['search_query'] = self.request.GET.get('search_query', '')
        return context

class ProductExpiryReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'inventory.view_inventorybatch'
    template_name = 'reports/inventory_expiry.html'
    context_object_name = 'batches'
    paginate_by = 50
    csv_filename = "product_expiry_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'تاريخ الصلاحية', 'المخزن', 'الكمية المتبقية', 'الحالة']

    def get_csv_row(self, obj):
        today = timezone.now().date()
        status = "منتهي" if obj.expiry_date and obj.expiry_date <= today else "قارب على الانتهاء"
        return [
            obj.product.barcode or '-',
            obj.product.name,
            obj.expiry_date.strftime('%Y-%m-%d') if obj.expiry_date else '-',
            obj.warehouse.name,
            obj.quantity_remaining,
            status
        ]

    def get_queryset(self):
        # Items with expiry date that are not exhausted
        qs = InventoryBatch.objects.filter(
            quantity_remaining__gt=0,
            expiry_date__isnull=False
        ).select_related('product', 'warehouse')

        # Filter by days to expire
        days = self.request.GET.get('days', '30')
        try:
            days_int = int(days)
        except ValueError:
            days_int = 30
            
        target_date = timezone.now().date() + timedelta(days=days_int)
        qs = qs.filter(expiry_date__lte=target_date).order_by('expiry_date')

        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(Q(product__name__icontains=search_query) | Q(product__barcode__icontains=search_query))

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير صلاحيات المنتجات'
        context['search_query'] = self.request.GET.get('search_query', '')
        context['days'] = self.request.GET.get('days', '30')
        context['today'] = timezone.now().date()
        return context

class InventoryValuationReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'inventory.view_product'
    template_name = 'reports/inventory_valuation.html'
    context_object_name = 'products'
    paginate_by = 50
    csv_filename = "inventory_valuation_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'الرصيد الفعلي', 'متوسط التكلفة', 'القيمة الإجمالية']

    def get_csv_row(self, obj):
        return [
            obj.barcode or '-',
            obj.name,
            obj.current_stock,
            obj.avg_cost,
            obj.total_value
        ]

    def get_queryset(self):
        # We need actual stock and actual cost.
        from django.db.models import OuterRef, Subquery, Value, DecimalField, F, ExpressionWrapper
        from django.db.models.functions import Coalesce

        # 1. Total stock
        stock_subquery = InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ).values('product_id').annotate(
            total=Sum('quantity_remaining')
        ).values('total')

        # 2. Total cost value (sum of quantity_remaining * unit_cost)
        # Using ExpressionWrapper to calculate the sum properly
        cost_subquery = InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ).values('product_id').annotate(
            total_cost_val=Sum(ExpressionWrapper(F('quantity_remaining') * F('unit_cost'), output_field=DecimalField()))
        ).values('total_cost_val')

        qs = Product.objects.filter(is_active=True).annotate(
            current_stock=Coalesce(Subquery(stock_subquery), Value(0), output_field=DecimalField()),
            total_val=Coalesce(Subquery(cost_subquery), Value(0), output_field=DecimalField())
        ).filter(current_stock__gt=0).order_by('-total_val')

        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(Q(name__icontains=search_query) | Q(barcode__icontains=search_query))
            
        # Add python attributes for the template
        for product in qs:
            product.total_value = product.total_val
            product.avg_cost = round(product.total_val / product.current_stock, 4) if product.current_stock > 0 else 0

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير جرد المخزون الفعلي (التقييم)'
        context['search_query'] = self.request.GET.get('search_query', '')
        
        # Calculate grand totals
        qs = self.get_queryset()
        total_inventory_qty = sum(p.current_stock for p in qs)
        total_inventory_value = sum(p.total_value for p in qs)
        
        context['grand_totals'] = {
            'total_qty': total_inventory_qty,
            'total_value': total_inventory_value
        }
        return context

# ---------------------------------------------------------
# Additional Inventory Reports
# ---------------------------------------------------------
from apps.tenant.inventory.models import StockMovement

class WarehouseStockReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'inventory.view_inventorybatch'
    template_name = 'reports/inventory_warehouse_stock.html'
    context_object_name = 'stock_data'
    paginate_by = 50
    csv_filename = "warehouse_stock_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'المستودع', 'الكمية المتاحة', 'القيمة']

    def get_csv_row(self, obj):
        return [
            obj['product__barcode'] or '-',
            obj['product__name'],
            obj['warehouse__name'],
            obj['total_qty'],
            obj['total_value']
        ]

    def get_queryset(self):
        from django.db.models import Sum, F, DecimalField, ExpressionWrapper
        qs = InventoryBatch.objects.filter(quantity_remaining__gt=0)
        
        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(Q(product__name__icontains=search_query) | Q(product__barcode__icontains=search_query))
            
        warehouse_id = self.request.GET.get('warehouse')
        if warehouse_id:
            qs = qs.filter(warehouse_id=warehouse_id)
            
        qs = qs.values(
            'product__id', 'product__name', 'product__barcode', 'warehouse__name'
        ).annotate(
            total_qty=Sum('quantity_remaining'),
            total_value=Sum(ExpressionWrapper(F('quantity_remaining') * F('unit_cost'), output_field=DecimalField()))
        ).order_by('product__name', 'warehouse__name')
        
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'أرصدة الأصناف في المستودعات'
        context['search_query'] = self.request.GET.get('search_query', '')
        
        from apps.tenant.inventory.models import Warehouse
        context['warehouses'] = Warehouse.objects.filter(is_active=True)
        context['selected_warehouse'] = self.request.GET.get('warehouse', '')
        
        return context

class ItemMovementReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'inventory.view_stockmovement'
    template_name = 'reports/inventory_movement.html'
    context_object_name = 'movements'
    paginate_by = 50
    csv_filename = "item_movement_report.csv"

    def get_csv_headers(self):
        return ['التاريخ', 'اسم الصنف', 'المستودع', 'نوع الحركة', 'الكمية', 'التكلفة', 'الرصيد بعد الحركة', 'المرجع', 'ملاحظات']

    def get_csv_row(self, obj):
        from django.utils.timezone import localtime
        date_str = localtime(obj.created_at).strftime("%Y-%m-%d %H:%M") if obj.created_at else '-'
        return [
            date_str,
            obj.product.name,
            obj.warehouse.name,
            obj.get_movement_type_display(),
            obj.quantity,
            obj.unit_cost,
            getattr(obj, 'running_balance', '-'),
            obj.reference or '-',
            obj.notes or '-'
        ]

    def get_queryset(self):
        from apps.tenant.inventory.models import StockMovement
        from django.db.models import Q
        from datetime import datetime, timedelta
        
        qs = StockMovement.objects.all().select_related('product', 'warehouse')
        
        # Date filters
        date_from = self.request.GET.get('date_from')
        date_to = self.request.GET.get('date_to')
        
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            try:
                date_to_obj = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                qs = qs.filter(created_at__lt=date_to_obj)
            except ValueError:
                pass
                
        product_id = self.request.GET.get('product_id')
        if product_id:
            qs = qs.filter(product_id=product_id)
            
        movement_type = self.request.GET.get('movement_type')
        if movement_type:
            qs = qs.filter(movement_type=movement_type)
            
        warehouse_id = self.request.GET.get('warehouse_id')
        if warehouse_id:
            qs = qs.filter(warehouse_id=warehouse_id)
            
        return qs.order_by('created_at', 'id')

    def get_context_data(self, **kwargs):
        from apps.tenant.inventory.models import StockMovement, Product, Warehouse
        from django.db.models import Sum, Q
        from decimal import Decimal
        
        context = super().get_context_data(**kwargs)
        context['title'] = 'حركة الأصناف (كارت الصنف)'
        
        product_id = self.request.GET.get('product_id')
        warehouse_id = self.request.GET.get('warehouse_id')
        date_from = self.request.GET.get('date_from')
        context['date_from'] = date_from or ''
        context['date_to'] = self.request.GET.get('date_to', '')
        context['selected_product'] = product_id or ''
        context['selected_warehouse'] = warehouse_id or ''
        context['selected_type'] = self.request.GET.get('movement_type', '')
        
        context['products'] = Product.objects.all()
        context['warehouses'] = Warehouse.objects.all()
        context['movement_types'] = StockMovement.MOVEMENT_TYPES
        
        opening_balance = Decimal('0')
        closing_balance = Decimal('0')
        
        if product_id:
            product_id = int(product_id)
            prev_qs = StockMovement.objects.filter(product_id=product_id)
            if warehouse_id:
                prev_qs = prev_qs.filter(warehouse_id=warehouse_id)
            if date_from:
                prev_qs = prev_qs.filter(created_at__lt=date_from)
            else:
                prev_qs = prev_qs.none()
                
            in_types = ['IN', 'TRANSFER_IN', 'ADJUSTMENT_IN', 'OPENING_BALANCE', 'RETURN_IN']
            out_types = ['OUT', 'TRANSFER_OUT', 'ADJUSTMENT_OUT', 'RETURN_OUT']
            
            in_qty = prev_qs.filter(movement_type__in=in_types).aggregate(Sum('quantity'))['quantity__sum'] or Decimal('0')
            out_qty = prev_qs.filter(movement_type__in=out_types).aggregate(Sum('quantity'))['quantity__sum'] or Decimal('0')
            
            opening_balance = in_qty - out_qty
            
            object_list = context['object_list']
            current_balance = opening_balance
            
            if object_list:
                first_item = object_list[0]
                qs_before = self.get_queryset().filter(
                    Q(created_at__lt=first_item.created_at) | 
                    Q(created_at=first_item.created_at, id__lt=first_item.id)
                )
                in_before = qs_before.filter(movement_type__in=in_types).aggregate(Sum('quantity'))['quantity__sum'] or Decimal('0')
                out_before = qs_before.filter(movement_type__in=out_types).aggregate(Sum('quantity'))['quantity__sum'] or Decimal('0')
                
                current_balance = opening_balance + in_before - out_before
                
                for obj in object_list:
                    if obj.movement_type in in_types:
                        current_balance += obj.quantity
                    else:
                        current_balance -= obj.quantity
                    obj.running_balance = current_balance
            
            all_in = self.get_queryset().filter(movement_type__in=in_types).aggregate(Sum('quantity'))['quantity__sum'] or Decimal('0')
            all_out = self.get_queryset().filter(movement_type__in=out_types).aggregate(Sum('quantity'))['quantity__sum'] or Decimal('0')
            closing_balance = opening_balance + all_in - all_out
            
            context['show_balances'] = True
            context['opening_balance'] = opening_balance
            context['closing_balance'] = closing_balance
        else:
            context['show_balances'] = False
            
        return context



class SlowMovingReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'inventory.view_product'
    template_name = 'reports/inventory_slow_moving.html'
    context_object_name = 'products'
    paginate_by = 50
    csv_filename = "slow_moving_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'الرصيد الحالي', 'تاريخ آخر بيع', 'أيام الركود']

    def get_csv_row(self, obj):
        from django.utils import timezone
        if obj.last_sale:
            days = (timezone.now().date() - obj.last_sale.date()).days
            last_sale_str = obj.last_sale.strftime('%Y-%m-%d')
        else:
            days = 'لم يُباع أبداً'
            last_sale_str = '-'
            
        return [
            obj.barcode or '-',
            obj.name,
            obj.current_stock,
            last_sale_str,
            days
        ]

    def get_queryset(self):
        from apps.tenant.inventory.models import Product, InventoryBatch, StockMovement
        from django.db.models import OuterRef, Subquery, Value, DecimalField, Max, Q, F
        from django.db.models.functions import Coalesce
        from django.utils import timezone
        import datetime

        days_inactive = int(self.request.GET.get('days', 30))
        threshold_date = timezone.now().date() - datetime.timedelta(days=days_inactive)

        # Current stock subquery
        stock_subquery = InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ).values('product_id').annotate(
            total=Sum('quantity_remaining')
        ).values('total')

        # Last sale subquery
        last_sale_subquery = StockMovement.objects.filter(
            product_id=OuterRef('pk'),
            movement_type='OUT'  # OUT is typically a sale
        ).values('product_id').annotate(
            latest=Max('created_at')
        ).values('latest')

        qs = Product.objects.filter(is_active=True).annotate(
            current_stock=Coalesce(Subquery(stock_subquery), Value(0), output_field=DecimalField()),
            last_sale=Subquery(last_sale_subquery)
        ).filter(current_stock__gt=0)
        
        # Filter by inactivity
        qs = qs.filter(Q(last_sale__date__lte=threshold_date) | Q(last_sale__isnull=True))

        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(Q(name__icontains=search_query) | Q(barcode__icontains=search_query))
        
        # Order by days inactive (oldest or null first)
        return qs.order_by(F('last_sale').asc(nulls_first=True))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير الأصناف الراكدة'
        context['search_query'] = self.request.GET.get('search_query', '')
        context['days'] = self.request.GET.get('days', '30')
        
        from django.utils import timezone
        today = timezone.now().date()
        for p in context['products']:
            if p.last_sale:
                p.days_inactive = (today - p.last_sale.date()).days
            else:
                p.days_inactive = 'لم يُباع أبداً'
                
        return context



from django.views.generic import TemplateView

class IncomeStatementReportView(CustomPermissionRequiredMixin, TemplateView):
    # Depending on your permissions, you can use something appropriate
    permission_required = 'accounting.view_expense'
    template_name = 'reports/accounting_income_statement.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.invoicing.models import Invoice, InvoiceLine
        from apps.tenant.accounting.models import Expense
        from django.db.models import Sum
        from django.utils import timezone
        import datetime

        date_from = self.request.GET.get('date_from')
        date_to = self.request.GET.get('date_to')
        
        today = timezone.now().date()
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            
        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()

        # 1. Sales Revenue
        sales_invoices = Invoice.objects.filter(
            invoice_type=Invoice.SALE,
            status=Invoice.POSTED,
            date__range=[date_from_obj, date_to_obj]
        )
        sales_data = sales_invoices.aggregate(
            total_subtotal=Sum('subtotal'),
            total_discount=Sum('discount_amount')
        )
        gross_sales = sales_data['total_subtotal'] or 0
        sales_discount = sales_data['total_discount'] or 0
        net_sales = gross_sales - sales_discount

        # 2. Sales Returns
        return_invoices = Invoice.objects.filter(
            invoice_type=Invoice.RETURN_SALE,
            status=Invoice.POSTED,
            date__range=[date_from_obj, date_to_obj]
        )
        returns_data = return_invoices.aggregate(
            total_subtotal=Sum('subtotal'),
            total_discount=Sum('discount_amount')
        )
        gross_returns = returns_data['total_subtotal'] or 0
        returns_discount = returns_data['total_discount'] or 0
        net_returns = gross_returns - returns_discount

        # Actual Net Revenue
        net_revenue = net_sales - net_returns

        # 3. Cost of Goods Sold (COGS)
        sales_cogs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.SALE,
            invoice__status=Invoice.POSTED,
            invoice__date__range=[date_from_obj, date_to_obj]
        ).aggregate(total_cogs=Sum('cogs_amount'))['total_cogs'] or 0
        
        returns_cogs = InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.RETURN_SALE,
            invoice__status=Invoice.POSTED,
            invoice__date__range=[date_from_obj, date_to_obj]
        ).aggregate(total_cogs=Sum('cogs_amount'))['total_cogs'] or 0
        
        net_cogs = sales_cogs - returns_cogs

        # 4. Gross Profit
        gross_profit = net_revenue - net_cogs

        # 5. Operating Expenses
        expenses_qs = Expense.objects.filter(
            status=Expense.POSTED,
            date__range=[date_from_obj, date_to_obj]
        )
        total_expenses = expenses_qs.aggregate(total=Sum('amount'))['total'] or 0
        
        # Group expenses by account
        expenses_by_account = expenses_qs.values(
            'expense_account__name', 'expense_account__code'
        ).annotate(
            total_amount=Sum('amount')
        ).order_by('-total_amount')

        # 6. Net Profit
        net_profit = gross_profit - total_expenses

        context.update({
            'date_from': date_from,
            'date_to': date_to,
            'gross_sales': gross_sales,
            'sales_discount': sales_discount,
            'net_sales': net_sales,
            'net_returns': net_returns,
            'net_revenue': net_revenue,
            'net_cogs': net_cogs,
            'gross_profit': gross_profit,
            'total_expenses': total_expenses,
            'expenses_by_account': expenses_by_account,
            'net_profit': net_profit,
            'title': 'قائمة الدخل (الأرباح والخسائر)'
        })
        return context



class ExpenseReportView(CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'accounting.view_expense'
    template_name = 'reports/accounting_expenses.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import Expense
        from django.db.models import Sum
        from django.utils import timezone
        import datetime

        date_from = self.request.GET.get('date_from')
        date_to = self.request.GET.get('date_to')
        
        today = timezone.now().date()
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            
        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()

        # Query all posted expenses in the date range
        expenses_qs = Expense.objects.filter(
            status=Expense.POSTED,
            date__range=[date_from_obj, date_to_obj]
        ).select_related('expense_account', 'payment_account', 'branch')

        # Total Expense
        total_expenses = expenses_qs.aggregate(total=Sum('amount'))['total'] or 0

        # Group by Expense Account
        expenses_by_account = expenses_qs.values(
            'expense_account__name', 'expense_account__code'
        ).annotate(
            total_amount=Sum('amount')
        ).order_by('-total_amount')

        context.update({
            'date_from': date_from,
            'date_to': date_to,
            'expenses': expenses_qs.order_by('-date', '-id'),
            'total_expenses': total_expenses,
            'expenses_by_account': expenses_by_account,
            'title': 'المصروفات التفصيلية'
        })
        return context



class TreasuryStatementReportView(CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'accounting.view_journalentry'
    template_name = 'reports/accounting_treasury.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import Treasury, JournalItem, JournalEntry
        from django.db.models import Sum, F
        from django.utils import timezone
        import datetime
        from decimal import Decimal

        treasuries = Treasury.objects.all()
        selected_treasury_id = self.request.GET.get('treasury')
        
        date_from = self.request.GET.get('date_from')
        date_to = self.request.GET.get('date_to')
        
        today = timezone.now().date()
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            
        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()

        selected_treasury = None
        statement_lines = []
        starting_balance = Decimal('0')
        total_in = Decimal('0')
        total_out = Decimal('0')
        ending_balance = Decimal('0')

        if selected_treasury_id:
            try:
                selected_treasury = Treasury.objects.get(id=selected_treasury_id)
                account = selected_treasury.account
                
                if account:
                    # 1. Calculate Starting Balance
                    # Base opening balance of the treasury
                    starting_balance = selected_treasury.opening_balance
                    
                    # Add/Subtract previous posted journal items (before date_from)
                    prev_items = JournalItem.objects.filter(
                        account=account,
                        entry__status=JournalEntry.POSTED,
                        entry__date__lt=date_from_obj
                    ).aggregate(
                        total_debit=Sum('debit'),
                        total_credit=Sum('credit')
                    )
                    
                    prev_debit = prev_items['total_debit'] or Decimal('0')
                    prev_credit = prev_items['total_credit'] or Decimal('0')
                    
                    # Asset account: Balance = Debit - Credit
                    starting_balance += (prev_debit - prev_credit)
                    
                    # 2. Get period transactions
                    period_items = JournalItem.objects.filter(
                        account=account,
                        entry__status=JournalEntry.POSTED,
                        entry__date__range=[date_from_obj, date_to_obj]
                    ).select_related('entry').order_by('entry__date', 'entry__id')
                    
                    current_balance = starting_balance
                    
                    for item in period_items:
                        debit = item.debit or Decimal('0')
                        credit = item.credit or Decimal('0')
                        
                        total_in += debit
                        total_out += credit
                        current_balance += (debit - credit)
                        
                        statement_lines.append({
                            'date': item.entry.date,
                            'reference': item.entry.reference,
                            'description': item.entry.description,
                            'debit': debit,
                            'credit': credit,
                            'balance': current_balance
                        })
                        
                    ending_balance = current_balance
            except Treasury.DoesNotExist:
                pass

        context.update({
            'treasuries': treasuries,
            'selected_treasury': selected_treasury,
            'date_from': date_from,
            'date_to': date_to,
            'starting_balance': starting_balance,
            'total_in': total_in,
            'total_out': total_out,
            'ending_balance': ending_balance,
            'statement_lines': statement_lines,
            'title': 'كشف حساب خزينة / بنك'
        })
        return context



class ShiftReportView(CustomPermissionRequiredMixin, TemplateView):
    # Depending on your permissions, you can use something appropriate
    permission_required = 'invoicing.view_possession'
    template_name = 'reports/sales_shifts.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.invoicing.models import POSSession, Invoice
        from django.contrib.auth import get_user_model
        from django.db.models import Sum, Q, Count
        from django.utils import timezone
        import datetime
        from decimal import Decimal

        User = get_user_model()
        users = User.objects.all()

        date_from = self.request.GET.get('date_from')
        date_to = self.request.GET.get('date_to')
        selected_user_id = self.request.GET.get('user')
        
        today = timezone.now().date()
        if not date_from:
            date_from_obj = today
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            
        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()

        # Query sessions
        sessions_qs = POSSession.objects.filter(
            start_time__date__range=[date_from_obj, date_to_obj]
        ).select_related('user', 'branch', 'treasury').order_by('-start_time')

        if selected_user_id:
            sessions_qs = sessions_qs.filter(user_id=selected_user_id)

        # Annotate with sales and returns
        # Actually, using Django annotation for this might be tricky due to join duplicates if there are multiple lines.
        # But we can annotate Invoices simply.
        sessions_data = []
        
        total_expected = Decimal('0')
        total_actual = Decimal('0')
        total_diff = Decimal('0')

        for session in sessions_qs:
            # Get sales
            sales_aggr = Invoice.objects.filter(
                pos_session=session,
                invoice_type=Invoice.SALE,
                status=Invoice.POSTED
            ).aggregate(total_sales=Sum('total_amount'), count=Count('id'))
            
            # Get returns
            returns_aggr = Invoice.objects.filter(
                pos_session=session,
                invoice_type=Invoice.RETURN_SALE,
                status=Invoice.POSTED
            ).aggregate(total_returns=Sum('total_amount'), count=Count('id'))
            
            sales_amount = sales_aggr['total_sales'] or Decimal('0')
            sales_count = sales_aggr['count'] or 0
            
            returns_amount = returns_aggr['total_returns'] or Decimal('0')
            returns_count = returns_aggr['count'] or 0
            
            total_expected += session.closing_balance_expected
            total_actual += session.closing_balance_actual
            total_diff += session.difference
            
            sessions_data.append({
                'session': session,
                'sales_amount': sales_amount,
                'sales_count': sales_count,
                'returns_amount': returns_amount,
                'returns_count': returns_count,
                'net_sales': sales_amount - returns_amount
            })

        context.update({
            'date_from': date_from,
            'date_to': date_to,
            'users': users,
            'selected_user_id': selected_user_id,
            'sessions_data': sessions_data,
            'total_expected': total_expected,
            'total_actual': total_actual,
            'total_diff': total_diff,
            'title': 'يومية الكاشير (الورديات)'
        })
        return context

