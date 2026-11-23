from django.shortcuts import render, redirect
from django.views.generic import View, ListView, TemplateView, DetailView
from django.db import models
from django.db.models import Sum, Count, F, Q, FloatField
from django.db.models.functions import TruncDay, TruncMonth, Cast, Coalesce
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from apps.tenant.core.mixins import CustomPermissionRequiredMixin
from apps.tenant.invoicing.models import Invoice, InvoiceLine, POSSession
from apps.tenant.inventory.models import Product
from apps.tenant.partners.models import Partner
import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from django.http import HttpResponse
from django.contrib import messages
from apps.tenant.core.access import (
    filter_by_branch, get_branch_scope, is_branch_restricted, is_tenant_admin,
    is_branch_unassigned,
)
from apps.tenant.core.usernames import display_username

# Hard cap on the number of rows written to an Excel export.
EXPORT_MAX_ROWS = 50000


def _date_param(request, name):
    """Return the GET date param if it is a valid YYYY-MM-DD date, else None
    (invalid input must never reach a queryset filter and raise a 500)."""
    import datetime as _dt
    value = (request.GET.get(name) or '').strip()
    if not value:
        return None
    try:
        _dt.datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        return None
    return value


def _scope_treasuries(qs, request):
    """Restrict a Treasury queryset to the request's branch scope."""
    return filter_by_branch(qs, request, 'branch')


class BranchRestrictedDenyMixin:
    """
    For reports built purely on company-wide data (journal entries, partner
    balances) that cannot be scoped to a branch: deny access to non-admin
    employees who are pinned to a branch instead of leaking company figures.
    """
    branch_restricted_message = 'عفواً، هذا التقرير يعرض بيانات على مستوى الشركة بالكامل ولا يتاح لموظفي الفروع.'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and is_branch_restricted(request):
            messages.error(request, self.branch_restricted_message)
            return redirect('core:reports_index')
        return super().dispatch(request, *args, **kwargs)


class CsvExportMixin:
    """
    Mixin to allow exporting any report View directly to a real Microsoft Excel (.xlsx) file.
    The view defines `csv_filename`, `get_csv_headers()`, and `get_csv_row(obj)`.
    """
    csv_filename = "report_export.xlsx"
    
    def get_csv_headers(self):
        return []
        
    def get_csv_row(self, obj):
        return []

    def get(self, request, *args, **kwargs):
        if request.GET.get('export') == 'excel':
            # Disable pagination for complete export
            self.paginate_by = None
            
            response = super().get(request, *args, **kwargs)
            context = getattr(response, 'context_data', {}) or {}
            obj_name = getattr(self, 'context_object_name', None)
            object_list = (context.get(obj_name) if obj_name else None) or context.get('object_list') or context.get('transactions') or context.get('report_data') or []
            # Cap the number of exported rows to protect the server.
            try:
                object_list = object_list[:EXPORT_MAX_ROWS]
            except TypeError:
                object_list = list(object_list)[:EXPORT_MAX_ROWS]
            
            # Create Workbook using openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "تقرير"
            
            # Set Right-To-Left view for Arabic text in Excel
            ws.views.sheetView[0].rightToLeft = True
            
            # Formatting styles
            header_font = Font(name='Calibri', size=12, bold=True, color='FFFFFF')
            header_fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
            data_font = Font(name='Calibri', size=11)
            alignment_right = Alignment(horizontal='right', vertical='center')
            alignment_center = Alignment(horizontal='center', vertical='center')
            thin_border = Border(
                left=Side(style='thin', color='D9D9D9'),
                right=Side(style='thin', color='D9D9D9'),
                top=Side(style='thin', color='D9D9D9'),
                bottom=Side(style='thin', color='D9D9D9')
            )
            
            # Write Header Row
            headers = self.get_csv_headers()
            if headers:
                ws.append(headers)
                for col_num, header in enumerate(headers, 1):
                    cell = ws.cell(row=1, column=col_num)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = alignment_center
                    cell.border = thin_border
            
            # Write Data Rows
            for row_idx, obj in enumerate(object_list, start=2):
                row_data = self.get_csv_row(obj)
                ws.append(row_data)
                for col_num, val in enumerate(row_data, 1):
                    cell = ws.cell(row=row_idx, column=col_num)
                    cell.font = data_font
                    cell.alignment = alignment_right
                    cell.border = thin_border
            
            # Auto-fit Column Widths
            for col in ws.columns:
                max_len = 0
                col_letter = get_column_letter(col[0].column)
                for cell in col:
                    val_str = str(cell.value or '')
                    if len(val_str) > max_len:
                        max_len = len(val_str)
                ws.column_dimensions[col_letter].width = max(max_len + 5, 14)
                
            # Build filename with .xlsx extension
            filename = self.csv_filename
            if filename.endswith('.csv'):
                filename = filename[:-4] + '.xlsx'
            elif not filename.endswith('.xlsx'):
                filename += '.xlsx'
                
            # Save Workbook to memory stream
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            
            excel_resp = HttpResponse(
                output.getvalue(),
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            excel_resp['Content-Disposition'] = f'attachment; filename="{filename}"'
            return excel_resp
            
        return super().get(request, *args, **kwargs)

# ---------------------------------------------------------
# Product Sales Report (مبيعات الأصناف)
# ---------------------------------------------------------
class ProductSalesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_sales_reports'
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
        # Base query: Posted Sales Invoices and Returns
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
            invoice__status=Invoice.POSTED
        )
        qs = filter_by_branch(qs, self.request, 'invoice__branch')
        
        # Apply Filters
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
            
        from django.db.models import Sum, F, Case, When, DecimalField
            
        # Aggregate by product
        aggregated = qs.values(
            'product__id', 
            'product__name', 
            'product__barcode'
        ).annotate(
            sold_qty=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='quantity'), default=0, output_field=DecimalField())),
            returned_qty=Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then='quantity'), default=0, output_field=DecimalField())),
            gross_revenue=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='subtotal'), default=0, output_field=DecimalField())),
            returned_revenue=Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then='subtotal'), default=0, output_field=DecimalField())),
            gross_cogs=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='cogs_amount'), default=0, output_field=DecimalField())),
            returned_cogs=Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then='cogs_amount'), default=0, output_field=DecimalField())),
        ).annotate(
            net_qty=F('sold_qty') - F('returned_qty'),
            net_total=F('gross_revenue') - F('returned_revenue'),
            total_cogs=F('gross_cogs') - F('returned_cogs')
        ).annotate(
            total_qty=F('net_qty'),
            total_revenue=F('net_total')
        ).annotate(
            total_profit=F('total_revenue') - F('total_cogs')
        ).order_by('-total_revenue')
        
        return aggregated
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير مبيعات الأصناف (صافي المبيعات)'
        context['start_date'] = self.request.GET.get('start_date', '')
        context['end_date'] = self.request.GET.get('end_date', '')
        context['search_query'] = self.request.GET.get('search_query', '')
        
        # Top 10 for chart
        top_10 = list(self.get_queryset()[:10])
        context['chart_labels'] = [item['product__name'] for item in top_10]
        context['chart_data_qty'] = [float(item['total_qty'] or 0) for item in top_10]
        context['chart_data_revenue'] = [float(item['total_revenue'] or 0) for item in top_10]
        
        from django.db.models import Sum
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
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/sales_by_period.html'
    context_object_name = 'periods_data'
    paginate_by = 31
    csv_filename = "period_sales_report.csv"

    def get_csv_headers(self):
        return ['الفترة', 'عدد الفواتير', 'الإيرادات (قبل الضريبة)', 'إجمالي الخصومات', 'تكلفة البضاعة المباعة', 'الربح التقريبي']

    def get_csv_row(self, obj):
        period = obj.get('period')
        period_str = period.strftime('%Y-%m-%d') if hasattr(period, 'strftime') else str(period or '')
        return [
            period_str,
            obj.get('invoice_count', 0),
            obj.get('total_revenue', 0),
            obj.get('total_discount', 0),
            obj.get('total_cogs', 0),
            obj.get('total_profit', 0)
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
            invoice__status=Invoice.POSTED
        )
        qs = filter_by_branch(qs, self.request, 'invoice__branch')
        
        # Apply Filters
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
            
        from django.db.models import Sum, Case, When, DecimalField, F, ExpressionWrapper
        # Aggregate
        # Note: Querying InvoiceLine directly avoids duplicate sums from JOINs!
        aggregated = qs.annotate(
            period=trunc_func
        ).values('period').annotate(
            invoice_count=Count('invoice_id', distinct=True),
            gross_revenue=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='subtotal'), default=0, output_field=DecimalField())),
            returned_revenue=Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then='subtotal'), default=0, output_field=DecimalField())),
            gross_cogs=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='cogs_amount'), default=0, output_field=DecimalField())),
            returned_cogs=Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then='cogs_amount'), default=0, output_field=DecimalField())),
            total_tax=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='tax_amount'), When(invoice__invoice_type=Invoice.RETURN_SALE, then=-F('tax_amount')), default=0, output_field=DecimalField())),
            total_discount=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='discount_amount'), When(invoice__invoice_type=Invoice.RETURN_SALE, then=-F('discount_amount')), default=0, output_field=DecimalField())),
        ).annotate(
            total_revenue=F('gross_revenue') - F('returned_revenue'),
            total_cogs=F('gross_cogs') - F('returned_cogs')
        ).annotate(
            total_profit=ExpressionWrapper(F('total_revenue') - F('total_cogs'), output_field=DecimalField())
        ).order_by('-period')
        
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
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/sales_detailed.html'
    context_object_name = 'invoice_lines'
    paginate_by = 50
    csv_filename = "detailed_sales_report.csv"

    def get_csv_headers(self):
        return ['التاريخ', 'رقم الفاتورة', 'نوع الحركة', 'العميل', 'الصنف', 'بيان/ملاحظات الفاتورة', 'الكمية', 'السعر', 'الإجمالي']

    def get_csv_row(self, obj):
        return [
            obj.invoice.date.strftime('%Y-%m-%d') if hasattr(obj.invoice, 'date') and obj.invoice.date else '',
            obj.invoice.invoice_number,
            'مبيعات' if obj.invoice.invoice_type == Invoice.SALE else 'مرتجع',
            obj.invoice.partner.name if obj.invoice.partner else 'عميل نقدي/طياري',
            obj.product.name if obj.product else '',
            obj.invoice.notes or '-',
            obj.quantity,
            obj.unit_price,
            obj.total_amount
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
            invoice__status=Invoice.POSTED
        ).select_related('invoice', 'invoice__partner', 'product')
        qs = filter_by_branch(qs, self.request, 'invoice__branch')
        
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
            
        return qs.order_by('-invoice__date', '-invoice__created_at', '-id')
        
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

class CustomerBalancesReportView(CustomPermissionRequiredMixin, View):
    """
    إعادة توجيه كشف حساب العميل إلى تقرير الشركاء الموحد الشامل
    """
    permission_required = 'core.view_sales_reports'

    def get(self, request, *args, **kwargs):
        partner_id = request.GET.get('partner_id')
        if partner_id:
            return redirect('partners:partner_ledger', pk=partner_id)
        from django.urls import reverse
        return redirect(reverse('core:report_partners') + '?partner_type=CUSTOMER')

class ItemProfitabilityReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/item_profitability.html'
    context_object_name = 'products_data'
    paginate_by = 20
    csv_filename = "item_profitability_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'النوع', 'إجمالي الكمية المباعة', 'إجمالي الإيرادات', 'إجمالي التكلفة', 'صافي الربح', 'نسبة الهامش %']

    def get_csv_row(self, obj):
        rev = float(obj.get('total_revenue') or 0)
        prof = float(obj.get('total_profit') or 0)
        margin = round((prof / rev * 100), 1) if rev > 0 else 0
        p_type = 'خدمة' if obj.get('product__product_type') == 'SERVICE' else 'منتج'
        return [
            obj.get('product__barcode') or '',
            obj.get('product__name') or '',
            p_type,
            obj.get('total_qty') or 0,
            obj.get('total_revenue') or 0,
            obj.get('total_cogs') or 0,
            obj.get('total_profit') or 0,
            f"{margin}%"
        ]

    def get_queryset(self):
        qs = InvoiceLine.objects.filter(
            invoice__invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
            invoice__status=Invoice.POSTED
        )
        qs = filter_by_branch(qs, self.request, 'invoice__branch')
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
        search_query = self.request.GET.get('search_query')
        if start_date: qs = qs.filter(invoice__date__gte=start_date)
        if end_date: qs = qs.filter(invoice__date__lte=end_date)
        if search_query:
            qs = qs.filter(
                Q(product__name__icontains=search_query) |
                Q(product__barcode__icontains=search_query)
            )

        from django.db.models import Sum, Case, When, DecimalField, F, ExpressionWrapper
        aggregated = qs.values(
            'product__id',
            'product__name',
            'product__barcode',
            'product__product_type'
        ).annotate(
            sold_qty=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='quantity'), default=0, output_field=DecimalField())),
            returned_qty=Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then='quantity'), default=0, output_field=DecimalField())),
            gross_revenue=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='subtotal'), default=0, output_field=DecimalField())),
            returned_revenue=Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then='subtotal'), default=0, output_field=DecimalField())),
            gross_cogs=Sum(Case(When(invoice__invoice_type=Invoice.SALE, then='cogs_amount'), default=0, output_field=DecimalField())),
            returned_cogs=Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then='cogs_amount'), default=0, output_field=DecimalField())),
        ).annotate(
            total_qty=F('sold_qty') - F('returned_qty'),
            total_revenue=F('gross_revenue') - F('returned_revenue'),
            total_cogs=F('gross_cogs') - F('returned_cogs')
        ).annotate(
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
            
        # Grand totals via database aggregation
        from django.db.models import Value, DecimalField
        from django.db.models.functions import Coalesce
        qs = self.get_queryset()
        totals = qs.aggregate(
            gt_profit=Coalesce(Sum('total_profit'), Value(0), output_field=DecimalField()),
            gt_revenue=Coalesce(Sum('total_revenue'), Value(0), output_field=DecimalField())
        )
        gt_profit = totals['gt_profit'] or Decimal('0')
        gt_revenue = totals['gt_revenue'] or Decimal('0')
        context['grand_totals'] = {
            'gt_profit': gt_profit,
            'gt_margin': (gt_profit / gt_revenue * 100) if gt_revenue > 0 else 0,
        }
        return context

class UserSalesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/sales_by_user.html'
    context_object_name = 'users_data'
    paginate_by = 20
    csv_filename = "sales_by_user_report.csv"

    def get_csv_headers(self):
        return ['اسم المستخدم / الكاشير', 'يوزر نيم', 'عدد فواتير البيع', 'قيمة المبيعات', 'عدد المرتجعات', 'قيمة المرتجعات', 'صافي الضريبة', 'صافي المبيعات (الدخل)']

    def get_csv_row(self, obj):
        name = f"{obj['cashier__first_name']} {obj['cashier__last_name']}".strip()
        if not name:
            name = display_username(obj['cashier__username'])
        return [
            name,
            display_username(obj['cashier__username']),
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
        qs = filter_by_branch(qs, self.request)
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
        ).order_by('-total_sales')
        
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
            
        # Grand totals via database aggregation
        from django.db.models import Value, DecimalField
        from django.db.models.functions import Coalesce
        qs = self.get_queryset()
        totals = qs.aggregate(
            gt_sales=Coalesce(Sum('total_sales'), Value(0), output_field=DecimalField()),
            gt_returns=Coalesce(Sum('total_returns'), Value(0), output_field=DecimalField())
        )
        gt_sales = totals['gt_sales'] or Decimal('0')
        gt_returns = totals['gt_returns'] or Decimal('0')
        context['grand_totals'] = {
            'gt_sales': gt_sales,
            'gt_returns': gt_returns,
            'gt_net': gt_sales - gt_returns
        }
            
        return context

class CustomerDebtsReportView(BranchRestrictedDenyMixin, CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_sales_reports'
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
            getattr(obj, 'tax_id', '') or getattr(obj, 'tax_number', ''),
            obj.customer_balance
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
        
        # Batch compute balances for the paginated subset to avoid N+1 queries:
        from apps.tenant.partners.services import batch_get_partners_balances
        page_customers = list(context['customers'])
        batch_get_partners_balances(partner_qs=page_customers)
        page_total = sum(c.customer_balance for c in page_customers if c.customer_balance > 0)
        context['page_total_debt'] = page_total
        
        return context

class SalesReturnsReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_sales_reports'
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
            obj.cashier.first_name or display_username(obj.cashier) if obj.cashier else '',
            obj.total_amount,
            obj.notes
        ]

    def get_queryset(self):
        qs = Invoice.objects.filter(
            invoice_type=Invoice.RETURN_SALE,
            status=Invoice.POSTED
        ).select_related('partner', 'cashier').order_by('-date', '-created_at')
        qs = filter_by_branch(qs, self.request)
        
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
    permission_required = 'core.view_sales_reports'
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
        qs = filter_by_branch(qs, self.request, 'invoice__branch')
        
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
            
        _ret_branch = get_branch_scope(self.request)
        _ret_extra = {'product__invoice_lines__invoice__branch': _ret_branch} if _ret_branch is not None else {}
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
                         product__invoice_lines__invoice__date__lte=end_date if end_date else '2100-01-01',
                         **_ret_extra)
            ),
            returned_total=Sum(
                'total_amount', 
                filter=Q(product__invoice_lines__invoice__invoice_type=Invoice.RETURN_PURCHASE, 
                         product__invoice_lines__invoice__status=Invoice.POSTED,
                         product__invoice_lines__invoice__date__gte=start_date if start_date else '2000-01-01',
                         product__invoice_lines__invoice__date__lte=end_date if end_date else '2100-01-01',
                         **_ret_extra)
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
    permission_required = 'core.view_sales_reports'
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
        qs = filter_by_branch(qs, self.request, 'invoice__branch')
        
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
        from django.db.models import Sum, Q
        from django.db.models.functions import Coalesce

        agg = qs.aggregate(
            total_purchases=Coalesce(Sum('total_amount', filter=Q(invoice__invoice_type=Invoice.PURCHASE)), Decimal('0.00')),
            total_returns=Coalesce(Sum('total_amount', filter=Q(invoice__invoice_type=Invoice.RETURN_PURCHASE)), Decimal('0.00')),
        )
        grand_totals = {
            'total_purchases': agg['total_purchases'],
            'total_returns': agg['total_returns'],
            'net_purchases': agg['total_purchases'] - agg['total_returns'],
        }
        context['grand_totals'] = grand_totals
        
        return context

# ---------------------------------------------------------
# Period Purchases Report (مشتريات فترة)
# ---------------------------------------------------------
class PeriodPurchasesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_sales_reports'
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
        qs = filter_by_branch(qs, self.request, 'invoice__branch')
        
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
        returns_qs = filter_by_branch(returns_qs, self.request, 'invoice__branch')
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
    permission_required = 'core.view_sales_reports'
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
        qs = filter_by_branch(qs, self.request, 'invoice__branch')
        
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
        last_price_base = filter_by_branch(InvoiceLine.objects.filter(
            product_id=OuterRef('product__id'),
            invoice__invoice_type=Invoice.PURCHASE,
            invoice__status=Invoice.POSTED
        ), self.request, 'invoice__branch')
        last_price_subquery = last_price_base.order_by('-invoice__date', '-id').values('unit_price')[:1]
            
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
class SupplierBalancesReportView(CustomPermissionRequiredMixin, View):
    """
    إعادة توجيه كشف حساب المورد إلى تقرير الشركاء الموحد الشامل
    """
    permission_required = 'core.view_sales_reports'

    def get(self, request, *args, **kwargs):
        partner_id = request.GET.get('partner_id')
        if partner_id:
            return redirect('partners:partner_ledger', pk=partner_id)
        from django.urls import reverse
        return redirect(reverse('core:report_partners') + '?partner_type=SUPPLIER')


# ---------------------------------------------------------
# Unified Partners Report (تقرير الشركاء الشامل - عملاء وموردين)
# ---------------------------------------------------------
class PartnersReportView(BranchRestrictedDenyMixin, CustomPermissionRequiredMixin, TemplateView):
    """
    تقرير الشركاء الشامل (عملاء وموردين):
    يعرض جميع الشركاء وأرصدتهم التفصيلية من مبيعات ومشتريات وسندات
    مع إمكانية الفلترة والبحث والدخول لكشف حساب أي شريك بنقرة واحدة.
    """
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/partners_report.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.partners.services import get_all_partners_summary
        from django.core.paginator import Paginator

        partner_type = self.request.GET.get('partner_type', '')
        balance_status = self.request.GET.get('balance_status', '')
        search_query = self.request.GET.get('search_query', '')
        page_number = self.request.GET.get('page', 1)
        per_page = self.request.GET.get('per_page', 25)

        try:
            per_page = int(per_page)
            if per_page not in [10, 25, 50, 100, 200]:
                per_page = 25
        except (ValueError, TypeError):
            per_page = 25

        summary = get_all_partners_summary(
            partner_type=partner_type if partner_type else None,
            balance_filter=balance_status if balance_status else None,
            search_query=search_query if search_query else None
        )

        paginator = Paginator(summary['partners'], per_page)
        page_obj = paginator.get_page(page_number)

        context.update({
            'title': 'تقرير الشركاء الشامل (عملاء وموردين)',
            'partners': page_obj,
            'page_obj': page_obj,
            'paginator': paginator,
            'is_paginated': page_obj.has_other_pages(),
            'per_page': per_page,
            'total_partners_count': summary['total_partners_count'],
            'total_receivables': summary['total_receivables'],
            'total_payables': summary['total_payables'],
            'net_position': summary['net_position'],
            'total_balanced_count': summary['total_balanced_count'],
            'selected_partner_type': partner_type,
            'selected_balance_status': balance_status,
            'search_query': search_query,
        })
        return context

# ---------------------------------------------------------
# Supplier Debts Report (مستحقات الموردين الإجمالي)
# ---------------------------------------------------------
class SupplierDebtsReportView(BranchRestrictedDenyMixin, CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_sales_reports'
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
            getattr(obj, 'tax_id', '') or getattr(obj, 'tax_number', ''),
            obj.supplier_balance
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
        from apps.tenant.partners.services import batch_get_partners_balances
        suppliers = list(context[self.context_object_name])
        batch_get_partners_balances(partner_qs=suppliers)
        
        # Calculate total debt
        total_debts = sum(s.supplier_balance for s in suppliers if s.supplier_balance > 0)
        context['total_debts'] = total_debts
            
        return context

# ---------------------------------------------------------
# User Purchases Report (مشتريات المستخدمين)
# ---------------------------------------------------------
class UserPurchasesReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/purchases_by_user.html'
    context_object_name = 'users_data'
    paginate_by = 20
    csv_filename = "purchases_by_user_report.csv"

    def get_csv_headers(self):
        return ['اسم المستخدم', 'يوزر نيم', 'عدد فواتير الشراء', 'قيمة المشتريات', 'عدد المرتجعات', 'قيمة المرتجعات', 'صافي المشتريات']

    def get_csv_row(self, obj):
        name = f"{obj['cashier__first_name']} {obj['cashier__last_name']}".strip()
        if not name:
            name = display_username(obj['cashier__username'])
        return [
            name,
            display_username(obj['cashier__username']),
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
        qs = filter_by_branch(qs, self.request)
        
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
        
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
    permission_required = 'core.view_sales_reports'
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
            obj.cashier.first_name or display_username(obj.cashier) if obj.cashier else '',
            obj.total_amount,
            obj.notes
        ]

    def get_queryset(self):
        qs = Invoice.objects.filter(
            invoice_type=Invoice.RETURN_PURCHASE,
            status=Invoice.POSTED
        ).select_related('partner', 'cashier')
        qs = filter_by_branch(qs, self.request)
        
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
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
    permission_required = 'core.view_inventory_reports'
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

        stock_subquery = filter_by_branch(InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ), self.request, 'warehouse__branch').values('product_id').annotate(
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

class OverstockReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_inventory_reports'
    template_name = 'reports/inventory_overstock.html'
    context_object_name = 'products'
    paginate_by = 50
    csv_filename = "overstock_report.xlsx"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'الرصيد الحالي', 'الحد الأقصى للمخزون', 'الكمية الفائضة', 'تكلفة الوحدة', 'إجمالي رأس المال الفائض']

    def get_csv_row(self, obj):
        cost = obj.last_purchase_price or Decimal('0')
        excess = getattr(obj, 'excess_qty', None)
        if excess is None:
            excess = obj.current_stock - obj.max_stock_level if obj.current_stock > obj.max_stock_level else Decimal('0')
        excess_val = excess * cost
        return [
            obj.barcode or '-',
            obj.name,
            float(obj.current_stock),
            float(obj.max_stock_level),
            float(excess),
            float(cost),
            float(excess_val)
        ]

    def get_queryset(self):
        from django.db.models import OuterRef, Subquery, Value, DecimalField, F, Q, Sum, Count, Case, When
        from django.db.models.functions import Coalesce
        from apps.tenant.inventory.models import SerialItem

        stock_subquery = filter_by_branch(InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ), self.request, 'warehouse__branch').values('product_id').annotate(
            total=Sum('quantity_remaining')
        ).values('total')

        serial_subquery = filter_by_branch(SerialItem.objects.filter(
            product_id=OuterRef('pk'),
            is_sold=False
        ), self.request, 'warehouse__branch').values('product_id').annotate(
            total=Count('id')
        ).values('total')

        qs = Product.objects.filter(is_active=True, max_stock_level__gt=0).annotate(
            current_stock=Case(
                When(product_type='SERIALIZED', then=Coalesce(Subquery(serial_subquery), Value(0), output_field=DecimalField())),
                default=Coalesce(Subquery(stock_subquery), Value(0), output_field=DecimalField()),
                output_field=DecimalField()
            )
        ).filter(current_stock__gt=F('max_stock_level')).order_by('-current_stock')

        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(Q(name__icontains=search_query) | Q(barcode__icontains=search_query))
        
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير فائض وتكدس المخزون (Overstock)'
        context['search_query'] = self.request.GET.get('search_query', '')
        
        object_list = context.get('products') or []
        for p in object_list:
            cost = p.last_purchase_price or Decimal('0')
            excess = p.current_stock - p.max_stock_level if p.current_stock > p.max_stock_level else Decimal('0')
            p.excess_qty = excess
            p.excess_cost = excess * cost
            p.overstock_pct = round(((p.current_stock - p.max_stock_level) / p.max_stock_level) * 100, 1) if p.max_stock_level > 0 else 0

        # تجميع إجماليات الفائض ورأس المال المجمد مباشرة بقاعدة البيانات
        base_qs = self.get_queryset()
        overstock_agg = base_qs.aggregate(
            total_excess_items=Count('id'),
            total_excess_qty=Coalesce(Sum(F('current_stock') - F('max_stock_level')), Decimal('0.00')),
            total_frozen_capital=Coalesce(Sum((F('current_stock') - F('max_stock_level')) * F('last_purchase_price')), Decimal('0.00'))
        )

        context['total_excess_items'] = overstock_agg['total_excess_items']
        context['total_excess_qty'] = overstock_agg['total_excess_qty']
        context['total_frozen_capital'] = overstock_agg['total_frozen_capital']
        return context

class ProductExpiryReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_inventory_reports'
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
        qs = filter_by_branch(qs, self.request, 'warehouse__branch')

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
    permission_required = 'core.view_inventory_reports'
    template_name = 'reports/inventory_valuation.html'
    context_object_name = 'products'
    paginate_by = 50
    csv_filename = "inventory_valuation_report.csv"

    def get_csv_headers(self):
        return ['الباركود', 'اسم الصنف', 'الرصيد الفعلي', 'متوسط التكلفة', 'القيمة الإجمالية']

    def get_csv_row(self, obj):
        total_val = getattr(obj, 'total_value', getattr(obj, 'total_val', Decimal('0')))
        curr_stock = getattr(obj, 'current_stock', Decimal('0'))
        avg_cost = getattr(obj, 'avg_cost', None)
        if avg_cost is None:
            avg_cost = round(total_val / curr_stock, 4) if curr_stock > 0 else Decimal('0')
        return [
            obj.barcode or '-',
            obj.name,
            curr_stock,
            avg_cost,
            total_val
        ]

    def get_queryset(self):
        # We need actual stock and actual cost.
        from django.db.models import OuterRef, Subquery, Value, DecimalField, F, ExpressionWrapper
        from django.db.models.functions import Coalesce

        # 1. Total stock
        stock_subquery = filter_by_branch(InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ), self.request, 'warehouse__branch').values('product_id').annotate(
            total=Sum('quantity_remaining')
        ).values('total')

        # 2. Total cost value (sum of quantity_remaining * unit_cost)
        # Using ExpressionWrapper to calculate the sum properly
        cost_subquery = filter_by_branch(InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ), self.request, 'warehouse__branch').values('product_id').annotate(
            total_cost_val=Sum(ExpressionWrapper(F('quantity_remaining') * F('unit_cost'), output_field=DecimalField()))
        ).values('total_cost_val')

        qs = Product.objects.filter(is_active=True).annotate(
            current_stock=Coalesce(Subquery(stock_subquery), Value(0), output_field=DecimalField()),
            total_val=Coalesce(Subquery(cost_subquery), Value(0), output_field=DecimalField())
        ).filter(current_stock__gt=0).order_by('-total_val')

        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(Q(name__icontains=search_query) | Q(barcode__icontains=search_query))

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تقرير جرد المخزون الفعلي (التقييم)'
        context['search_query'] = self.request.GET.get('search_query', '')
        
        # Calculate grand totals via database aggregation without iterating entire queryset in Python
        from django.db.models import Value, DecimalField
        from django.db.models.functions import Coalesce
        totals = self.get_queryset().aggregate(
            total_qty=Coalesce(Sum('current_stock'), Value(0), output_field=DecimalField()),
            total_value=Coalesce(Sum('total_val'), Value(0), output_field=DecimalField())
        )
        context['grand_totals'] = {
            'total_qty': totals['total_qty'],
            'total_value': totals['total_value']
        }

        # Decorate ONLY the products on the current paginated page (lazy evaluation):
        page_products = context.get('products') or context.get('page_obj') or []
        for product in page_products:
            product.total_value = product.total_val
            product.avg_cost = round(product.total_val / product.current_stock, 4) if product.current_stock > 0 else 0

        return context

# ---------------------------------------------------------
# Additional Inventory Reports
# ---------------------------------------------------------
from apps.tenant.inventory.models import StockMovement

class WarehouseStockReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_inventory_reports'
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
        qs = filter_by_branch(qs, self.request, 'warehouse__branch')
        
        search_query = self.request.GET.get('search_query')
        if search_query:
            qs = qs.filter(Q(product__name__icontains=search_query) | Q(product__barcode__icontains=search_query))
            
        warehouse_id = self.request.GET.get('warehouse')
        if warehouse_id and str(warehouse_id).isdigit():
            # Branch scope above still applies: a pinned user cannot escape it.
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
        context['warehouses'] = filter_by_branch(Warehouse.objects.filter(is_active=True), self.request)
        context['selected_warehouse'] = self.request.GET.get('warehouse', '')
        
        return context

