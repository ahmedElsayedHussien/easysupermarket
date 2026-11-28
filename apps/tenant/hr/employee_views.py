"""الموظفين: الملف الوظيفي، المستندات، العهد العينية، نهاية الخدمة، والخدمة الذاتية (صفحتي)."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.tenant.core.decorators import custom_permission_required
from apps.tenant.core.models import Employee

from .models import (Department, EmployeeAsset, EmployeeDocument, EmployeeLoan, EmployeeProfile, EndOfService, JobTitle,
                     LeaveRequest, LeaveType, LoanRequest, Payroll)
from .views import _employee_name, _scope_by_branch
from apps.tenant.core.money import money

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=14, decimal_places=2)
EXPIRY_WARNING_DAYS = 30


def _employees(request, active_only=False):
    qs = Employee.objects.select_related('user', 'branch', 'hr_profile__department', 'hr_profile__job_title')
    if active_only:
        qs = qs.filter(user__is_active=True)
    return _scope_by_branch(qs, request.user, 'branch')


def expiring_documents(employees, days=EXPIRY_WARNING_DAYS):
    today = timezone.localdate()
    return (EmployeeDocument.objects.filter(employee__in=employees, expiry_date__isnull=False,
                                            expiry_date__lte=today + datetime.timedelta(days=days))
            .select_related('employee__user').order_by('expiry_date'))


@custom_permission_required('hr.view_employeeprofile', redirect_url='core:main_screen')
def employee_list(request):
    if request.method == 'POST':
        if not request.user.has_perm('hr.change_department'):
            messages.error(request, 'عفواً، لا تملك صلاحية تعديل الأقسام والوظائف.')
            return redirect('hr:employee_list')
        name = (request.POST.get('name') or '').strip()[:120]
        if name and request.POST.get('kind') == 'department':
            Department.objects.get_or_create(name=name)
        elif name and request.POST.get('kind') == 'job_title':
            JobTitle.objects.get_or_create(name=name, defaults={
                'department': Department.objects.filter(pk=request.POST.get('department') or 0).first()})
        return redirect('hr:employee_list')
    employees = _employees(request).order_by('-user__is_active', 'user__first_name', 'user__username')
    dept = request.GET.get('department') or ''
    if dept.isdigit():
        employees = employees.filter(hr_profile__department_id=int(dept))
    employees = employees.annotate(
        open_assets=Count('assets', filter=Q(assets__returned_on__isnull=True), distinct=True),
        docs=Count('documents', distinct=True))
    expiring = list(expiring_documents(_employees(request, active_only=True)))
    return render(request, 'hr/employee_list.html', {
        'title': 'الموظفين', 'employees': employees, 'departments': Department.objects.annotate(n=Count('members')),
        'job_titles': JobTitle.objects.select_related('department'), 'dept': dept, 'expiring': expiring,
        'today': timezone.localdate(), 'can_org': request.user.has_perm('hr.change_department'),
    })


def _leave_remaining(emp, year):
    from .leave_services import balances
    annual = LeaveType.objects.filter(is_active=True, uses_balance=True).order_by('id').first()
    if annual is None:
        return ZERO, None
    return balances([emp], year)[(emp.id, annual.id)]['remaining'], annual


@custom_permission_required('hr.view_employeeprofile', redirect_url='core:main_screen')
def employee_file(request, pk):
    emp = get_object_or_404(_employees(request), pk=pk)
    profile, _ = EmployeeProfile.objects.get_or_create(employee=emp)
    can_edit = request.user.has_perm('hr.change_employeeprofile')
    if request.method == 'POST':
        if not can_edit:
            messages.error(request, 'عفواً، لا تملك صلاحية تعديل ملفات الموظفين.')
            return redirect('hr:employee_file', pk)
        action = request.POST.get('action')
        try:
            if action == 'profile':
                profile.department = Department.objects.filter(pk=request.POST.get('department') or 0).first()
                profile.job_title = JobTitle.objects.filter(pk=request.POST.get('job_title') or 0).first()
                profile.hire_date = request.POST.get('hire_date') or None
                profile.national_id = (request.POST.get('national_id') or '').strip()[:20]
                profile.phone = (request.POST.get('phone') or '').strip()[:30]
                profile.address = (request.POST.get('address') or '').strip()[:255]
                profile.save()
                messages.success(request, 'اتحفظت البيانات الوظيفية.')
            elif action == 'document':
                EmployeeDocument.objects.create(
                    employee=emp, doc_type=request.POST.get('doc_type') or 'OTHER',
                    title=(request.POST.get('title') or '').strip()[:150],
                    number=(request.POST.get('number') or '').strip()[:60],
                    issue_date=request.POST.get('issue_date') or None, expiry_date=request.POST.get('expiry_date') or None,
                    file=request.FILES.get('file'))
                messages.success(request, 'اتضاف المستند.')
            elif action == 'delete_document':
                EmployeeDocument.objects.filter(employee=emp, pk=request.POST.get('doc') or 0).delete()
                messages.success(request, 'اتحذف المستند.')
            elif action == 'asset':
                item = (request.POST.get('item') or '').strip()
                if not item:
                    raise ValidationError('اكتب اسم العهدة.')
                EmployeeAsset.objects.create(employee=emp, item=item[:150], serial=(request.POST.get('serial') or '')[:80],
                                             value=Decimal(request.POST.get('value') or '0'),
                                             issued_on=request.POST.get('issued_on') or timezone.localdate(),
                                             notes=(request.POST.get('notes') or '')[:255])
                messages.success(request, 'اتسجلت العهدة العينية.')
            elif action == 'return_asset':
                EmployeeAsset.objects.filter(employee=emp, pk=request.POST.get('asset') or 0,
                                             returned_on__isnull=True).update(returned_on=timezone.localdate())
                messages.success(request, 'اتسجل رجوع العهدة.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (InvalidOperation, ValueError):
            messages.error(request, 'بيانات غير صالحة.')
        return redirect('hr:employee_file', pk)

    from .loan_services import with_balances
    today = timezone.localdate()
    loans = list(with_balances(EmployeeLoan.objects.filter(employee=emp, status=EmployeeLoan.ACTIVE)))
    leave_left, annual = _leave_remaining(emp, today.year)
    return render(request, 'hr/employee_file.html', {
        'title': f'ملف {_employee_name(emp)}', 'emp': emp, 'name': _employee_name(emp), 'profile': profile,
        'departments': Department.objects.all(), 'job_titles': JobTitle.objects.all(),
        'documents': emp.documents.all(), 'doc_types': EmployeeDocument.TYPE_CHOICES,
        'assets': emp.assets.all(), 'loans': loans, 'loans_left': sum((l.remaining for l in loans), ZERO),
        'leave_left': leave_left, 'annual_type': annual,
        'payrolls': Payroll.objects.filter(employee=emp).order_by('-year', '-month')[:12],
        'eos': EndOfService.objects.filter(employee=emp).select_related('journal_entry').first(),
        'today': today, 'soon': today + datetime.timedelta(days=EXPIRY_WARNING_DAYS),
        'can_edit': can_edit, 'can_eos': request.user.has_perm('hr.add_endofservice'),
    })


# ---------------------------------------------------------------- end of service
def eos_preview(emp, termination_date):
    from .loan_services import with_balances
    hire = getattr(getattr(emp, 'hr_profile', None), 'hire_date', None) or timezone.localdate(emp.user.date_joined)
    years = money(max(Decimal((termination_date - hire).days) / Decimal('365'), ZERO))
    daily = money(emp.base_salary / Decimal(30)) if emp.base_salary else ZERO
    leave_left, _ = _leave_remaining(emp, termination_date.year)
    loans_left = sum((l.remaining for l in with_balances(EmployeeLoan.objects.filter(employee=emp, status=EmployeeLoan.ACTIVE))), ZERO)
    open_assets = list(emp.assets.filter(returned_on__isnull=True))
    return {'hire': hire, 'years': years, 'daily_rate': daily, 'leave_days': max(leave_left, ZERO),
            'leave_encashment': money(max(leave_left, ZERO) * daily),
            'loans_left': loans_left, 'open_assets': open_assets}


def post_end_of_service(emp, termination_date, reason, severance, leave_days, treasury, bank, user=None):
    from apps.tenant.services.journal_service import _get_system_account
    from .payroll_payments import _source_account
    if EndOfService.objects.filter(employee=emp).exists():
        raise ValidationError('الموظف ده ليه مخالصة بالفعل.')
    p = eos_preview(emp, termination_date)
    if p['open_assets']:
        raise ValidationError('فيه عهد عينية لسه مرجعتش: ' + '، '.join(a.item for a in p['open_assets']))
    severance = money(Decimal(severance or 0))
    leave_days = Decimal(leave_days or 0)
    encash = money(leave_days * p['daily_rate'])
    if severance < 0 or leave_days < 0:
        raise ValidationError('المكافأة وأيام الإجازة مينفعش يكونوا بالسالب.')
    gross = severance + encash
    loans = min(p['loans_left'], gross)              # taken out of what he is owed
    collected = p['loans_left'] - loans              # the rest: he pays it in now (a loan is never left behind)
    net = gross - loans
    if (net > 0 or collected > 0) and treasury is None and bank is None:
        raise ValidationError('اختار الخزينة أو البنك اللي هيتصرف منه صافي المخالصة.' if net > 0 else
                              f'الموظف عليه سلف {p["loans_left"]:.2f} أكتر من مستحقاته — اختار الخزينة أو البنك '
                              f'اللي هيورّد فيه الفرق ({collected:.2f}).')
    if treasury is not None and bank is not None:
        raise ValidationError('اختار خزينة أو بنك — مش الاتنين.')
    with transaction.atomic():
        eos = EndOfService.objects.create(
            employee=emp, termination_date=termination_date, reason=(reason or '')[:150], years_of_service=p['years'],
            severance=severance, leave_days=leave_days, leave_encashment=encash, loans_deducted=loans,
            loans_collected=collected, net_amount=net,
            assets_returned=True, treasury=treasury, bank_account=bank, created_by=user)
        if gross > 0 or collected > 0:
            label = f'مخالصة نهاية خدمة {_employee_name(emp)}'
            lines = []
            if gross > 0:
                lines.append((_get_system_account('5210'), gross, ZERO, label))
            if collected > 0:
                holder = treasury or bank
                lines.append((holder.ensure_account(), collected, ZERO, f'{label} - سلف ورّدها الموظف'))
            if loans + collected > 0:
                lines.append((_get_system_account('1135'), ZERO, loans + collected, f'{label} - إقفال السلف'))
            if net > 0:
                lines.append((_source_account(treasury, bank, net), ZERO, net, f'{label} - الصافي'))
            from apps.tenant.invoicing.shift_services import _post as post_entry
            eos.journal_entry = post_entry(f'EOS-{eos.pk}', label, lines, user, eos)
            eos.save(update_fields=['journal_entry'])
        # every loan is closed (covered by the settlement or paid in now); the user can no longer log in
        if loans + collected > 0:
            EmployeeLoan.objects.filter(employee=emp, status=EmployeeLoan.ACTIVE).update(status=EmployeeLoan.SETTLED)
        emp.user.is_active = False
        emp.user.save(update_fields=['is_active'])
    return eos


@custom_permission_required('hr.add_endofservice', redirect_url='core:main_screen')
def end_of_service(request, pk):
    from apps.tenant.accounting.forms import scoped_treasuries
    from apps.tenant.accounting.models import BankAccount
    from .payroll_payments import parse_source
    emp = get_object_or_404(_employees(request), pk=pk)
    if EndOfService.objects.filter(employee=emp).exists():
        messages.info(request, 'الموظف ده ليه مخالصة بالفعل.')
        return redirect('hr:employee_file', pk)
    try:
        term = datetime.datetime.strptime(request.POST.get('termination_date') or request.GET.get('date') or '',
                                          '%Y-%m-%d').date()
    except ValueError:
        term = timezone.localdate()
    preview = eos_preview(emp, term)
    if request.method == 'POST':
        try:
            treasury, bank = (None, None)
            if request.POST.get('source'):
                treasury, bank = parse_source(request.POST.get('source'), request.user)
            eos = post_end_of_service(emp, term, request.POST.get('reason'), Decimal(request.POST.get('severance') or '0'),
                                      Decimal(request.POST.get('leave_days') or '0'), treasury, bank, user=request.user)
            messages.success(request, f'اتعملت مخالصة {_employee_name(emp)}: صافي {eos.net_amount}، والحساب اتقفل.')
            return redirect('hr:employee_file', pk)
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (InvalidOperation, ValueError):
            messages.error(request, 'بيانات غير صالحة.')
    return render(request, 'hr/end_of_service.html', {
        'title': f'نهاية خدمة {_employee_name(emp)}', 'emp': emp, 'name': _employee_name(emp), 'p': preview,
        'term': term, 'treasuries': scoped_treasuries(request.user), 'banks': BankAccount.objects.all(),
    })


# ---------------------------------------------------------------- self service
@login_required
def my_portal(request):
    """صفحتي: حضوري ومرتباتي ورصيد إجازاتي وسلفي — وطلب إجازة أو سلفة."""
    from .attendance_services import AR_MONTHS, month_summary
    from .leave_services import balances, create_request
    from .loan_services import with_balances
    from .views import _payroll_settings
    emp = getattr(request.user, 'employee_profile', None)
    if emp is None:
        messages.error(request, 'حسابك مش مربوط بملف موظف.')
        return redirect('core:main_screen')
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'leave':
                lt = LeaveType.objects.filter(pk=request.POST.get('leave_type') or 0, is_active=True).first()
                if lt is None:
                    raise ValidationError('اختار نوع الإجازة.')
                start = datetime.date.fromisoformat(request.POST.get('start_date'))
                end = datetime.date.fromisoformat(request.POST.get('end_date') or request.POST.get('start_date'))
                create_request(emp, lt, start, end, request.POST.get('reason', ''), user=request.user)
                messages.success(request, 'اتبعت طلب الإجازة — مستني الاعتماد.')
            elif request.POST.get('action') == 'loan':
                amount = Decimal(request.POST.get('amount') or '0')
                if amount <= 0:
                    raise ValidationError('اكتب مبلغ السلفة.')
                if LoanRequest.objects.filter(employee=emp, status=LoanRequest.PENDING).exists():
                    raise ValidationError('عندك طلب سلفة لسه مستني الاعتماد.')
                LoanRequest.objects.create(employee=emp, amount=amount,
                                           installments_count=max(int(request.POST.get('installments') or 1), 1),
                                           reason=(request.POST.get('reason') or '')[:255])
                messages.success(request, 'اتبعت طلب السلفة — مستني الاعتماد.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (InvalidOperation, ValueError, TypeError):
            messages.error(request, 'بيانات غير صالحة.')
        return redirect('hr:my_portal')
    today = timezone.localdate()
    cfg = _payroll_settings()
    summary = month_summary([emp], today.year, today.month, until=today, weekly_off=cfg['weekly_off'],
                            grace_hours=cfg['grace_hours'])[emp.id]
    bal = balances([emp], today.year)
    return render(request, 'hr/my_portal.html', {
        'title': 'صفحتي', 'emp': emp, 'name': _employee_name(emp), 'summary': summary,
        'month_name': f'{AR_MONTHS[today.month - 1]} {today.year}',
        'payslips': Payroll.objects.filter(employee=emp, is_paid=True).order_by('-year', '-month')[:12],
        'balances': [v for (e, t), v in bal.items()],
        'leave_types': LeaveType.objects.filter(is_active=True),
        'leaves': LeaveRequest.objects.filter(employee=emp).select_related('leave_type')[:10],
        'loans': with_balances(EmployeeLoan.objects.filter(employee=emp).exclude(status=EmployeeLoan.CANCELLED))[:10],
        'loan_requests': LoanRequest.objects.filter(employee=emp)[:5], 'today': today,
    })


# ---------------------------------------------------------------- loan requests (manager)
@custom_permission_required('hr.change_loanrequest', redirect_url='core:main_screen')
def loan_request_decide(request, pk):
    from .loan_services import create_cash_loan
    from apps.tenant.accounting.forms import scoped_treasuries
    req = get_object_or_404(LoanRequest.objects.filter(employee__in=_employees(request)), pk=pk,
                            status=LoanRequest.PENDING)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'reject':
                req.status, req.decided_by = LoanRequest.REJECTED, request.user
                req.save(update_fields=['status', 'decided_by'])
                messages.success(request, 'اترفض طلب السلفة.')
            else:
                treasury = scoped_treasuries(request.user).filter(pk=request.POST.get('treasury') or 0).first()
                today = timezone.localdate()
                start = request.POST.get('start') or f'{today.year}-{today.month}'
                y, m = (int(x) for x in start.split('-')[:2])
                loan = create_cash_loan(req.employee, Decimal(request.POST.get('amount') or req.amount),
                                        int(request.POST.get('installments') or req.installments_count), y, m, treasury,
                                        reason=req.reason or 'طلب سلفة', user=request.user)
                req.status, req.loan, req.decided_by = LoanRequest.APPROVED, loan, request.user
                req.save(update_fields=['status', 'loan', 'decided_by'])
                messages.success(request, f'اتصرفت السلفة {loan.amount}.')
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
        except (InvalidOperation, ValueError):
            messages.error(request, 'بيانات غير صالحة.')
    return redirect('hr:loan_list')
