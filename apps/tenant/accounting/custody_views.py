"""العهد النقدية: القائمة والصرف، والتسوية / الإلغاء (custody_services)."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.tenant.core.access import filter_by_branch
from apps.tenant.core.decorators import custom_permission_required
from apps.tenant.core.models import Employee

from . import custody_services as svc
from .forms import scoped_treasuries
from .models import BankAccount, Custody


def _employees(request):
    return filter_by_branch(Employee.objects.filter(user__is_active=True).select_related('user', 'branch'),
                            request).order_by('user__first_name', 'user__username')


def _custodies(request):
    return filter_by_branch(Custody.objects.select_related('employee__user', 'treasury'), request, 'employee__branch')


@login_required
@custom_permission_required('accounting.view_custody', redirect_url='core:main_screen')
def custody_list(request):
    employees = list(_employees(request))
    if request.method == 'POST':
        if not request.user.has_perm('accounting.add_custody'):
            messages.error(request, 'عفواً، لا تملك صلاحية صرف عهد.')
            return redirect('accounting:custody_list')
        try:
            emp = next((e for e in employees if str(e.id) == request.POST.get('employee')), None)
            if emp is None:
                raise ValidationError('اختار الموظف.')
            treasury = scoped_treasuries(request.user).filter(id=request.POST.get('treasury') or 0).first()
            c = svc.create_custody(emp, Decimal(request.POST.get('amount') or '0'), treasury,
                                   request.POST.get('purpose', ''), user=request.user)
            messages.success(request, f'تم صرف العهدة {c.number} بمبلغ {c.amount}.')
            return redirect('accounting:custody_detail', c.pk)
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (InvalidOperation, ValueError):
            messages.error(request, 'المبلغ غير صالح.')
        return redirect('accounting:custody_list')

    status = request.GET.get('status', Custody.OPEN)
    qs = _custodies(request)
    if status in dict(Custody.STATUS_CHOICES):
        qs = qs.filter(status=status)
    money = DecimalField(max_digits=18, decimal_places=2)
    open_total = _custodies(request).filter(status=Custody.OPEN).aggregate(
        s=Coalesce(Sum('amount'), Value(Decimal('0')), output_field=money))['s']
    page = Paginator(qs.order_by('-date', '-id'), 25).get_page(request.GET.get('page'))
    return render(request, 'accounting/custody_list.html', {
        'title': 'العهد النقدية', 'custodies': page, 'page_obj': page, 'status': status,
        'status_choices': Custody.STATUS_CHOICES, 'employees': employees,
        'treasuries': scoped_treasuries(request.user), 'open_total': open_total,
        'can_add': request.user.has_perm('accounting.add_custody'),
    })


@login_required
@custom_permission_required('accounting.view_custody', redirect_url='core:main_screen')
def custody_detail(request, pk):
    custody = get_object_or_404(_custodies(request), pk=pk)
    accounts = list(svc.expense_accounts())
    if request.method == 'POST':
        if not request.user.has_perm('accounting.change_custody'):
            messages.error(request, 'عفواً، لا تملك صلاحية تسوية العهد.')
            return redirect('accounting:custody_detail', pk)
        try:
            if request.POST.get('action') == 'cancel':
                svc.cancel_custody(custody, user=request.user)
                messages.success(request, 'اتلغت العهدة واتعكس قيد صرفها.')
            else:
                by_id = {str(a.id): a for a in accounts}
                lines = [(by_id.get(a), amt, d) for a, amt, d in zip(
                    request.POST.getlist('exp_account[]'), request.POST.getlist('exp_amount[]'),
                    request.POST.getlist('exp_desc[]')) if (amt or '').strip()]
                ret_treasury = scoped_treasuries(request.user).filter(id=request.POST.get('return_treasury') or 0).first()
                svc.settle_custody(custody, [(a, Decimal(m), d) for a, m, d in lines],
                                   returned=Decimal(request.POST.get('returned') or '0'),
                                   return_treasury=ret_treasury,
                                   leftover_to_loan=bool(request.POST.get('leftover_to_loan')), user=request.user)
                messages.success(request, 'اتسوّت العهدة واتعمل قيد التسوية.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (InvalidOperation, ValueError):
            messages.error(request, 'مبلغ غير صالح.')
        return redirect('accounting:custody_detail', pk)

    return render(request, 'accounting/custody_detail.html', {
        'title': f'العهدة {custody.number}', 'c': custody,
        'expenses': custody.expenses.select_related('account'),
        'accounts': accounts, 'treasuries': scoped_treasuries(request.user),
        'can_settle': request.user.has_perm('accounting.change_custody'),
    })


@login_required
@custom_permission_required('accounting.view_taxsettlement', redirect_url='core:main_screen')
def tax_settlement_view(request):
    """التسوية الضريبية: أرصدة الضريبة، إقفال القيمة المضافة لفترة، وسداد المستحقات."""
    import datetime
    from apps.tenant.accounting.models import TaxSettlement
    from apps.tenant.accounting import tax_settlement as tax
    from apps.tenant.hr.payroll_payments import parse_source
    if request.method == 'POST':
        if not request.user.has_perm('accounting.add_taxsettlement'):
            messages.error(request, 'عفواً، لا تملك صلاحية التسويات الضريبية.')
            return redirect('accounting:tax_settlement')
        try:
            action = request.POST.get('action')
            if action == 'close_vat':
                end = datetime.datetime.strptime(request.POST.get('period_end') or '', '%Y-%m-%d').date()
                t = tax.close_vat(end, user=request.user)
                messages.success(request, f'اتقفلت ضريبة القيمة المضافة حتى {end}: المستحق {t.amount}.')
            elif action in ('vat_pay', 'wht_pay'):
                treasury, bank = parse_source(request.POST.get('source'), request.user)
                kind = TaxSettlement.VAT_PAY if action == 'vat_pay' else TaxSettlement.WHT_PAY
                t = tax.pay(kind, Decimal(request.POST.get('amount') or '0'), treasury, bank, user=request.user)
                messages.success(request, f'{t.get_kind_display()}: {t.amount}.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (ValueError, InvalidOperation):
            messages.error(request, 'بيانات غير صالحة.')
        return redirect('accounting:tax_settlement')
    today = datetime.date.today()
    last_month_end = today.replace(day=1) - datetime.timedelta(days=1)
    return render(request, 'accounting/tax_settlement.html', {
        'title': 'التسوية الضريبية', 'pos': tax.position(today), 'last_month_end': last_month_end,
        'history': TaxSettlement.objects.select_related('journal_entry', 'treasury', 'bank_account')[:50],
        'treasuries': scoped_treasuries(request.user), 'banks': BankAccount.objects.all(),
        'can_add': request.user.has_perm('accounting.add_taxsettlement'),
    })


@login_required
@custom_permission_required('accounting.view_fiscalyearclose', redirect_url='core:main_screen')
def fiscal_years_view(request):
    """إقفال السنوات المالية."""
    from apps.tenant.accounting import fiscal_year as fy
    from apps.tenant.accounting.models import FiscalYearClose
    if request.method == 'POST':
        if not request.user.has_perm('accounting.add_fiscalyearclose'):
            messages.error(request, 'عفواً، إقفال السنوات للمدير بس.')
            return redirect('accounting:fiscal_years')
        try:
            year = int(request.POST.get('year') or 0)
            if request.POST.get('action') == 'reopen':
                fy.reopen_year(get_object_or_404(FiscalYearClose, year=year), user=request.user)
                messages.success(request, f'اتفتحت سنة {year} تاني واتعكس قيد إقفالها.')
            else:
                c = fy.close_year(year, user=request.user)
                messages.success(request, f'اتقفلت سنة {year}: صافي {"ربح" if c.net_profit >= 0 else "خسارة"} {abs(c.net_profit)} اتنقل للأرباح المحتجزة، والدفاتر اتقفلت لحد 31/12/{year}.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except ValueError:
            messages.error(request, 'سنة غير صالحة.')
        return redirect('accounting:fiscal_years')
    return render(request, 'accounting/fiscal_years.html', {
        'title': 'إقفال السنة المالية', 'rows': fy.years_overview(),
        'can_close': request.user.has_perm('accounting.add_fiscalyearclose'),
    })