class ItemMovementReportView(CsvExportMixin, CustomPermissionRequiredMixin, ListView):
    permission_required = 'core.view_inventory_reports'
    template_name = 'reports/inventory_movement.html'
    context_object_name = 'movements'
    paginate_by = 50
    csv_filename = "item_movement_report.csv"

    def get_csv_headers(self):
        return ['التاريخ', 'اسم الصنف', 'المستودع', 'نوع الحركة', 'الكمية', 'سعر الشراء', 'سعر البيع', 'الرصيد بعد الحركة', 'المرجع', 'ملاحظات']

    def get_csv_row(self, obj):
        from django.utils.timezone import localtime
        date_str = localtime(obj.created_at).strftime("%Y-%m-%d %H:%M") if obj.created_at else '-'
        purchase_price = getattr(obj, 'purchase_price', obj.unit_cost or getattr(obj.product, 'last_purchase_price', 0))
        selling_price = getattr(obj, 'selling_price', getattr(obj.product, 'sale_price', 0))
        return [
            date_str,
            obj.product.name,
            obj.warehouse.name,
            obj.get_movement_type_display(),
            obj.quantity,
            purchase_price,
            selling_price,
            getattr(obj, 'running_balance', '-'),
            obj.reference or '-',
            obj.notes or '-'
        ]

    def get_queryset(self):
        from apps.tenant.inventory.models import StockMovement
        from django.db.models import Q
        from datetime import datetime, timedelta
        
        qs = StockMovement.objects.all().select_related('product', 'warehouse')
        qs = filter_by_branch(qs, self.request, 'warehouse__branch')
        
        # Date filters
        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')
        
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            try:
                date_to_obj = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                qs = qs.filter(created_at__lt=date_to_obj)
            except ValueError:
                pass
                
        product_id = self.request.GET.get('product_id')
        if product_id and str(product_id).isdigit():
            qs = qs.filter(product_id=product_id)
            
        movement_type = self.request.GET.get('movement_type')
        if movement_type:
            qs = qs.filter(movement_type=movement_type)
            
        warehouse_id = self.request.GET.get('warehouse_id')
        if warehouse_id and str(warehouse_id).isdigit():
            qs = qs.filter(warehouse_id=warehouse_id)
            
        return qs.order_by('-created_at', '-id')

    def get_context_data(self, **kwargs):
        from apps.tenant.inventory.models import StockMovement, Product, Warehouse
        from django.db.models import Sum, Q
        from decimal import Decimal
        
        context = super().get_context_data(**kwargs)
        context['title'] = 'حركة الأصناف (كارت الصنف)'
        
        product_id = self.request.GET.get('product_id')
        warehouse_id = self.request.GET.get('warehouse_id')
        date_from = _date_param(self.request, 'date_from')
        context['date_from'] = date_from or ''
        context['date_to'] = self.request.GET.get('date_to', '')
        context['selected_product'] = product_id or ''
        context['selected_warehouse'] = warehouse_id or ''
        context['selected_type'] = self.request.GET.get('movement_type', '')
        
        if product_id and str(product_id).isdigit():
            context['products'] = Product.objects.filter(id=product_id).only('id', 'name', 'barcode')
        else:
            context['products'] = Product.objects.filter(is_active=True).only('id', 'name', 'barcode').order_by('name')[:30]
        context['warehouses'] = filter_by_branch(Warehouse.objects.all(), self.request)
        context['movement_types'] = StockMovement.MOVEMENT_TYPES
        
        opening_balance = Decimal('0')
        closing_balance = Decimal('0')

        object_list = context.get('object_list') or []
        if object_list:
            from apps.tenant.invoicing.models import InvoiceLine
            references = [obj.reference for obj in object_list if obj.reference]
            inv_lines = filter_by_branch(InvoiceLine.objects.filter(
                invoice__invoice_number__in=references
            ), self.request, 'invoice__branch').select_related('invoice') if references else []
            
            inv_line_map = {}
            for il in inv_lines:
                inv_line_map[(il.invoice.invoice_number, il.product_id)] = il

            for obj in object_list:
                il = inv_line_map.get((obj.reference, obj.product_id))
                
                # Purchase Price (سعر الشراء / التكلفة)
                if obj.unit_cost and obj.unit_cost > Decimal('0'):
                    obj.purchase_price = obj.unit_cost
                elif il and il.invoice.invoice_type in ['PURCHASE', 'RETURN_PURCHASE']:
                    obj.purchase_price = il.unit_price
                else:
                    obj.purchase_price = getattr(obj.product, 'last_purchase_price', Decimal('0')) or Decimal('0')

                # Selling Price (سعر البيع)
                if il and il.invoice.invoice_type in ['SALE', 'RETURN_SALE']:
                    obj.selling_price = il.unit_price
                else:
                    obj.selling_price = getattr(obj.product, 'sale_price', Decimal('0')) or Decimal('0')
        
        if product_id:
            try:
                product_id = int(product_id)
            except (TypeError, ValueError):
                product_id = 0
            prev_qs = StockMovement.objects.filter(product_id=product_id)
            prev_qs = filter_by_branch(prev_qs, self.request, 'warehouse__branch')
            if warehouse_id and str(warehouse_id).isdigit():
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
    permission_required = 'core.view_inventory_reports'
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

        try:
            days_inactive = int(self.request.GET.get('days', 30))
        except (TypeError, ValueError):
            days_inactive = 30
        days_inactive = max(0, min(days_inactive, 3650))
        threshold_date = timezone.now().date() - datetime.timedelta(days=days_inactive)

        # Current stock subquery
        stock_subquery = filter_by_branch(InventoryBatch.objects.filter(
            product_id=OuterRef('pk')
        ), self.request, 'warehouse__branch').values('product_id').annotate(
            total=Sum('quantity_remaining')
        ).values('total')

        # Last sale subquery
        last_sale_subquery = filter_by_branch(StockMovement.objects.filter(
            product_id=OuterRef('pk'),
            movement_type='OUT'  # OUT is typically a sale
        ), self.request, 'warehouse__branch').values('product_id').annotate(
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


def _get_operating_expense_account_ids():
    """
    Returns IDs of all operating expense accounts based on chart-of-accounts tree hierarchy
    and account_type, regardless of what code numbering convention the user used.
    Excludes COGS (Cost of Goods Sold) accounts which belong in Gross Profit.
    """
    from apps.tenant.accounting.models import Account
    # 1. Any account with account_type == EXPENSE
    expense_account_ids = set(Account.objects.filter(account_type=Account.EXPENSE).values_list('id', flat=True))

    # 2. Any descendant of root accounts of type EXPENSE
    root_expense_accounts = Account.objects.filter(parent__isnull=True, account_type=Account.EXPENSE)
    for root in root_expense_accounts:
        expense_account_ids.update(root.get_descendants(include_self=True).values_list('id', flat=True))

    # 3. Exclude COGS accounts (تكلفة البضاعة المباعة) - uses is_cogs=True flag
    cogs_account_ids = set(Account.objects.filter(id__in=expense_account_ids, is_cogs=True).values_list('id', flat=True))
    if not cogs_account_ids:
        cogs_account_ids = set(Account.objects.filter(code__in=['5100', '5105']).values_list('id', flat=True))
    
    # Also exclude contra-purchases/returns (5110) if present
    cogs_account_ids.update(Account.objects.filter(id__in=expense_account_ids, code='5110').values_list('id', flat=True))

    return expense_account_ids - cogs_account_ids


class IncomeStatementReportView(BranchRestrictedDenyMixin, CustomPermissionRequiredMixin, TemplateView):
    # Depending on your permissions, you can use something appropriate
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/accounting_income_statement.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.invoicing.models import Invoice, InvoiceLine
        from apps.tenant.accounting.models import Expense
        from django.db.models import Sum
        from django.utils import timezone
        import datetime

        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')
        
        today = timezone.now().date()
        if date_from:
            try:
                date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            except ValueError:
                messages.warning(self.request, 'تاريخ البداية غير صالح، تم استخدام القيمة الافتراضية.')
                date_from = None
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')

        if date_to:
            try:
                date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()
            except ValueError:
                messages.warning(self.request, 'تاريخ النهاية غير صالح، تم استخدام القيمة الافتراضية.')
                date_to = None
        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')

        # 1. Sales Revenue
        sales_invoices = Invoice.objects.filter(
            invoice_type=Invoice.SALE,
            status=Invoice.POSTED,
            date__range=[date_from_obj, date_to_obj]
        )
        sales_invoices = filter_by_branch(sales_invoices, self.request)
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
        return_invoices = filter_by_branch(return_invoices, self.request)
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
        sales_cogs = filter_by_branch(InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.SALE,
            invoice__status=Invoice.POSTED,
            invoice__date__range=[date_from_obj, date_to_obj]
        ), self.request, 'invoice__branch').aggregate(total_cogs=Sum('cogs_amount'))['total_cogs'] or 0
        
        returns_cogs = filter_by_branch(InvoiceLine.objects.filter(
            invoice__invoice_type=Invoice.RETURN_SALE,
            invoice__status=Invoice.POSTED,
            invoice__date__range=[date_from_obj, date_to_obj]
        ), self.request, 'invoice__branch').aggregate(total_cogs=Sum('cogs_amount'))['total_cogs'] or 0
        
        net_cogs = sales_cogs - returns_cogs

        # 4. Gross Profit
        gross_profit = net_revenue - net_cogs

        # 5. Operating Expenses (All posted ledger movements on expense accounts hierarchy)
        from apps.tenant.accounting.models import JournalItem, Account, JournalEntry
        operating_expense_account_ids = _get_operating_expense_account_ids()
        operating_expense_items = JournalItem.objects.filter(
            account_id__in=operating_expense_account_ids,
            entry__status=JournalEntry.POSTED,
            entry__date__range=[date_from_obj, date_to_obj]
        ).select_related('account')

        # تجميع وحساب المصروفات وتصنيفها في قاعدة البيانات مباشرة بـ SQL
        total_expenses = operating_expense_items.aggregate(
            total=Coalesce(Sum(F('debit') - F('credit')), Decimal('0.00'))
        )['total']

        expenses_by_account = [
            {'expense_account__code': row['account__code'], 'expense_account__name': row['account__name'], 'total_amount': row['total_amount']}
            for row in operating_expense_items.values('account__code', 'account__name').annotate(
                total_amount=Coalesce(Sum(F('debit') - F('credit')), Decimal('0.00'))
            ).filter(total_amount__gt=0).order_by('-total_amount')
        ]

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


class IncomeStatementDetailView(BranchRestrictedDenyMixin, CustomPermissionRequiredMixin, TemplateView):
    """
    تقرير تفصيلي لحركات وبنود قائمة الدخل (Drill-Down):
    يعرض الفواتير والقيود والعمليات المكونة لأي رقم في قائمة الدخل.
    """
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/accounting_income_statement_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.invoicing.models import Invoice, InvoiceLine
        from apps.tenant.accounting.models import JournalItem, Account, JournalEntry
        from django.db.models import Sum, F, Case, When, Value, DecimalField
        from django.db.models.functions import Coalesce
        from django.utils import timezone
        import datetime
        from decimal import Decimal
        from django.core.paginator import Paginator
        from django.urls import reverse

        detail_type = self.request.GET.get('type', 'gross_sales')
        account_code = self.request.GET.get('account')
        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')

        today = timezone.now().date()
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            except ValueError:
                date_from_obj = today.replace(day=1)
                date_from = date_from_obj.strftime('%Y-%m-%d')

        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()
            except ValueError:
                date_to_obj = today
                date_to = date_to_obj.strftime('%Y-%m-%d')

        title = ""
        badge_type = ""
        qs = None

        def get_inv_url(inv_id):
            try:
                return reverse('invoicing:invoice_detail', kwargs={'invoice_id': inv_id})
            except Exception:
                return f"/invoicing/invoice/{inv_id}/"

        def get_entry_url(entry_id):
            try:
                return reverse('accounting:journal_detail', kwargs={'pk': entry_id})
            except Exception:
                return f"/accounting/journals/{entry_id}/"

        if detail_type in ['gross_sales', 'sales_discount', 'net_sales']:
            qs = Invoice.objects.filter(
                invoice_type=Invoice.SALE,
                status=Invoice.POSTED,
                date__range=[date_from_obj, date_to_obj]
            ).select_related('partner', 'branch').order_by('-date', '-id')
            qs = filter_by_branch(qs, self.request)

            if detail_type == 'sales_discount':
                title = "تفاصيل خصومات المبيعات"
                badge_type = "discount"
                qs = qs.filter(discount_amount__gt=0)
                total_amount = qs.aggregate(t=Coalesce(Sum('discount_amount'), Value(0), output_field=DecimalField()))['t']
            elif detail_type == 'gross_sales':
                title = "تفاصيل إجمالي المبيعات (قبل الخصم)"
                badge_type = "sales"
                total_amount = qs.aggregate(t=Coalesce(Sum('subtotal'), Value(0), output_field=DecimalField()))['t']
            else: # net_sales
                title = "تفاصيل إجمالي المبيعات الصافية"
                badge_type = "sales"
                total_amount = qs.aggregate(
                    t=Coalesce(Sum(F('subtotal') - Coalesce(F('discount_amount'), Value(0), output_field=DecimalField())), Value(0), output_field=DecimalField())
                )['t']

        elif detail_type == 'sales_returns':
            title = "تفاصيل مردودات المبيعات (المرتجعات)"
            badge_type = "returns"
            qs = Invoice.objects.filter(
                invoice_type=Invoice.RETURN_SALE,
                status=Invoice.POSTED,
                date__range=[date_from_obj, date_to_obj]
            ).select_related('partner', 'branch').order_by('-date', '-id')
            qs = filter_by_branch(qs, self.request)
            total_amount = qs.aggregate(
                t=Coalesce(Sum(F('subtotal') - Coalesce(F('discount_amount'), Value(0), output_field=DecimalField())), Value(0), output_field=DecimalField())
            )['t']

        elif detail_type == 'net_revenue':
            title = "تفاصيل صافي الإيرادات (المبيعات والمردودات)"
            badge_type = "net_revenue"
            qs = Invoice.objects.filter(
                invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
                status=Invoice.POSTED,
                date__range=[date_from_obj, date_to_obj]
            ).select_related('partner', 'branch').order_by('-date', '-id')
            qs = filter_by_branch(qs, self.request)
            net_expr = F('subtotal') - Coalesce(F('discount_amount'), Value(0), output_field=DecimalField())
            total_amount = qs.aggregate(
                t=Coalesce(Sum(Case(When(invoice_type=Invoice.RETURN_SALE, then=-net_expr), default=net_expr, output_field=DecimalField())), Value(0), output_field=DecimalField())
            )['t']

        elif detail_type == 'cogs':
            title = "تفاصيل تكلفة البضاعة المباعة (COGS)"
            badge_type = "cogs"
            qs = InvoiceLine.objects.filter(
                invoice__invoice_type__in=[Invoice.SALE, Invoice.RETURN_SALE],
                invoice__status=Invoice.POSTED,
                invoice__date__range=[date_from_obj, date_to_obj]
            ).select_related('invoice', 'product', 'invoice__partner', 'invoice__branch').order_by('-invoice__date', '-id')
            qs = filter_by_branch(qs, self.request, 'invoice__branch')
            total_amount = qs.aggregate(
                t=Coalesce(Sum(Case(When(invoice__invoice_type=Invoice.RETURN_SALE, then=-Coalesce(F('cogs_amount'), Value(0), output_field=DecimalField())), default=Coalesce(F('cogs_amount'), Value(0), output_field=DecimalField()), output_field=DecimalField())), Value(0), output_field=DecimalField())
            )['t']

        elif detail_type == 'expenses':
            badge_type = "expenses"
            operating_expense_account_ids = _get_operating_expense_account_ids()
            qs = JournalItem.objects.filter(
                account_id__in=operating_expense_account_ids,
                entry__status=JournalEntry.POSTED,
                entry__date__range=[date_from_obj, date_to_obj]
            ).select_related('account', 'entry').order_by('-entry__date', '-id')

            if account_code:
                qs = qs.filter(account__code=account_code)
                acc = Account.objects.filter(code=account_code).first()
                acc_name = acc.name if acc else account_code
                title = f"تفاصيل مصروف: {acc_name} ({account_code})"
            else:
                title = "تفاصيل المصروفات التشغيلية الإجمالية"

            total_amount = qs.aggregate(
                t=Coalesce(Sum(Coalesce(F('debit'), Value(0), output_field=DecimalField()) - Coalesce(F('credit'), Value(0), output_field=DecimalField())), Value(0), output_field=DecimalField())
            )['t']
        else:
            total_amount = Decimal('0')
            qs = Invoice.objects.none()

        # Database-level pagination (loads only 50 items instead of looping over thousands in memory)
        page_number = self.request.GET.get('page', 1)
        paginator = Paginator(qs, 50)
        page_obj = paginator.get_page(page_number)

        records = []
        for item in page_obj:
            if detail_type in ['sales_discount', 'gross_sales', 'net_sales', 'sales_returns', 'net_revenue']:
                inv = item
                is_return = inv.invoice_type == Invoice.RETURN_SALE
                sub = inv.subtotal or Decimal('0')
                disc = inv.discount_amount or Decimal('0')
                net_val = sub - disc
                if detail_type == 'sales_discount':
                    focus = disc
                elif detail_type == 'gross_sales':
                    focus = sub
                elif detail_type == 'sales_returns':
                    focus = net_val
                elif detail_type == 'net_revenue':
                    focus = -net_val if is_return else net_val
                else:
                    focus = net_val

                records.append({
                    'number': inv.invoice_number,
                    'id': inv.id,
                    'url': get_inv_url(inv.id),
                    'date': inv.date,
                    'partner': inv.partner.name if inv.partner else 'عميل نقدي',
                    'branch': inv.branch.name if inv.branch else '—',
                    'subtotal': -sub if (is_return and detail_type == 'net_revenue') else sub,
                    'discount': disc,
                    'net': -net_val if (is_return and detail_type == 'net_revenue') else net_val,
                    'tax': inv.tax_amount,
                    'total': -inv.total_amount if (is_return and detail_type == 'net_revenue') else inv.total_amount,
                    'amount_focus': focus,
                    'status': inv.get_payment_type_display(),
                    'is_return': is_return,
                    'doc_type': 'مرتجع بيع' if is_return else 'فاتورة بيع'
                })
            elif detail_type == 'cogs':
                l = item
                is_return = l.invoice.invoice_type == Invoice.RETURN_SALE
                line_cogs = l.cogs_amount or Decimal('0')
                signed_cogs = -line_cogs if is_return else line_cogs
                records.append({
                    'number': l.invoice.invoice_number,
                    'invoice_id': l.invoice.id,
                    'url': get_inv_url(l.invoice.id),
                    'date': l.invoice.date,
                    'product_name': l.product.name if l.product else '—',
                    'barcode': l.product.barcode if l.product else '',
                    'quantity': -l.quantity if is_return else l.quantity,
                    'unit_price': l.unit_price,
                    'unit_cost': (line_cogs / l.quantity if l.quantity else Decimal('0')),
                    'cogs_amount': signed_cogs,
                    'partner': l.invoice.partner.name if l.invoice.partner else 'عميل نقدي',
                    'doc_type': 'مرتجع بيع' if is_return else 'فاتورة بيع',
                    'is_return': is_return
                })
            elif detail_type == 'expenses':
                it = item
                net_val = (it.debit or Decimal('0')) - (it.credit or Decimal('0'))
                records.append({
                    'number': it.entry.reference or f"#{it.entry.id}",
                    'entry_id': it.entry.id,
                    'url': get_entry_url(it.entry.id),
                    'date': it.entry.date,
                    'account_code': it.account.code,
                    'account_name': it.account.name,
                    'description': it.description or it.entry.description or '—',
                    'debit': it.debit or Decimal('0'),
                    'credit': it.credit or Decimal('0'),
                    'net_amount': net_val,
                })

        context.update({
            'title': title,
            'detail_type': detail_type,
            'account_code': account_code,
            'badge_type': badge_type,
            'date_from': date_from,
            'date_to': date_to,
            'records': records,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'records_count': paginator.count,
            'total_amount': total_amount,
        })
        return context



class ExpenseReportView(BranchRestrictedDenyMixin, CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/accounting_expenses.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import JournalItem, Account, JournalEntry, Voucher, Expense
        from django.utils import timezone
        import datetime
        from decimal import Decimal

        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')
        
        today = timezone.now().date()
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            except ValueError:
                date_from_obj = today.replace(day=1)
                date_from = date_from_obj.strftime('%Y-%m-%d')
            
        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()
            except ValueError:
                date_to_obj = today
                date_to = date_to_obj.strftime('%Y-%m-%d')

        # Query all posted journal items on expense accounts hierarchy (regardless of code digits)
        operating_expense_account_ids = _get_operating_expense_account_ids()
        expense_items = JournalItem.objects.filter(
            account_id__in=operating_expense_account_ids,
            entry__status=JournalEntry.POSTED,
            entry__date__range=[date_from_obj, date_to_obj]
        ).select_related('entry', 'account').prefetch_related('entry__items__account').order_by('-entry__date', '-entry__id')

        # Cache voucher and expense lookups for fast linking
        all_vouchers = {v.voucher_number: v.id for v in Voucher.objects.all().only('id', 'voucher_number')}
        all_expenses = {e.expense_number: e.id for e in Expense.objects.all().only('id', 'expense_number')}

        items = []
        accounts_map = {}

        for it in expense_items:
            net_amt = (it.debit or Decimal('0')) - (it.credit or Decimal('0'))
            if net_amt <= Decimal('0'):
                continue

            ref = it.entry.reference or ''
            
            # Determine source type and url
            if ref.startswith('PAY-'):
                source_type = 'سند صرف'
                v_id = all_vouchers.get(ref)
                url = f"/accounting/vouchers/{v_id}/" if v_id else None
            elif ref.startswith('EXP-'):
                source_type = 'مصروف مباشر'
                e_id = all_expenses.get(ref)
                url = f"/accounting/expenses/{e_id}/" if e_id else None
            elif ref.startswith('STK-') or ref.startswith('ADJ-'):
                source_type = 'تسوية مخزنية'
                url = None
            elif ref.startswith('JRN-'):
                source_type = 'قيد يومية'
                url = None
            else:
                source_type = 'حركة محاسبية'
                url = None

            # Detect contra (payment/funding) account from the credit side of the entry
            credit_items = [ci for ci in it.entry.items.all() if ci.credit > 0 and ci.account_id != it.account_id]
            payment_name = ', '.join([ci.account.name for ci in credit_items]) if credit_items else '-'

            items.append({
                'date': it.entry.date,
                'number': ref or f"JE-{it.entry.id}",
                'source_type': source_type,
                'branch_name': 'المركز الرئيسي',
                'expense_account_name': it.account.name,
                'expense_account_code': it.account.code,
                'payment_name': payment_name,
                'amount': net_amt,
                'vat_amount': Decimal('0'),
                'total_amount': net_amt,
                'description': it.description or it.entry.description or '',
                'url': url,
            })

        # Total Expense & Grouping calculated in database via SQL aggregation
        total_expenses = expense_items.aggregate(
            total=Coalesce(Sum(F('debit') - F('credit')), Decimal('0.00'))
        )['total']

        expenses_by_account = [
            {'expense_account__code': row['account__code'], 'expense_account__name': row['account__name'], 'total_amount': row['total_amount']}
            for row in expense_items.order_by().values('account__code', 'account__name').annotate(
                total_amount=Coalesce(Sum(F('debit') - F('credit')), Decimal('0.00'))
            ).filter(total_amount__gt=0).order_by('-total_amount')
        ]

        context.update({
            'date_from': date_from,
            'date_to': date_to,
            'expenses': items,
            'total_expenses': total_expenses,
            'expenses_by_account': expenses_by_account,
            'title': 'المصروفات التفصيلية'
        })
        return context



class TreasuryStatementReportView(CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/accounting_treasury.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import Treasury, BankAccount, EWallet, JournalItem, JournalEntry
        from django.db.models import Sum, F
        from django.utils import timezone
        import datetime
        from decimal import Decimal

        treasuries = list(_scope_treasuries(Treasury.objects.all(), self.request).select_related('branch'))
        if is_branch_restricted(self.request):
            # Bank accounts / e-wallets are company-level (no branch): hidden from branch staff.
            bank_accounts = []
            ewallets = []
        else:
            bank_accounts = list(BankAccount.objects.all())
            ewallets = list(EWallet.objects.all())

        # Auto-healing ensuring GL accounts exist
        for t in treasuries:
            t.ensure_account()
        for b in bank_accounts:
            b.ensure_account()
        for w in ewallets:
            w.ensure_account()

        selected_account_val = self.request.GET.get('account') or self.request.GET.get('treasury')
        
        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')
        
        today = timezone.now().date()
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            except ValueError:
                date_from_obj = today.replace(day=1)
                date_from = date_from_obj.strftime('%Y-%m-%d')
            
        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()
            except ValueError:
                date_to_obj = today
                date_to = date_to_obj.strftime('%Y-%m-%d')

        selected_obj = None
        statement_lines = []
        starting_balance = Decimal('0')
        total_in = Decimal('0')
        total_out = Decimal('0')
        ending_balance = Decimal('0')
        selected_account_type = ''
        val_str = ''

        if selected_account_val:
            val_str = str(selected_account_val).strip()
            if val_str.startswith('treasury_'):
                t_id = val_str.split('_')[1]
                selected_obj = next((t for t in treasuries if str(t.id) == str(t_id)), None)
                selected_account_type = 'خزينة نقدية'
            elif val_str.startswith('bank_'):
                b_id = val_str.split('_')[1]
                selected_obj = next((b for b in bank_accounts if str(b.id) == str(b_id)), None)
                selected_account_type = 'حساب بنكي'
            elif val_str.startswith('wallet_'):
                w_id = val_str.split('_')[1]
                selected_obj = next((w for w in ewallets if str(w.id) == str(w_id)), None)
                selected_account_type = 'محفظة إلكترونية'
            else:
                # Backward compatibility for plain integer ID
                selected_obj = next((t for t in treasuries if str(t.id) == str(val_str)), None)
                if selected_obj:
                    selected_account_type = 'خزينة نقدية'
                    val_str = f"treasury_{selected_obj.id}"
                else:
                    selected_obj = next((b for b in bank_accounts if str(b.id) == str(val_str)), None)
                    if selected_obj:
                        selected_account_type = 'حساب بنكي'
                        val_str = f"bank_{selected_obj.id}"
                    else:
                        selected_obj = next((w for w in ewallets if str(w.id) == str(val_str)), None)
                        if selected_obj:
                            selected_account_type = 'محفظة إلكترونية'
                            val_str = f"wallet_{selected_obj.id}"

            if selected_obj:
                account = selected_obj.ensure_account()
                if account:
                    starting_balance = getattr(selected_obj, 'opening_balance', Decimal('0')) or Decimal('0')
                    
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
                    
                    starting_balance += (prev_debit - prev_credit)
                    
                    period_items = JournalItem.objects.filter(
                        account=account,
                        entry__status=JournalEntry.POSTED,
                        entry__date__range=[date_from_obj, date_to_obj]
                    ).select_related('entry').order_by('entry__date', 'entry__id')
                    
                    current_bal = starting_balance
                    for item in period_items:
                        debit = item.debit or Decimal('0')
                        credit = item.credit or Decimal('0')
                        
                        total_in += debit
                        total_out += credit
                        current_bal += (debit - credit)
                        
                        statement_lines.append({
                            'date': item.entry.date,
                            'reference': item.entry.reference,
                            'description': item.description or item.entry.description,
                            'debit': debit,
                            'credit': credit,
                            'balance': current_bal
                        })
                        
                    ending_balance = current_bal
                    statement_lines.reverse()

        context.update({
            'treasuries': treasuries,
            'bank_accounts': bank_accounts,
            'ewallets': ewallets,
            'selected_obj': selected_obj,
            'selected_treasury': selected_obj,  # for backward compatibility
            'selected_account_val': val_str,
            'selected_account_type': selected_account_type,
            'date_from': date_from,
            'date_to': date_to,
            'starting_balance': starting_balance,
            'total_in': total_in,
            'total_out': total_out,
            'ending_balance': ending_balance,
            'statement_lines': statement_lines,
            'title': 'كشف حساب وحركات الخزن والبنوك والمحافظ'
        })
        return context


class LiquidityPositionReportView(CustomPermissionRequiredMixin, TemplateView):
    """
    تقرير موقف وأرصدة السيولة النقدية الشامل:
    يعرض أرصدة الخزن النقدية، الحسابات البنكية، والمحافظ الإلكترونية، وإجمالي السيولة المتاحة فوراً.
    """
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/liquidity_position.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
        from decimal import Decimal

        treasuries = list(_scope_treasuries(Treasury.objects.all(), self.request).select_related('branch'))
        if is_branch_restricted(self.request):
            # Bank accounts / e-wallets are company-level (no branch): hidden from branch staff.
            bank_accounts = []
            ewallets = []
        else:
            bank_accounts = list(BankAccount.objects.all())
            ewallets = list(EWallet.objects.all())

        for t in treasuries:
            t.ensure_account()
        for b in bank_accounts:
            b.ensure_account()
        for w in ewallets:
            w.ensure_account()

        # تجميع وحساب أرصدة كافة الخزن والحسابات دفعة واحدة في استعلام تجميعي واحد (SQL)
        from apps.tenant.services.journal_service import batch_get_account_balances
        all_accounts = [t.account for t in treasuries if t.account] + \
                       [b.account for b in bank_accounts if b.account] + \
                       [w.account for w in ewallets if w.account]
        batch_get_account_balances(account_qs=all_accounts)

        total_treasuries = sum((t.current_balance for t in treasuries), Decimal('0'))
        total_banks = sum((b.current_balance for b in bank_accounts), Decimal('0'))
        total_wallets = sum((w.current_balance for w in ewallets), Decimal('0'))
        grand_total_liquidity = total_treasuries + total_banks + total_wallets

        pos_treasuries = max(total_treasuries, Decimal('0'))
        pos_banks = max(total_banks, Decimal('0'))
        pos_wallets = max(total_wallets, Decimal('0'))
        total_positive = pos_treasuries + pos_banks + pos_wallets

        if total_positive > Decimal('0'):
            treasury_pct = round((pos_treasuries / total_positive) * 100, 1)
            bank_pct = round((pos_banks / total_positive) * 100, 1)
            wallet_pct = round((pos_wallets / total_positive) * 100, 1)
            is_deficit = False
        elif (abs(total_treasuries) + abs(total_banks) + abs(total_wallets)) > Decimal('0'):
            # In case accounts have negative balance (net deficit / overdraft share)
            total_abs = abs(total_treasuries) + abs(total_banks) + abs(total_wallets)
            treasury_pct = round((abs(total_treasuries) / total_abs) * 100, 1)
            bank_pct = round((abs(total_banks) / total_abs) * 100, 1)
            wallet_pct = round((abs(total_wallets) / total_abs) * 100, 1)
            is_deficit = True
        else:
            treasury_pct = bank_pct = wallet_pct = Decimal('0')
            is_deficit = False

        context.update({
            'title': 'تقرير موقف وأرصدة السيولة النقدية الشامل',
            'treasuries': treasuries,
            'bank_accounts': bank_accounts,
            'ewallets': ewallets,
            'total_treasuries': total_treasuries,
            'total_banks': total_banks,
            'total_wallets': total_wallets,
            'grand_total_liquidity': grand_total_liquidity,
            'treasury_pct': treasury_pct,
            'bank_pct': bank_pct,
            'wallet_pct': wallet_pct,
            'is_deficit': is_deficit,
        })
        return context


class CashVouchersReportView(CustomPermissionRequiredMixin, TemplateView):
    """
    تقرير المقبوضات والمدفوعات وسندات النقدية:
    تتبع سندات القبض والصرف، الإيرادات والمصروفات النقدية وصافي التدفق النقدي.
    """
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/liquidity_vouchers.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import Voucher, Treasury, BankAccount, EWallet
        from django.utils import timezone
        import datetime
        from decimal import Decimal

        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')
        voucher_type = self.request.GET.get('voucher_type')
        payment_method = self.request.GET.get('payment_method')
        account_filter = self.request.GET.get('account')

        today = timezone.now().date()
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            except ValueError:
                date_from_obj = today.replace(day=1)
                date_from = date_from_obj.strftime('%Y-%m-%d')

        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()
            except ValueError:
                date_to_obj = today
                date_to = date_to_obj.strftime('%Y-%m-%d')

        queryset = Voucher.objects.filter(
            date__range=[date_from_obj, date_to_obj]
        ).select_related('partner', 'account', 'treasury', 'bank_account', 'ewallet').order_by('-date', '-id')
        _branch = get_branch_scope(self.request)
        if is_branch_unassigned(self.request):
            queryset = queryset.none()
        elif _branch is not None:
            # Voucher.branch may be unset on legacy rows: fall back to the treasury's branch.
            queryset = queryset.filter(
                models.Q(branch=_branch) | models.Q(branch__isnull=True, treasury__branch=_branch)
            )

        if voucher_type:
            queryset = queryset.filter(voucher_type=voucher_type)

        if payment_method:
            queryset = queryset.filter(payment_method=payment_method)

        if account_filter:
            if account_filter.startswith('treasury_'):
                queryset = queryset.filter(treasury_id=account_filter.split('_')[1])
            elif account_filter.startswith('bank_'):
                queryset = queryset.filter(bank_account_id=account_filter.split('_')[1])
            elif account_filter.startswith('wallet_'):
                queryset = queryset.filter(ewallet_id=account_filter.split('_')[1])

        from django.db.models import Sum, Q, Count
        from django.db.models.functions import Coalesce

        v_agg = queryset.aggregate(
            total_receipts=Coalesce(Sum('amount', filter=Q(voucher_type=Voucher.RECEIPT)), Decimal('0.00')),
            total_payments=Coalesce(Sum('amount', filter=Q(voucher_type=Voucher.PAYMENT)), Decimal('0.00')),
        )
        total_receipts = v_agg['total_receipts']
        total_payments = v_agg['total_payments']
        net_flow = total_receipts - total_payments

        vouchers = list(queryset)

        treasuries = _scope_treasuries(Treasury.objects.all(), self.request)
        if is_branch_restricted(self.request):
            bank_accounts = BankAccount.objects.none()
            ewallets = EWallet.objects.none()
        else:
            bank_accounts = BankAccount.objects.all()
            ewallets = EWallet.objects.all()

        context.update({
            'title': 'تقرير المقبوضات والمدفوعات (سندات النقدية)',
            'vouchers': vouchers,
            'date_from': date_from,
            'date_to': date_to,
            'selected_voucher_type': voucher_type or '',
            'selected_payment_method': payment_method or '',
            'selected_account': account_filter or '',
            'total_receipts': total_receipts,
            'total_payments': total_payments,
            'net_flow': net_flow,
            'treasuries': treasuries,
            'bank_accounts': bank_accounts,
            'ewallets': ewallets,
        })
        return context


class InternalTransfersReportView(CustomPermissionRequiredMixin, TemplateView):
    """
    تقرير التحويلات النقدية بين الحسابات:
    تتبع حركة نقل الأموال بين الخزن والبنوك والمحافظ الإلكترونية وحالات الترحيل.
    """
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/liquidity_transfers.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import InternalTransfer
        from django.utils import timezone
        import datetime
        from decimal import Decimal

        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')
        status = self.request.GET.get('status')

        today = timezone.now().date()
        if not date_from:
            date_from_obj = today.replace(day=1)
            date_from = date_from_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_from_obj = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
            except ValueError:
                date_from_obj = today.replace(day=1)
                date_from = date_from_obj.strftime('%Y-%m-%d')

        if not date_to:
            date_to_obj = today
            date_to = date_to_obj.strftime('%Y-%m-%d')
        else:
            try:
                date_to_obj = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()
            except ValueError:
                date_to_obj = today
                date_to = date_to_obj.strftime('%Y-%m-%d')

        queryset = InternalTransfer.objects.filter(
            date__range=[date_from_obj, date_to_obj]
        ).select_related(
            'from_treasury', 'from_bank_account', 'from_ewallet',
            'to_treasury', 'to_bank_account', 'to_ewallet',
            'created_by'
        ).order_by('-date', '-id')

        _branch = get_branch_scope(self.request)
        if is_branch_unassigned(self.request):
            queryset = queryset.none()
        elif _branch is not None:
            queryset = queryset.filter(models.Q(from_treasury__branch=_branch) | models.Q(to_treasury__branch=_branch))

        if status:
            queryset = queryset.filter(status=status)

        from django.db.models import Sum, Count, Q
        from django.db.models.functions import Coalesce

        agg = queryset.aggregate(
            total_amount=Coalesce(Sum('amount'), Decimal('0.00')),
            confirmed_count=Count('id', filter=Q(status=InternalTransfer.POSTED)),
            draft_count=Count('id', filter=Q(status=InternalTransfer.DRAFT)),
        )
        total_amount = agg['total_amount']
        confirmed_count = agg['confirmed_count']
        draft_count = agg['draft_count']

        transfers = list(queryset)

        context.update({
            'title': 'تقرير التحويلات النقدية بين الحسابات',
            'transfers': transfers,
            'date_from': date_from,
            'date_to': date_to,
            'selected_status': status or '',
            'total_amount': total_amount,
            'confirmed_count': confirmed_count,
            'draft_count': draft_count,
        })
        return context



def get_pos_session_details(session):
    from apps.tenant.invoicing.models import Invoice
    from apps.tenant.accounting.models import Voucher
    from decimal import Decimal
    from django.db.models import Sum, Count, Q, F, Case, When
    from django.db.models.functions import Coalesce

    posted_invoices = Invoice.objects.filter(
        pos_session=session,
        status=Invoice.POSTED
    ).select_related('partner', 'branch', 'warehouse').order_by('-created_at')

    # احتساب إجماليات الفواتير وطرق الدفع مباشرة في قاعدة البيانات بـ SQL Aggregation
    inv_agg = posted_invoices.aggregate(
        total_gross_sales=Coalesce(Sum('total_amount', filter=Q(invoice_type=Invoice.SALE)), Decimal('0.00')),
        total_returns_amount=Coalesce(Sum('total_amount', filter=Q(invoice_type=Invoice.RETURN_SALE)), Decimal('0.00')),
        sales_count=Count('id', filter=Q(invoice_type=Invoice.SALE)),
        returns_count=Count('id', filter=Q(invoice_type=Invoice.RETURN_SALE)),
        total_cash_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_cash')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.CASH), then=F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_cash_refunds=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=F('paid_cash')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.CASH), then=F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_card_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_card')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.CARD), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_card')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.CARD), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_ewallet_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_ewallet')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.EWALLET), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_ewallet')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.EWALLET), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_bank_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_bank')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.BANK_TRANSFER), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_bank')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.BANK_TRANSFER), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
        total_credit_sales=Coalesce(Sum(
            Case(
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.SPLIT), then=F('paid_credit')),
                When(Q(invoice_type=Invoice.SALE, payment_type=Invoice.CREDIT), then=F('total_amount')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.SPLIT), then=-F('paid_credit')),
                When(Q(invoice_type=Invoice.RETURN_SALE, payment_type=Invoice.CREDIT), then=-F('total_amount')),
                default=Decimal('0.00'),
                output_field=models.DecimalField(max_digits=18, decimal_places=4)
            )
        ), Decimal('0.00')),
    )

    total_gross_sales = inv_agg['total_gross_sales']
    total_returns_amount = inv_agg['total_returns_amount']
    net_sales_amount = total_gross_sales - total_returns_amount
    total_cash_sales = inv_agg['total_cash_sales']
    total_cash_refunds = inv_agg['total_cash_refunds']
    total_card_sales = inv_agg['total_card_sales']
    total_ewallet_sales = inv_agg['total_ewallet_sales']
    total_bank_sales = inv_agg['total_bank_sales']
    total_credit_sales = inv_agg['total_credit_sales']

    v_base = Voucher.objects.filter(status=Voucher.POSTED).filter(
        Q(payment_method=Voucher.CASH) | Q(payment_method=Voucher.SPLIT, paid_cash__gt=0)
    )
    time_filter = Q(treasury=session.treasury, created_at__gte=session.start_time)
    if session.end_time:
        time_filter &= Q(created_at__lte=session.end_time)

    shift_vouchers = v_base.filter(
        Q(pos_session=session) | (Q(journal_entry__created_by=session.user) & time_filter)
    ).distinct().select_related('partner').order_by('-created_at')

    # احتساب المقبوضات والمدفوعات النقدية في قاعدة البيانات مباشرة
    v_cash_expr = Case(
        When(payment_method=Voucher.SPLIT, then=F('paid_cash')),
        default=F('amount'),
        output_field=models.DecimalField(max_digits=18, decimal_places=4)
    )
    v_agg = shift_vouchers.aggregate(
        total_cash_receipts=Coalesce(Sum(v_cash_expr, filter=Q(voucher_type=Voucher.RECEIPT)), Decimal('0.00')),
        total_cash_payments=Coalesce(Sum(v_cash_expr, filter=Q(voucher_type=Voucher.PAYMENT)), Decimal('0.00')),
        receipts_count=Count('id', filter=Q(voucher_type=Voucher.RECEIPT)),
        payments_count=Count('id', filter=Q(voucher_type=Voucher.PAYMENT)),
    )
    total_cash_receipts = v_agg['total_cash_receipts']
    total_cash_payments = v_agg['total_cash_payments']

    # Rentals issued during the shift
    from apps.tenant.rentals.models import Rental
    rental_time_filter = Q(created_at__gte=session.start_time)
    if session.end_time:
        rental_time_filter &= Q(created_at__lte=session.end_time)

    shift_rentals = Rental.objects.filter(
        Q(pos_session=session) | (Q(created_by=session.user) & rental_time_filter)
    ).distinct().select_related('equipment', 'customer').order_by('-created_at')

    rental_agg = shift_rentals.aggregate(
        total_rentals_amount=Coalesce(Sum('total_amount'), Decimal('0.00')),
        total_cash_rentals=Coalesce(Sum('total_amount', filter=Q(payment_method=Rental.PAYMENT_CASH)), Decimal('0.00')),
        total_credit_rentals=Coalesce(Sum('total_amount', filter=~Q(payment_method=Rental.PAYMENT_CASH)), Decimal('0.00')),
        rentals_count=Count('id'),
    )
    total_rentals_amount = rental_agg['total_rentals_amount']
    total_cash_rentals = rental_agg['total_cash_rentals']
    total_credit_rentals = rental_agg['total_credit_rentals']

    expected_balance = (
        session.opening_balance +
        total_cash_sales -
        total_cash_refunds +
        total_cash_receipts -
        total_cash_payments +
        total_cash_rentals
    )

    actual_balance = session.closing_balance_actual if session.status == 'CLOSED' else expected_balance
    difference = session.difference if session.status == 'CLOSED' else (actual_balance - expected_balance)

    return {
        'session': session,
        'posted_invoices': posted_invoices,
        'sales_count': inv_agg['sales_count'],
        'returns_count': inv_agg['returns_count'],
        'total_gross_sales': total_gross_sales,
        'total_returns_amount': total_returns_amount,
        'net_sales_amount': net_sales_amount,
        'total_cash_sales': total_cash_sales,
        'total_cash_refunds': total_cash_refunds,
        'total_card_sales': total_card_sales,
        'total_ewallet_sales': total_ewallet_sales,
        'total_bank_sales': total_bank_sales,
        'total_credit_sales': total_credit_sales,
        'shift_vouchers': shift_vouchers,
        'total_cash_receipts': total_cash_receipts,
        'total_cash_payments': total_cash_payments,
        'shift_rentals': shift_rentals,
        'rentals_count': rental_agg['rentals_count'],
        'total_rentals_amount': total_rentals_amount,
        'total_cash_rentals': total_cash_rentals,
        'total_credit_rentals': total_credit_rentals,
        'expected_balance': expected_balance,
        'actual_balance': actual_balance,
        'difference': difference,
    }


class ShiftReportView(CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/sales_shifts.html'

    DEFAULT_RANGE_DAYS = 31
    MAX_RANGE_DAYS = 92
    SESSIONS_PER_PAGE = 50
    EXPORT_MAX_SESSIONS = 500

    def _parse_date(self, value, label):
        import datetime
        if not value:
            return None
        try:
            return datetime.datetime.strptime(value, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            messages.warning(self.request, f'{label} غير صالح، تم استخدام القيمة الافتراضية.')
            return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.invoicing.models import POSSession
        from django.contrib.auth import get_user_model
        from django.core.paginator import Paginator
        from django.db.models import Q
        from django.utils import timezone
        import datetime
        from decimal import Decimal

        User = get_user_model()
        # auth_user is global across tenants: only list users that belong to
        # this tenant (have an Employee profile), and only the branch's staff
        # for branch-scoped viewers.
        users = User.objects.filter(employee_profile__isnull=False)
        branch = get_branch_scope(self.request)
        if is_branch_unassigned(self.request):
            users = users.filter(pk=self.request.user.pk)
        elif branch is not None:
            users = users.filter(employee_profile__branch=branch)
        users = users.order_by('first_name', 'username')

        selected_user_id = self.request.GET.get('user') or ''
        if selected_user_id and not str(selected_user_id).isdigit():
            selected_user_id = ''

        today = timezone.now().date()
        date_to_obj = self._parse_date(self.request.GET.get('date_to'), 'تاريخ النهاية') or today
        date_from_obj = self._parse_date(self.request.GET.get('date_from'), 'تاريخ البداية')             or (date_to_obj - datetime.timedelta(days=self.DEFAULT_RANGE_DAYS))
        if date_from_obj > date_to_obj:
            date_from_obj, date_to_obj = date_to_obj, date_from_obj
        if (date_to_obj - date_from_obj).days > self.MAX_RANGE_DAYS:
            date_from_obj = date_to_obj - datetime.timedelta(days=self.MAX_RANGE_DAYS)
            messages.warning(
                self.request,
                f'أقصى مدة مسموح بها للتقرير هي {self.MAX_RANGE_DAYS} يوماً، تم ضبط تاريخ البداية تلقائياً.'
            )
        date_from = date_from_obj.strftime('%Y-%m-%d')
        date_to = date_to_obj.strftime('%Y-%m-%d')

        sessions_qs = POSSession.objects.filter(
            Q(start_time__date__gte=date_from_obj, start_time__date__lte=date_to_obj) |
            Q(end_time__date__gte=date_from_obj, end_time__date__lte=date_to_obj) |
            Q(status=POSSession.OPEN)
        ).select_related('user', 'branch', 'treasury').order_by('-start_time').distinct()
        sessions_qs = filter_by_branch(sessions_qs, self.request)

        if selected_user_id:
            sessions_qs = sessions_qs.filter(user_id=selected_user_id)

        exporting = self.request.GET.get('export') == 'excel'
        if exporting:
            page_obj = None
            page_sessions = list(sessions_qs[:self.EXPORT_MAX_SESSIONS])
        else:
            paginator = Paginator(sessions_qs, self.SESSIONS_PER_PAGE)
            page_obj = paginator.get_page(self.request.GET.get('page', 1))
            page_sessions = list(page_obj.object_list)

        sessions_data = [get_pos_session_details(session) for session in page_sessions]

        # Grand totals over the whole filtered range (not just the current page).
        # Closed shifts use the figures stored when the shift was closed (one
        # aggregate query); open shifts (few) are computed live.
        total_expected = Decimal('0')
        total_actual = Decimal('0')
        total_diff = Decimal('0')
        closed_agg = sessions_qs.filter(status=POSSession.CLOSED).order_by().aggregate(
            exp=Coalesce(Sum('closing_balance_expected'), Decimal('0')),
            act=Coalesce(Sum('closing_balance_actual'), Decimal('0')),
            diff=Coalesce(Sum('difference'), Decimal('0')),
        )
        total_expected += closed_agg['exp']
        total_actual += closed_agg['act']
        total_diff += closed_agg['diff']
        computed = {dt['session'].id: dt for dt in sessions_data}
        for session in sessions_qs.filter(status=POSSession.OPEN):
            dt = computed.get(session.id) or get_pos_session_details(session)
            total_expected += dt['expected_balance']
            total_actual += dt['actual_balance']
            total_diff += dt['difference']

        context.update({
            'date_from': date_from,
            'date_to': date_to,
            'users': users,
            'selected_user_id': selected_user_id,
            'sessions_data': sessions_data,
            'page_obj': page_obj,
            'is_paginated': bool(page_obj and page_obj.has_other_pages()),
            'total_expected': total_expected,
            'total_actual': total_actual,
            'total_diff': total_diff,
            'title': 'يومية الكاشير (الورديات)'
        })
        return context

    def get(self, request, *args, **kwargs):
        if request.GET.get('export') == 'excel':
            context = self.get_context_data(**kwargs)
            sessions_data = context.get('sessions_data', [])

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "يومية الورديات"
            ws.views.sheetView[0].rightToLeft = True

            # Styles
            title_font = Font(name='Calibri', size=14, bold=True, color='1F4E79')
            header_font = Font(name='Calibri', size=11, bold=True, color='FFFFFF')
            header_fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
            summary_fill = PatternFill(start_color='F2F4F8', end_color='F2F4F8', fill_type='solid')
            summary_font = Font(name='Calibri', size=11, bold=True)
            data_font = Font(name='Calibri', size=10)
            align_center = Alignment(horizontal='center', vertical='center')
            align_right = Alignment(horizontal='right', vertical='center')
            thin_border = Border(
                left=Side(style='thin', color='D9D9D9'),
                right=Side(style='thin', color='D9D9D9'),
                top=Side(style='thin', color='D9D9D9'),
                bottom=Side(style='thin', color='D9D9D9')
            )

            # Title
            ws.merge_cells('A1:M1')
            ws['A1'] = f"تقرير يومية الكاشير (الورديات) — الفترة من {context['date_from']} إلى {context['date_to']}"
            ws['A1'].font = title_font
            ws['A1'].alignment = align_center

            def to_dec(val):
                if val is None:
                    return Decimal('0.00')
                return Decimal(str(val)).quantize(Decimal('0.01'))

            # KPI Summary
            ws.append([])
            ws.append([
                "إجمالي النقدية المتوقعة بالدرج", to_dec(context.get('total_expected')),
                "إجمالي النقدية الفعلية (المسلمة)", to_dec(context.get('total_actual')),
                "إجمالي العجز / الزيادة", to_dec(context.get('total_diff'))
            ])
            for col in range(1, 7):
                cell = ws.cell(row=3, column=col)
                cell.font = summary_font
                cell.fill = summary_fill
                cell.border = thin_border
                cell.alignment = align_center

            ws.append([])

            # Headers
            headers = [
                'رقم الوردية', 'تاريخ الفتح', 'تاريخ الإغلاق', 'الكاشير', 'الخزنة',
                'صافي المبيعات', 'مبيعات كاش', 'إلكتروني/شبكة', 'عهدة افتتاحية',
                'متوقع بالدرج', 'فعلي مسلّم', 'العجز / الزيادة', 'الحالة'
            ]
            ws.append(headers)
            header_row_idx = ws.max_row
            for col_idx, h in enumerate(headers, 1):
                cell = ws.cell(row=header_row_idx, column=col_idx)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = align_center
                cell.border = thin_border

            # Data rows
            for item in sessions_data:
                s = item['session']
                status_str = 'مفتوحة' if s.status == 'OPEN' else 'مغلقة'
                start_str = s.start_time.strftime('%Y-%m-%d %H:%M') if s.start_time else ''
                end_str = s.end_time.strftime('%Y-%m-%d %H:%M') if s.end_time else '—'
                digital_sales = to_dec(item.get('total_card_sales')) + to_dec(item.get('total_ewallet_sales')) + to_dec(item.get('total_bank_sales'))
                row = [
                    f"#{s.id}",
                    start_str,
                    end_str,
                    s.user.get_full_name() or display_username(s.user),
                    s.treasury.name if s.treasury else '',
                    to_dec(item.get('net_sales_amount')),
                    to_dec(item.get('total_cash_sales')),
                    digital_sales,
                    to_dec(s.opening_balance),
                    to_dec(item.get('expected_balance')),
                    to_dec(item.get('actual_balance')),
                    to_dec(item.get('difference')),
                    status_str
                ]
                ws.append(row)
                r_idx = ws.max_row
                for col_idx in range(1, len(row) + 1):
                    cell = ws.cell(row=r_idx, column=col_idx)
                    cell.font = data_font
                    cell.border = thin_border
                    if col_idx in [6, 7, 8, 9, 10, 11, 12]:
                        cell.number_format = '#,##0.00'
                        cell.alignment = align_right
                    else:
                        cell.alignment = align_center

            for col in ws.columns:
                max_len = 0
                col_letter = get_column_letter(col[0].column)
                for cell in col:
                    val_str = str(cell.value or '')
                    if len(val_str) > max_len:
                        max_len = len(val_str)
                ws.column_dimensions[col_letter].width = max(max_len + 4, 13)

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            resp = HttpResponse(
                output.getvalue(),
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            resp['Content-Disposition'] = f'attachment; filename="cashier_shifts_{context["date_from"]}_{context["date_to"]}.xlsx"'
            return resp

        return super().get(request, *args, **kwargs)


class ShiftDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'core.view_sales_reports'
    model = POSSession
    template_name = 'reports/pos_session_detail.html'
    context_object_name = 'session_obj'

    def get_queryset(self):
        # Non-admins may only open shifts of their own branch (others -> 404).
        return filter_by_branch(POSSession.objects.all(), self.request)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        details = get_pos_session_details(self.object)
        context['details'] = details
        context['title'] = f"تقرير مفصل للوردية #{self.object.id}"
        return context


from django.contrib.auth.decorators import login_required
from apps.tenant.core.decorators import custom_permission_required

@login_required
@custom_permission_required('core.view_sales_reports', redirect_url='core:report_shifts')
def shift_detail_api(request, pk):
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from apps.tenant.invoicing.models import POSSession

    session = get_object_or_404(filter_by_branch(POSSession.objects.all(), request), pk=pk)
    dt = get_pos_session_details(session)

    invoices_json = [
        {
            'id': inv.id,
            'number': inv.invoice_number,
            'time': inv.created_at.strftime('%H:%M:%S') if inv.created_at else '',

            'type': inv.get_invoice_type_display(),
            'type_code': inv.invoice_type,
            'customer': inv.partner.name if inv.partner else 'عميل نقدي',
            'payment_type': inv.get_payment_type_display(),
            'total': float(inv.total_amount)
        } for inv in dt['posted_invoices']
    ]

    vouchers_json = [
        {
            'id': v.id,
            'number': v.voucher_number,
            'time': v.created_at.strftime('%H:%M:%S') if v.created_at else '',
            'type': v.get_voucher_type_display(),
            'type_code': v.voucher_type,
            'partner': v.partner.name if v.partner else 'سند كاش',
            'notes': v.description or '-',
            'amount': float(v.amount)

        } for v in dt['shift_vouchers']
    ]

    data = {
        'id': session.id,
        'user': session.user.get_full_name() or display_username(session.user),
        'treasury': session.treasury.name if session.treasury else '-',
        'branch': session.branch.name if session.branch else '-',
        'start_time': session.start_time.strftime('%Y-%m-%d %H:%M') if session.start_time else '',
        'end_time': session.end_time.strftime('%Y-%m-%d %H:%M') if session.end_time else 'مفتوحة حالياً',
        'status': session.get_status_display(),
        'status_code': session.status,
        'notes': session.notes or '',
        'opening_balance': float(session.opening_balance),
        'sales_count': dt['sales_count'],
        'returns_count': dt['returns_count'],
        'net_sales_amount': float(dt['net_sales_amount']),
        'total_cash_sales': float(dt['total_cash_sales']),
        'total_cash_refunds': float(dt['total_cash_refunds']),
        'total_card_sales': float(dt['total_card_sales']),
        'total_ewallet_sales': float(dt['total_ewallet_sales']),
        'total_bank_sales': float(dt['total_bank_sales']),
        'total_credit_sales': float(dt['total_credit_sales']),
        'total_cash_receipts': float(dt['total_cash_receipts']),
        'total_cash_payments': float(dt['total_cash_payments']),
        'expected_balance': float(dt['expected_balance']),
        'actual_balance': float(dt['actual_balance']),
        'difference': float(dt['difference']),
        'invoices': invoices_json,
        'vouchers': vouchers_json
    }
    return JsonResponse(data)

class TrialBalanceReportView(BranchRestrictedDenyMixin, CsvExportMixin, CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/trial_balance.html'
    csv_filename = "trial_balance_report.csv"

    def get_csv_headers(self):
        return ['كود الحساب', 'اسم الحساب', 'النوع', 'إجمالي مدين', 'إجمالي دائن', 'رصيد مدين', 'رصيد دائن']

    def get_csv_row(self, obj):
        return [
            obj['code'],
            obj['name'],
            obj['type'],
            obj['debit'],
            obj['credit'],
            obj['net_debit'],
            obj['net_credit']
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')

        from apps.tenant.accounting.models import Account, JournalItem
        from django.db.models import Sum

        items = JournalItem.objects.filter(entry__status='POSTED')
        if start_date:
            items = items.filter(entry__date__gte=start_date)
        if end_date:
            items = items.filter(entry__date__lte=end_date)

        acc_balances = items.values(
            'account__id', 'account__code', 'account__name', 'account__account_type'
        ).annotate(
            total_debit=Sum('debit'),
            total_credit=Sum('credit')
        ).order_by('account__code')

        accounts_list = []
        total_debit = Decimal('0')
        total_credit = Decimal('0')

        for ab in acc_balances:
            td = ab['total_debit'] or Decimal('0')
            tc = ab['total_credit'] or Decimal('0')
            
            if ab['account__account_type'] in ['ASSET', 'EXPENSE']:
                net = td - tc
                net_debit = net if net > 0 else Decimal('0')
                net_credit = abs(net) if net < 0 else Decimal('0')
            else:
                net = tc - td
                net_credit = net if net > 0 else Decimal('0')
                net_debit = abs(net) if net < 0 else Decimal('0')

            if net_debit > 0 or net_credit > 0 or td > 0 or tc > 0:
                accounts_list.append({
                    'code': ab['account__code'],
                    'name': ab['account__name'],
                    'type': dict(Account.ACCOUNT_TYPES).get(ab['account__account_type'], ab['account__account_type']),
                    'debit': td,
                    'credit': tc,
                    'net_debit': net_debit,
                    'net_credit': net_credit
                })
                total_debit += td
                total_credit += tc

        context['accounts'] = accounts_list
        context['total_debit'] = total_debit
        context['total_credit'] = total_credit
        context['total_net_debit'] = sum(a['net_debit'] for a in accounts_list)
        context['total_net_credit'] = sum(a['net_credit'] for a in accounts_list)
        context['start_date'] = start_date
        context['end_date'] = end_date
        context['title'] = 'ميزان المراجعة'
        
        # for CSV export
        context['object_list'] = accounts_list

        return context

class GeneralLedgerReportView(BranchRestrictedDenyMixin, CsvExportMixin, CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/general_ledger.html'
    csv_filename = "general_ledger_report.csv"
    context_object_name = 'transactions'

    def get_csv_headers(self):
        return ['التاريخ', 'رقم القيد', 'البيان', 'مدين', 'دائن', 'الرصيد التراكمي']

    def get_csv_row(self, obj):
        return [
            obj['date'].strftime('%Y-%m-%d') if hasattr(obj['date'], 'strftime') else obj['date'],
            obj['reference'],
            obj['description'],
            obj['debit'],
            obj['credit'],
            obj['balance']
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import Account, JournalItem
        from django.db.models import Sum

        account_id = self.request.GET.get('account_id')
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')

        context['accounts_list'] = Account.objects.all().order_by('code')
        context['start_date'] = start_date
        context['end_date'] = end_date
        context['account_id'] = account_id

        if account_id and account_id.isdigit():
            try:
                account = Account.objects.get(id=account_id)
                context['selected_account'] = account
                
                opening_balance = Decimal('0')
                items = JournalItem.objects.filter(account=account, entry__status='POSTED')
                
                if start_date:
                    opening_items = items.filter(entry__date__lt=start_date)
                    od = opening_items.aggregate(s=Sum('debit'))['s'] or Decimal('0')
                    oc = opening_items.aggregate(s=Sum('credit'))['s'] or Decimal('0')
                    
                    if account.normal_balance == 'DEBIT':
                        opening_balance = od - oc
                    else:
                        opening_balance = oc - od
                        
                context['opening_balance'] = opening_balance
                
                period_items = items
                if start_date:
                    period_items = period_items.filter(entry__date__gte=start_date)
                if end_date:
                    period_items = period_items.filter(entry__date__lte=end_date)
                    
                period_items = period_items.select_related('entry').order_by('entry__date', 'entry__created_at')
                
                transactions = []
                running_balance = opening_balance
                total_debit = Decimal('0')
                total_credit = Decimal('0')
                
                for item in period_items:
                    debit = item.debit
                    credit = item.credit
                    
                    if account.normal_balance == 'DEBIT':
                        running_balance += debit
                        running_balance -= credit
                    else:
                        running_balance += credit
                        running_balance -= debit
                        
                    transactions.append({
                        'date': item.entry.date,
                        'reference': item.entry.reference,
                        'description': item.description or item.entry.description,
                        'debit': debit,
                        'credit': credit,
                        'balance': running_balance
                    })
                    
                    total_debit += debit
                    total_credit += credit
                    
                transactions.reverse()
                context['transactions'] = transactions
                context['total_debit'] = total_debit
                context['total_credit'] = total_credit
                context['final_balance'] = running_balance
                context['title'] = f'دفتر الأستاذ - {account.name}'
                context['object_list'] = transactions
                
            except Account.DoesNotExist:
                pass

        return context

class TaxReportView(CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/tax_report.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')

        from apps.tenant.invoicing.models import Invoice
        from apps.tenant.accounting.models import Expense
        from django.db.models import Sum
        from decimal import Decimal

        sales = Invoice.objects.filter(invoice_type=Invoice.SALE, status=Invoice.POSTED)
        returns_sales = Invoice.objects.filter(invoice_type=Invoice.RETURN_SALE, status=Invoice.POSTED)
        purchases = Invoice.objects.filter(invoice_type=Invoice.PURCHASE, status=Invoice.POSTED)
        returns_purchases = Invoice.objects.filter(invoice_type=Invoice.RETURN_PURCHASE, status=Invoice.POSTED)
        expenses = Expense.objects.filter(status=Expense.POSTED)
        sales = filter_by_branch(sales, self.request)
        returns_sales = filter_by_branch(returns_sales, self.request)
        purchases = filter_by_branch(purchases, self.request)
        returns_purchases = filter_by_branch(returns_purchases, self.request)
        expenses = filter_by_branch(expenses, self.request)

        if start_date:
            sales = sales.filter(date__gte=start_date)
            returns_sales = returns_sales.filter(date__gte=start_date)
            purchases = purchases.filter(date__gte=start_date)
            returns_purchases = returns_purchases.filter(date__gte=start_date)
            expenses = expenses.filter(date__gte=start_date)

        if end_date:
            sales = sales.filter(date__lte=end_date)
            returns_sales = returns_sales.filter(date__lte=end_date)
            purchases = purchases.filter(date__lte=end_date)
            returns_purchases = returns_purchases.filter(date__lte=end_date)
            expenses = expenses.filter(date__lte=end_date)

        # Output Tax (المبيعات)
        sales_tax = sales.aggregate(s=Sum('tax_amount'))['s'] or Decimal('0')
        returns_sales_tax = returns_sales.aggregate(s=Sum('tax_amount'))['s'] or Decimal('0')
        net_output_tax = sales_tax - returns_sales_tax

        # Input Tax (المشتريات والمصروفات)
        purchases_tax = purchases.aggregate(s=Sum('tax_amount'))['s'] or Decimal('0')
        returns_purchases_tax = returns_purchases.aggregate(s=Sum('tax_amount'))['s'] or Decimal('0')
        expenses_tax = expenses.aggregate(s=Sum('vat_amount'))['s'] or Decimal('0')
        net_input_tax = (purchases_tax - returns_purchases_tax) + expenses_tax

        # Withholding Tax
        # WHT on Sales (خصم واضافة مخصوم علينا من العملاء)
        sales_wht = sales.aggregate(s=Sum('wht_amount'))['s'] or Decimal('0')
        returns_sales_wht = returns_sales.aggregate(s=Sum('wht_amount'))['s'] or Decimal('0')
        net_sales_wht = sales_wht - returns_sales_wht

        # WHT on Purchases/Expenses (خصم واضافة مخصوم للغير - موردين)
        purchases_wht = purchases.aggregate(s=Sum('wht_amount'))['s'] or Decimal('0')
        returns_purchases_wht = returns_purchases.aggregate(s=Sum('wht_amount'))['s'] or Decimal('0')
        expenses_wht = expenses.aggregate(s=Sum('withholding_tax_amount'))['s'] or Decimal('0')
        net_purchases_wht = (purchases_wht - returns_purchases_wht) + expenses_wht

        # Net Payable VAT (الضريبة المستحقة) = Output - Input
        net_vat_payable = net_output_tax - net_input_tax

        context.update({
            'start_date': start_date,
            'end_date': end_date,
            'sales_tax': sales_tax,
            'returns_sales_tax': returns_sales_tax,
            'net_output_tax': net_output_tax,
            
            'purchases_tax': purchases_tax,
            'returns_purchases_tax': returns_purchases_tax,
            'expenses_tax': expenses_tax,
            'net_input_tax': net_input_tax,
            
            'net_vat_payable': net_vat_payable,

            'sales_wht': sales_wht,
            'returns_sales_wht': returns_sales_wht,
            'net_sales_wht': net_sales_wht,
            
            'purchases_wht': purchases_wht,
            'returns_purchases_wht': returns_purchases_wht,
            'expenses_wht': expenses_wht,
            'net_purchases_wht': net_purchases_wht,
            'title': 'تقرير الضرائب (الإقرار الضريبي)'
        })
        return context



from apps.tenant.einvoicing.models import EInvoiceLog

class EInvoicingReportView(CustomPermissionRequiredMixin, TemplateView):
    permission_required = 'core.view_sales_reports'
    template_name = 'reports/einvoicing_report.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        start_date = _date_param(self.request, 'start_date')
        end_date = _date_param(self.request, 'end_date')
        
        logs = EInvoiceLog.objects.filter(status='VALID').select_related('invoice')
        logs = filter_by_branch(logs, self.request, 'invoice__branch')
        
        if start_date:
            logs = logs.filter(invoice__date__gte=start_date)
        if end_date:
            logs = logs.filter(invoice__date__lte=end_date)
            
        logs = logs.order_by('-invoice__date', '-created_at')
        
        agg = logs.aggregate(
            total_invoices=Count('id'),
            total_amount=Coalesce(Sum('invoice__subtotal'), Decimal('0.00')),
            total_vat=Coalesce(Sum('invoice__tax_amount'), Decimal('0.00')),
            total_wht=Coalesce(Sum('invoice__wht_amount'), Decimal('0.00')),
            total_net=Coalesce(Sum('invoice__total_amount'), Decimal('0.00')),
        )
        total_invoices = agg['total_invoices']
        total_amount = agg['total_amount']
        total_vat = agg['total_vat']
        total_wht = agg['total_wht']
        total_net = agg['total_net']
        
        context.update({
            'start_date': start_date,
            'end_date': end_date,
            'logs': logs,
            'total_invoices': total_invoices,
            'total_amount': total_amount,
            'total_vat': total_vat,
            'total_wht': total_wht,
            'total_net': total_net,
            'title': 'تقرير الفواتير الإلكترونية المعتمدة'
        })
        return context


@login_required
@custom_permission_required('core.view_sales_reports', redirect_url='core:report_shifts')
def export_shift_detail_excel(request, pk):
    """Exports a comprehensive Excel report (.xlsx) for a POS Session / Shift."""
    from decimal import Decimal
    from django.http import HttpResponse
    from django.shortcuts import get_object_or_404
    from apps.tenant.invoicing.models import POSSession

    session = get_object_or_404(filter_by_branch(POSSession.objects.all(), request), pk=pk)
    dt = get_pos_session_details(session)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"وردية #{session.id}"
    ws.views.sheetView[0].rightToLeft = True

    # Formatting styles
    title_font = Font(name='Calibri', size=15, bold=True, color='1F4E79')
    header_font = Font(name='Calibri', size=11, bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
    section_fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')
    section_font = Font(name='Calibri', size=12, bold=True, color='1F4E79')
    total_fill = PatternFill(start_color='F2F2F2', end_color='F2F2F2', fill_type='solid')
    data_font = Font(name='Calibri', size=11)
    bold_font = Font(name='Calibri', size=11, bold=True)
    align_center = Alignment(horizontal='center', vertical='center')
    align_right = Alignment(horizontal='right', vertical='center')
    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )

    def to_dec(val):
        if val is None:
            return Decimal('0.00')
        return Decimal(str(val)).quantize(Decimal('0.01'))

    # 1. Main Title
    ws.merge_cells('A1:F1')
    cell_title = ws['A1']
    cell_title.value = f"تقرير تسوية وتقفيل الوردية رقم #{session.id} - مسار ERP"
    cell_title.font = title_font
    cell_title.alignment = align_center

    # 2. Metadata Section
    metadata = [
        ["الكاشير المسؤول", session.user.get_full_name() or display_username(session.user), "الخزينة المعتمدة", session.treasury.name if session.treasury else '-'],
        ["الفرع", session.branch.name if session.branch else '-', "حالة الوردية", session.get_status_display()],
        ["تاريخ ووقت الفتح", session.start_time.strftime('%Y-%m-%d %H:%M') if session.start_time else '-', "تاريخ ووقت الإغلاق", session.end_time.strftime('%Y-%m-%d %H:%M') if session.end_time else 'مفتوحة حالياً'],
    ]
    if session.notes:
        metadata.append(["ملاحظات الإغلاق", session.notes, "", ""])

    for row in metadata:
        ws.append(row)
        for col_idx in range(1, len(row) + 1):
            cell = ws.cell(row=ws.max_row, column=col_idx)
            cell.font = bold_font if col_idx % 2 != 0 else data_font
            cell.border = thin_border
            cell.alignment = align_right

    ws.append([])

    # 3. Cash Reconciliation Table
    ws.append(["💵 مطابقة وحركة النقدية بالدرج (Cash Drawer Settlement)", "", "", "", "", ""])
    ws.merge_cells(f'A{ws.max_row}:F{ws.max_row}')
    sec_cell = ws.cell(row=ws.max_row, column=1)
    sec_cell.font = section_font
    sec_cell.fill = section_fill

    recon_headers = ["بيان الحركة النقدية", "المبلغ (ج.م)", "طبيعة الحركة"]
    ws.append(recon_headers)
    for c_idx in [1, 2, 3]:
        cell = ws.cell(row=ws.max_row, column=c_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center

    recon_rows = [
        ["الرصيد الافتتاحي (العهدة المستلمة)", to_dec(session.opening_balance), "رصيد بدء الوردية"],
        ["(+) المقبوضات النقدية من المبيعات", to_dec(dt['total_cash_sales']), "مقبوض كاش من نقاط البيع"],
    ]
    if to_dec(dt['total_cash_rentals']) > 0:
        recon_rows.append(["(+) المقبوضات النقدية من إيجار المعدات", to_dec(dt['total_cash_rentals']), "عقود إيجار مسددة نقداً"])
    if to_dec(dt['total_cash_receipts']) > 0:
        recon_rows.append(["(+) سندات القبض النقدية المقبوضة بالوردية", to_dec(dt['total_cash_receipts']), "مقبوضات سندات كاش"])
    recon_rows.append(["(-) المرتجعات النقدية المسددة من الدرج", -to_dec(dt['total_cash_refunds']), "مرتجعات مبيعات كاش"])
    if to_dec(dt['total_cash_payments']) > 0:
        recon_rows.append(["(-) سندات الصرف النقدية والمصروفات", -to_dec(dt['total_cash_payments']), "مصروفات مسددة نقداً"])
    recon_rows.extend([
        ["(=) النقدية المتوقعة بالدرج (دفترياً)", to_dec(dt['expected_balance']), "الرصيد الدفتري المفترض"],
        ["(📥) النقدية الفعلية المسلمة عند الإغلاق", to_dec(dt['actual_balance']), "الجرد الفعلي للكاش"],
        ["(⚖️) الفارق النهائي (العجز / الزيادة)", to_dec(dt['difference']), "مطابق" if to_dec(dt['difference']) == 0 else ("زيادة" if to_dec(dt['difference']) > 0 else "عجز")],
    ])

    for r in recon_rows:
        ws.append(r)
        cell_label = ws.cell(row=ws.max_row, column=1)
        cell_val = ws.cell(row=ws.max_row, column=2)
        cell_desc = ws.cell(row=ws.max_row, column=3)
        cell_label.font = bold_font
        cell_label.border = thin_border
        cell_val.font = bold_font
        cell_val.border = thin_border
        cell_val.alignment = align_right
        cell_val.number_format = '#,##0.00'
        cell_desc.font = data_font
        cell_desc.border = thin_border
        cell_desc.alignment = align_center

        if "العجز / الزيادة" in r[0]:
            diff_val = to_dec(dt['difference'])
            if diff_val < 0:
                cell_val.font = Font(name='Calibri', size=11, bold=True, color='9C0006')
                cell_val.fill = PatternFill(start_color='FFC7CE', end_color='FFC7CE', fill_type='solid')
            elif diff_val > 0:
                cell_val.font = Font(name='Calibri', size=11, bold=True, color='006100')
                cell_val.fill = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')

    ws.append([])

    # 4. Payment Types Breakdown & Sales Summary
    ws.append(["💳 ملخص مبيعات الوردية وتوزيع طرق التحصيل", "", "", "", "", ""])
    ws.merge_cells(f'A{ws.max_row}:F{ws.max_row}')
    sec_cell = ws.cell(row=ws.max_row, column=1)
    sec_cell.font = section_font
    sec_cell.fill = section_fill

    pay_headers = ["طريقة التحصيل / البيان", "المبلغ (ج.م)", "ملاحظات وتفاصيل"]
    ws.append(pay_headers)
    for c_idx in [1, 2, 3]:
        cell = ws.cell(row=ws.max_row, column=c_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center

    pay_rows = [
        ["صافي إجمالي المبيعات (Net Sales)", to_dec(dt['net_sales_amount']), f"إجمالي البيع: {to_dec(dt['total_gross_sales'])} | المرتجع: {to_dec(dt['total_returns_amount'])}"],
        ["- مبيعات نقدية (كاش بالدرج)", to_dec(dt['total_cash_sales']), "مقبوضات كاش"],
        ["- مبيعات بطاقات (فيزا / شبكة)", to_dec(dt['total_card_sales']), "نقاط البيع الإلكترونية POS"],
        ["- مبيعات محافظ إلكترونية", to_dec(dt['total_ewallet_sales']), "محافظ الهاتف الذكي"],
        ["- مبيعات تحويلات بنكية", to_dec(dt['total_bank_sales']), "حسابات البنوك"],
        ["- مبيعات آجلة (ذمم عملاء)", to_dec(dt['total_credit_sales']), "حسابات العملاء الآجلة"],
        ["عدد الفواتير الصادرة", len(dt['posted_invoices']), f"بيع: {dt['sales_count']} | مرتجع: {dt['returns_count']}"],
    ]
    for r in pay_rows:
        ws.append(r)
        c1 = ws.cell(row=ws.max_row, column=1)
        c2 = ws.cell(row=ws.max_row, column=2)
        c3 = ws.cell(row=ws.max_row, column=3)
        c1.font = bold_font
        c1.border = thin_border
        c2.font = bold_font
        c2.border = thin_border
        c2.alignment = align_right
        if isinstance(r[1], Decimal):
            c2.number_format = '#,##0.00'
        else:
            c2.alignment = align_center
        c3.font = data_font
        c3.border = thin_border
        c3.alignment = align_center

    ws.append([])

    # 5. Invoices Table
    ws.append([f"📄 فواتير الوردية ({len(dt['posted_invoices'])} فاتورة)", "", "", "", "", ""])
    ws.merge_cells(f'A{ws.max_row}:F{ws.max_row}')
    sec_cell = ws.cell(row=ws.max_row, column=1)
    sec_cell.font = section_font
    sec_cell.fill = section_fill

    inv_headers = ["رقم الفاتورة", "الوقت", "النوع", "العميل", "طريقة الدفع", "المبلغ الإجمالي"]
    ws.append(inv_headers)
    for c_idx in range(1, 7):
        cell = ws.cell(row=ws.max_row, column=c_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center

    for inv in dt['posted_invoices']:
        mult = Decimal('1') if inv.invoice_type == 'SALE' else Decimal('-1')
        inv_amt = to_dec(inv.total_amount) * mult
        ws.append([
            inv.invoice_number,
            inv.created_at.strftime('%H:%M:%S') if inv.created_at else '',
            inv.get_invoice_type_display(),
            inv.partner.name if inv.partner else 'عميل نقدي',
            inv.get_payment_type_display(),
            inv_amt
        ])
        for c_idx in range(1, 7):
            cell = ws.cell(row=ws.max_row, column=c_idx)
            cell.font = data_font
            cell.border = thin_border
            if c_idx == 6:
                cell.alignment = align_right
                cell.number_format = '#,##0.00'
            else:
                cell.alignment = align_center

    # Invoices Total Row
    ws.append(["الإجمالي", "", "", "", f"{len(dt['posted_invoices'])} فاتورة", to_dec(dt['net_sales_amount'])])
    for c_idx in range(1, 7):
        cell = ws.cell(row=ws.max_row, column=c_idx)
        cell.font = bold_font
        cell.fill = total_fill
        cell.border = thin_border
        if c_idx == 6:
            cell.alignment = align_right
            cell.number_format = '#,##0.00'
        else:
            cell.alignment = align_center

    ws.append([])

    # 6. Vouchers Table
    if dt['shift_vouchers']:
        ws.append([f"🧾 سندات القبض والصرف والمصروفات ({len(dt['shift_vouchers'])} سند)", "", "", "", "", ""])
        ws.merge_cells(f'A{ws.max_row}:F{ws.max_row}')
        sec_cell = ws.cell(row=ws.max_row, column=1)
        sec_cell.font = section_font
        sec_cell.fill = section_fill

        vouch_headers = ["رقم السند", "الوقت", "النوع", "المحرر / الطرف", "البيان", "المبلغ"]
        ws.append(vouch_headers)
        for c_idx in range(1, 7):
            cell = ws.cell(row=ws.max_row, column=c_idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = align_center

        for v in dt['shift_vouchers']:
            mult = Decimal('1') if v.voucher_type == 'RECEIPT' else Decimal('-1')
            v_amt = to_dec(v.amount) * mult
            ws.append([
                v.voucher_number,
                v.created_at.strftime('%H:%M:%S') if v.created_at else '',
                v.get_voucher_type_display(),
                v.partner.name if v.partner else 'سند كاش / مصروف',
                v.description or '-',
                v_amt
            ])
            for c_idx in range(1, 7):
                cell = ws.cell(row=ws.max_row, column=c_idx)
                cell.font = data_font
                cell.border = thin_border
                if c_idx == 6:
                    cell.alignment = align_right
                    cell.number_format = '#,##0.00'
                else:
                    cell.alignment = align_center

        # Vouchers Total Row
        net_vouchers = to_dec(dt['total_cash_receipts']) - to_dec(dt['total_cash_payments'])
        ws.append(["الإجمالي (صافي حركة السندات)", "", "", "", f"{len(dt['shift_vouchers'])} سند", net_vouchers])
        for c_idx in range(1, 7):
            cell = ws.cell(row=ws.max_row, column=c_idx)
            cell.font = bold_font
            cell.fill = total_fill
            cell.border = thin_border
            if c_idx == 6:
                cell.alignment = align_right
                cell.number_format = '#,##0.00'
            else:
                cell.alignment = align_center

        ws.append([])

    # 7. Rentals Table
    if dt['shift_rentals']:
        ws.append([f"🚜 عقود إيجار المعدات بالوردية ({len(dt['shift_rentals'])} عقد)", "", "", "", "", ""])
        ws.merge_cells(f'A{ws.max_row}:F{ws.max_row}')
        sec_cell = ws.cell(row=ws.max_row, column=1)
        sec_cell.font = section_font
        sec_cell.fill = section_fill

        rent_headers = ["رقم العقد", "الوقت", "المعدة", "العميل", "طريقة الدفع", "المبلغ الإجمالي"]
        ws.append(rent_headers)
        for c_idx in range(1, 7):
            cell = ws.cell(row=ws.max_row, column=c_idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = align_center

        for r in dt['shift_rentals']:
            pm_str = "كاش" if r.payment_method == 'CASH' else "آجل"
            ws.append([
                f"#{r.id}",
                r.created_at.strftime('%H:%M:%S') if r.created_at else '',
                f"{r.equipment.name} ({r.equipment.code})",
                r.customer.name if r.customer else '-',
                pm_str,
                to_dec(r.total_amount or 0)
            ])
            for c_idx in range(1, 7):
                cell = ws.cell(row=ws.max_row, column=c_idx)
                cell.font = data_font
                cell.border = thin_border
                if c_idx == 6:
                    cell.alignment = align_right
                    cell.number_format = '#,##0.00'
                else:
                    cell.alignment = align_center

        # Rentals Total Row
        ws.append(["الإجمالي", "", "", "", f"{len(dt['shift_rentals'])} عقد", to_dec(dt['total_rentals_amount'])])
        for c_idx in range(1, 7):
            cell = ws.cell(row=ws.max_row, column=c_idx)
            cell.font = bold_font
            cell.fill = total_fill
            cell.border = thin_border
            if c_idx == 6:
                cell.alignment = align_right
                cell.number_format = '#,##0.00'
            else:
                cell.alignment = align_center

    # Auto-fit Column Widths
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.coordinate in ['A1', 'A2']:
                continue
            val_str = str(cell.value or '')
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 16)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="Shift_Report_#{session.id}.xlsx"'
    wb.save(response)
    return response


class FixedAssetReportView(CsvExportMixin, CustomPermissionRequiredMixin, TemplateView):
    """
    تقرير سجل الأصول الثابتة والإهلاك:
    يعرض كافة الأصول وتكلفتها ومجمع الإهلاك وصافي القيمة الدفترية ومعدلات الاستهلاك مع إمكانية التصدير.
    """
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/fixed_assets_report.html'
    csv_filename = "fixed_assets_report.csv"

    def get_csv_headers(self):
        return [
            'كود الأصل', 'اسم الأصل', 'التصنيف', 'تاريخ الشراء',
            'تكلفة الشراء', 'مجمع الإهلاك', 'صافي القيمة الدفترية',
            'نسبة الاستهلاك %', 'القسط الشهري', 'الحالة'
        ]

    def get_csv_row(self, obj):
        return [
            obj['code'],
            obj['name'],
            obj['category_display'],
            obj['purchase_date'].strftime('%Y-%m-%d') if hasattr(obj['purchase_date'], 'strftime') else str(obj['purchase_date']),
            float(obj['purchase_cost']),
            float(obj['total_accumulated_depreciation']),
            float(obj['current_book_value']),
            f"{obj['depreciation_progress_percent']}%",
            float(obj['monthly_depreciation_amount']),
            obj['status_display'],
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import FixedAsset
        from decimal import Decimal

        qs = FixedAsset.objects.all().select_related('branch', 'asset_account').order_by('-purchase_date', '-id')
        qs = filter_by_branch(qs, self.request)

        category = self.request.GET.get('category')
        status = self.request.GET.get('status')
        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')
        q = self.request.GET.get('q')

        if category:
            qs = qs.filter(category=category)
        if status:
            qs = qs.filter(status=status)
        if date_from:
            qs = qs.filter(purchase_date__gte=date_from)
        if date_to:
            qs = qs.filter(purchase_date__lte=date_to)
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(location__icontains=q))

        asset_rows = []
        total_cost = Decimal('0.00')
        total_acc_dep = Decimal('0.00')
        total_book_val = Decimal('0.00')
        total_monthly_dep = Decimal('0.00')

        for a in qs:
            cost = a.purchase_cost
            acc_dep = a.total_accumulated_depreciation
            book_val = a.current_book_value
            monthly_dep = a.monthly_depreciation_amount

            if a.status != FixedAsset.AssetStatus.DISPOSED:
                total_cost += cost
                total_acc_dep += acc_dep
                total_book_val += book_val
                if a.status == FixedAsset.AssetStatus.ACTIVE:
                    total_monthly_dep += monthly_dep

            asset_rows.append({
                'id': a.id,
                'code': a.code,
                'name': a.name,
                'category': a.category,
                'category_display': a.get_category_display(),
                'location': a.location,
                'purchase_date': a.purchase_date,
                'purchase_cost': cost,
                'total_accumulated_depreciation': acc_dep,
                'current_book_value': book_val,
                'depreciation_progress_percent': a.depreciation_progress_percent,
                'monthly_depreciation_amount': monthly_dep,
                'useful_life_years': a.useful_life_years,
                'depreciation_rate': a.depreciation_rate,
                'status': a.status,
                'status_display': a.get_status_display(),
            })

        context.update({
            'title': 'سجل الأصول الثابتة والإهلاك',
            'assets': asset_rows,
            'categories': FixedAsset.AssetCategory.choices,
            'statuses': FixedAsset.AssetStatus.choices,
            'selected_category': category or '',
            'selected_status': status or '',
            'date_from': date_from or '',
            'date_to': date_to or '',
            'search_query': q or '',
            'total_cost': total_cost,
            'total_accumulated_depreciation': total_acc_dep,
            'total_book_value': total_book_val,
            'total_monthly_depreciation': total_monthly_dep,
            'total_count': len(asset_rows),
            'report_data': asset_rows,
        })

        return context

    def get(self, request, *args, **kwargs):
        # CSV export must be handled here, not in get_context_data(): returning
        # an HttpResponse from get_context_data() crashes the template render.
        if request.GET.get('export') == 'csv':
            context = self.get_context_data(**kwargs)
            return self.export_csv(context.get('report_data') or [])
        return super().get(request, *args, **kwargs)

    def export_csv(self, rows):
        import csv
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = f'attachment; filename="{self.csv_filename}"'
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow(self.get_csv_headers())
        for row in rows:
            writer.writerow(self.get_csv_row(row))
        return response


class LoanReportView(CsvExportMixin, CustomPermissionRequiredMixin, TemplateView):
    """
    تقرير القروض والتسهيلات البنكية وجداول الأقساط:
    يعرض كافة القروض والالتزامات القائمة، المسدد، الفوائد، الأقساط المتأخرة،
    مع إمكانية الفلترة وتصدير التقرير لـ Excel أو CSV.
    """
    permission_required = 'core.view_accounting_reports'
    template_name = 'reports/loans_report.html'
    csv_filename = "loans_and_financing_report.xlsx"

    def get_csv_headers(self):
        return [
            'كود القرض', 'اسم القرض', 'نوع التمويل', 'الجهة الممولة',
            'تاريخ البدء', 'أصل القرض', 'إجمالي الفوائد', 'إجمالي الالتزام',
            'المسدد (أصل)', 'المسدد (فوائد)', 'إجمالي المسدد',
            'المتبقي من أصل القرض', 'المتبقي الإجمالي', 'نسبة السداد %',
            'الأقساط المتأخرة', 'الحالة'
        ]

    def get_csv_row(self, obj):
        return [
            obj['code'],
            obj['name'],
            obj['loan_type_display'],
            obj['lender_name'],
            obj['start_date'].strftime('%Y-%m-%d') if hasattr(obj['start_date'], 'strftime') else str(obj['start_date']),
            float(obj['principal_amount']),
            float(obj['total_interest_amount']),
            float(obj['total_amount']),
            float(obj['paid_principal']),
            float(obj['paid_interest']),
            float(obj['total_paid']),
            float(obj['remaining_principal']),
            float(obj['remaining_total']),
            f"{obj['repayment_progress_percent']}%",
            int(obj['overdue_count']),
            obj['status_display'],
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenant.accounting.models import Loan
        from decimal import Decimal

        qs = Loan.objects.all().select_related('loan_account', 'interest_expense_account').order_by('-start_date', '-id')
        qs = filter_by_branch(qs, self.request)

        loan_type = self.request.GET.get('loan_type')
        status = self.request.GET.get('status')
        date_from = _date_param(self.request, 'date_from')
        date_to = _date_param(self.request, 'date_to')
        q = self.request.GET.get('q')

        if loan_type:
            qs = qs.filter(loan_type=loan_type)
        if status:
            qs = qs.filter(status=status)
        if date_from:
            qs = qs.filter(start_date__gte=date_from)
        if date_to:
            qs = qs.filter(start_date__lte=date_to)
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(lender_name__icontains=q) | Q(contract_number__icontains=q))

        loan_rows = []
        total_principal = Decimal('0.00')
        total_interest = Decimal('0.00')
        total_amount = Decimal('0.00')
        total_paid_principal = Decimal('0.00')
        total_paid_interest = Decimal('0.00')
        total_paid = Decimal('0.00')
        total_remaining_principal = Decimal('0.00')
        total_remaining_total = Decimal('0.00')
        total_overdue_count = 0

        for loan in qs:
            p = loan.principal_amount
            i = loan.total_interest_amount
            tot = loan.total_amount
            paid_p = loan.paid_principal
            paid_i = loan.paid_interest
            tot_paid = loan.total_paid
            rem_p = loan.remaining_principal
            rem_tot = loan.remaining_total
            overdue = loan.overdue_count

            total_principal += p
            total_interest += i
            total_amount += tot
            total_paid_principal += paid_p
            total_paid_interest += paid_i
            total_paid += tot_paid
            total_remaining_principal += rem_p
            total_remaining_total += rem_tot
            total_overdue_count += overdue

            loan_rows.append({
                'id': loan.id,
                'code': loan.code,
                'name': loan.name,
                'loan_type': loan.loan_type,
                'loan_type_display': loan.get_loan_type_display(),
                'lender_name': loan.lender_name,
                'contract_number': loan.contract_number or '',
                'start_date': loan.start_date,
                'installments_count': loan.installments_count,
                'interest_rate': loan.interest_rate,
                'principal_amount': p,
                'total_interest_amount': i,
                'total_amount': tot,
                'paid_principal': paid_p,
                'paid_interest': paid_i,
                'total_paid': tot_paid,
                'remaining_principal': rem_p,
                'remaining_total': rem_tot,
                'repayment_progress_percent': loan.repayment_progress_percent,
                'overdue_count': overdue,
                'status': loan.status,
                'status_display': loan.get_status_display(),
            })

        context.update({
            'title': 'تقرير القروض والتسهيلات البنكية',
            'report_data': loan_rows,
            'loans': loan_rows,
            'loan_types': Loan.LoanType.choices,
            'statuses': Loan.LoanStatus.choices,
            'selected_loan_type': loan_type or '',
            'selected_status': status or '',
            'date_from': date_from or '',
            'date_to': date_to or '',
            'search_query': q or '',
            'total_principal': total_principal,
            'total_interest': total_interest,
            'total_amount': total_amount,
            'total_paid_principal': total_paid_principal,
            'total_paid_interest': total_paid_interest,
            'total_paid': total_paid,
            'total_remaining_principal': total_remaining_principal,
            'total_remaining_total': total_remaining_total,
            'total_overdue_count': total_overdue_count,
            'total_count': len(loan_rows),
        })

        return context

    def get(self, request, *args, **kwargs):
        # CSV export must be handled here, not in get_context_data(): returning
        # an HttpResponse from get_context_data() crashes the template render.
        if request.GET.get('export') == 'csv':
            context = self.get_context_data(**kwargs)
            return self.export_csv(context.get('report_data') or [])
        return super().get(request, *args, **kwargs)

    def export_csv(self, rows):
        import csv
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = f'attachment; filename="loans_report.csv"'
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow(self.get_csv_headers())
        for row in rows:
            writer.writerow(self.get_csv_row(row))
        return response


