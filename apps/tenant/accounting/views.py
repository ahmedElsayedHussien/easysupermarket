from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.urls import reverse_lazy
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from apps.tenant.core.mixins import CustomPermissionRequiredMixin
from apps.tenant.core.decorators import custom_permission_required, is_admin_user

import datetime
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.db import IntegrityError
from django.utils import timezone
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View
from .models import (
    JournalEntry, JournalItem, Account, PaymentMethod, Treasury, BankAccount, EWallet, Expense, Voucher, InternalTransfer,
    Cheque, ChequeBook, ChequeType, ChequeStatus, ChequeActionLog, FixedAsset, AssetDepreciationLog,
    Loan, LoanInstallment
)
from .forms import (
    TreasuryForm, BankAccountForm, EWalletForm, ExpenseForm, ExpenseItemFormSet, VoucherForm, AccountForm, InternalTransferForm,
    ChequeBookForm, ReceivableChequeForm, PayableChequeForm, ChequeCollectForm, ChequeBounceForm,
    ChequeClearPayableForm, ChequeCancelPayableForm, FixedAssetForm, FixedAssetDisposalForm, FixedAssetDepreciationRunForm,
    LoanForm, LoanInstallmentPayForm
)
from django.db import transaction
from django.core.exceptions import ValidationError


# ---------------------------------------------------------------------------
# Branch scoping helpers (non-admin users only see / act on their own branch)
# ---------------------------------------------------------------------------

def _user_branch(user):
    emp = getattr(user, 'employee_profile', None)
    return getattr(emp, 'branch', None) if emp else None


def _branch_scoped(qs, user, make_q):
    """Admins see everything; other users only rows matching make_q(branch) (none without a branch)."""
    if is_admin_user(user):
        return qs
    branch = _user_branch(user)
    if branch is None:
        return qs.none()
    return qs.filter(make_q(branch)).distinct()


def scope_expenses(qs, user):
    return _branch_scoped(qs, user, lambda b: Q(branch=b))


def scope_vouchers(qs, user):
    return _branch_scoped(qs, user, lambda b: Q(branch=b) | Q(treasury__branch=b) | Q(pos_session__branch=b))


def scope_transfers(qs, user):
    return _branch_scoped(
        qs, user,
        lambda b: Q(from_treasury__branch=b) | Q(to_treasury__branch=b) | Q(created_by=user)
    )


def scope_treasuries(qs, user):
    return _branch_scoped(qs, user, lambda b: Q(branch=b))


def _error_text(exc):
    if isinstance(exc, ValidationError):
        return ' '.join(exc.messages)
    return str(exc)


def _unique_reference(base):
    """base, base-2, base-3 ... (first one not used by an existing journal entry)."""
    ref = base
    n = 2
    while JournalEntry.objects.filter(reference=ref).exists():
        ref = f"{base}-{n}"
        n += 1
    return ref


class JournalEntryListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_journalentry'

    model = JournalEntry
    template_name = 'accounting/journal_list.html'
    context_object_name = 'entries'
    paginate_by = 20

    def dispatch(self, request, *args, **kwargs):
        if not is_admin_user(request.user):
            messages.error(request, 'عفواً، الدخول إلى قيود اليومية مقتصر على الأدمن والمديرين فقط.')
            return redirect('core:main_screen')
        return super().dispatch(request, *args, **kwargs)

    
    def get_queryset(self):
        queryset = JournalEntry.objects.select_related('reversal_of', 'reversed_by').order_by('-date', '-created_at')
        
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        
        if search_query:
            from django.db.models import Q
            queryset = queryset.filter(
                Q(reference__icontains=search_query) | 
                Q(description__icontains=search_query)
            )
            
        if start_date:
            queryset = queryset.filter(date__gte=start_date)
        if end_date:
            queryset = queryset.filter(date__lte=end_date)
            
        return queryset
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'قيود اليومية'
        # Keep filter parameters for pagination links
        context['start_date'] = self.request.GET.get('start_date', '')
        context['end_date'] = self.request.GET.get('end_date', '')
        context['search_query'] = self.request.GET.get('search_query', '')
        return context

class JournalEntryDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_journalentry'

    model = JournalEntry
    template_name = 'accounting/journal_detail.html'
    context_object_name = 'entry'

    def get_queryset(self):
        return JournalEntry.objects.select_related('reversal_of', 'reversed_by', 'created_by')

    def dispatch(self, request, *args, **kwargs):
        if not is_admin_user(request.user):
            messages.error(request, 'عفواً، الدخول إلى تفاصيل القيد المحاسبي مقتصر على الأدمن والمديرين فقط.')
            return redirect('core:main_screen')
        return super().dispatch(request, *args, **kwargs)

    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f'تفاصيل القيد {self.object.reference}'
        return context

# Treasury Views
class TreasuryListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_treasury'

    model = Treasury
    template_name = 'accounting/treasury_list.html'
    context_object_name = 'treasuries'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'الخزائن'
        return context

class TreasuryCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_treasury'

    model = Treasury
    form_class = TreasuryForm
    template_name = 'accounting/treasury_form.html'
    success_url = reverse_lazy('accounting:treasury_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة خزينة'
        context['cancel_url'] = self.success_url
        return context

class TreasuryUpdateView(CustomPermissionRequiredMixin, UpdateView):
    permission_required = 'accounting.change_treasury'

    model = Treasury
    form_class = TreasuryForm
    template_name = 'accounting/treasury_form.html'
    success_url = reverse_lazy('accounting:treasury_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل خزينة'
        context['cancel_url'] = self.success_url
        return context

class TreasuryDeleteView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.delete_treasury'

    def post(self, request, pk):
        treasury = get_object_or_404(Treasury, pk=pk)
        
        has_transactions = False
        if treasury.account and treasury.account.journal_items.exists():
            has_transactions = True
            
        if has_transactions:
            messages.error(request, 'لا يمكن حذف هذه الخزينة لوجود حركات مالية مسجلة عليها.')
        else:
            from django.db.models import ProtectedError
            try:
                account = treasury.account
                treasury.delete()
                if account:
                    account.delete()
                messages.success(request, 'تم حذف الخزينة بنجاح.')
            except ProtectedError:
                messages.error(request, 'لا يمكن حذف الخزينة لارتباطها بمستندات أو حركات أخرى في النظام.')
            except Exception as e:
                messages.error(request, f'حدث خطأ أثناء الحذف: {str(e)}')
                
        return redirect('accounting:treasury_list')

# BankAccount Views
class BankAccountListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_bankaccount'

    model = BankAccount
    template_name = 'accounting/bank_list.html'
    context_object_name = 'banks'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'حسابات البنوك'
        return context

class BankAccountCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_bankaccount'

    model = BankAccount
    form_class = BankAccountForm
    template_name = 'accounting/bank_form.html'
    success_url = reverse_lazy('accounting:bank_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة حساب بنكي'
        context['cancel_url'] = self.success_url
        return context

class BankAccountUpdateView(CustomPermissionRequiredMixin, UpdateView):
    permission_required = 'accounting.change_bankaccount'

    model = BankAccount
    form_class = BankAccountForm
    template_name = 'accounting/bank_form.html'
    success_url = reverse_lazy('accounting:bank_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل حساب بنكي'
        context['cancel_url'] = self.success_url
        return context

class BankAccountDeleteView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.delete_bankaccount'

    def post(self, request, pk):
        bank_account = get_object_or_404(BankAccount, pk=pk)
        
        has_transactions = False
        if bank_account.account and bank_account.account.journal_items.exists():
            has_transactions = True
            
        if has_transactions:
            messages.error(request, 'لا يمكن حذف هذا الحساب البنكي لوجود حركات مالية مسجلة عليه.')
        else:
            from django.db.models import ProtectedError
            try:
                account = bank_account.account
                bank_account.delete()
                if account:
                    account.delete()
                messages.success(request, 'تم حذف الحساب البنكي بنجاح.')
            except ProtectedError:
                messages.error(request, 'لا يمكن حذف الحساب البنكي لارتباطه بمستندات أو حركات أخرى في النظام.')
            except Exception as e:
                messages.error(request, f'حدث خطأ أثناء الحذف: {str(e)}')
                
        return redirect('accounting:bank_list')

# EWallet Views
class EWalletListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_ewallet'

    model = EWallet
    template_name = 'accounting/ewallet_list.html'
    context_object_name = 'ewallets'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'المحافظ الإلكترونية'
        return context

class EWalletCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_ewallet'

    model = EWallet
    form_class = EWalletForm
    template_name = 'accounting/ewallet_form.html'
    success_url = reverse_lazy('accounting:ewallet_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة محفظة إلكترونية'
        context['cancel_url'] = self.success_url
        return context

class EWalletUpdateView(CustomPermissionRequiredMixin, UpdateView):
    permission_required = 'accounting.change_ewallet'

    model = EWallet
    form_class = EWalletForm
    template_name = 'accounting/ewallet_form.html'
    success_url = reverse_lazy('accounting:ewallet_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل محفظة إلكترونية'
        context['cancel_url'] = self.success_url
        return context

class EWalletDeleteView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.delete_ewallet'

    def post(self, request, pk):
        ewallet = get_object_or_404(EWallet, pk=pk)
        
        has_transactions = False
        if ewallet.account and ewallet.account.journal_items.exists():
            has_transactions = True
            
        if has_transactions:
            messages.error(request, 'لا يمكن حذف هذه المحفظة لوجود حركات مالية مسجلة عليها.')
        else:
            from django.db.models import ProtectedError
            try:
                account = ewallet.account
                ewallet.delete()
                if account:
                    account.delete()
                messages.success(request, 'تم حذف المحفظة بنجاح.')
            except ProtectedError:
                messages.error(request, 'لا يمكن حذف المحفظة لارتباطها بمستندات أو حركات أخرى في النظام.')
            except Exception as e:
                messages.error(request, f'حدث خطأ أثناء الحذف: {str(e)}')
                
        return redirect('accounting:ewallet_list')

# Payment Method Views
class PaymentMethodListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_paymentmethod'

    model = PaymentMethod
    template_name = 'core/generic_list.html'
    context_object_name = 'objects'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'طرق الدفع'
        context['create_url'] = reverse_lazy('accounting:payment_method_create')
        context['update_url_name'] = 'accounting:payment_method_update'
        return context

class PaymentMethodCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_paymentmethod'

    model = PaymentMethod
    fields = ['name', 'account', 'is_active']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('accounting:payment_method_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة طريقة دفع'
        context['cancel_url'] = self.success_url
        return context

class PaymentMethodUpdateView(CustomPermissionRequiredMixin, UpdateView):
    permission_required = 'accounting.change_paymentmethod'

    model = PaymentMethod
    fields = ['name', 'account', 'is_active']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('accounting:payment_method_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل طريقة دفع'
        context['cancel_url'] = self.success_url
        return context

class AccountCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_account'
    model = Account
    form_class = AccountForm
    template_name = 'accounting/account_form.html'
    success_url = reverse_lazy('accounting:chart_of_accounts')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة حساب جديد في شجرة الحسابات'
        context['cancel_url'] = self.success_url
        return context

class AccountUpdateView(CustomPermissionRequiredMixin, UpdateView):
    permission_required = 'accounting.change_account'
    model = Account
    form_class = AccountForm
    template_name = 'accounting/account_form.html'
    success_url = reverse_lazy('accounting:chart_of_accounts')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f'تعديل الحساب #{self.object.code}'
        context['cancel_url'] = self.success_url
        return context

@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def chart_of_accounts(request):
    accounts = Account.objects.all()
    has_accounts = accounts.exists()
    context = {'accounts': accounts, 'has_accounts': has_accounts, 'title': 'شجرة الحسابات'}
    return render(request, 'accounting/chart_of_accounts.html', context)

@login_required
@custom_permission_required('accounting.add_account', redirect_url='core:main_screen')
def setup_default_accounts(request):
    from django.contrib import messages
    from django.shortcuts import redirect
    from apps.tenant.core.management.commands.setup_chart_of_accounts import setup_chart_of_accounts_data
    
    if Account.objects.exists():
        messages.error(request, 'لا يمكن إنشاء شجرة الحسابات لأنها موجودة بالفعل!')
        return redirect('accounting:chart_of_accounts')
        
    try:
        setup_chart_of_accounts_data()
        messages.success(request, 'تم زراعة شجرة الحسابات الافتراضية بنجاح!')
    except Exception as e:
        messages.error(request, f'حدث خطأ أثناء الإنشاء: {str(e)}')
        
    return redirect('accounting:chart_of_accounts')


def _report_accounts(qs):
    """
    Loads accounts with their *own* (direct) posted balances in a single aggregate query.
    Every account is included — leaf or parent — so amounts posted directly on a parent
    account (e.g. 1110/1120/1130/1160 fallbacks in journal_service) are reported, while
    descendants are never added on top of their parent (no double counting).
    """
    from apps.tenant.services.journal_service import batch_get_own_balances
    accounts = list(qs)
    batch_get_own_balances(account_qs=accounts)
    for acc in accounts:
        acc.report_name = acc.name if acc.is_leaf else f'{acc.name} (مرحّل مباشرة على الحساب الرئيسي)'
        # Contra accounts (is_contra / is_contra_revenue) carry a balance opposite to their type
        # (e.g. 1220 accumulated depreciation is a credit balance under ASSETS), so own_balance is
        # negative. Reports show it as a positive amount presented as a deduction; the signed
        # own_balance is still what every total uses, so totals do not change.
        acc.is_contra_account = bool(getattr(acc, 'is_contra', False) or acc.is_contra_revenue)
        acc.contra_amount = -acc.own_balance if acc.is_contra_account else Decimal('0.00')
    return accounts


def _section(accounts):
    """Gross (normal accounts), deductions (contra accounts, positive) and net (= signed sum)."""
    gross = sum((a.own_balance for a in accounts if not a.is_contra_account), Decimal('0.00'))
    deductions = sum((a.contra_amount for a in accounts if a.is_contra_account), Decimal('0.00'))
    return {
        'gross': gross,
        'deductions': deductions,
        'net': gross - deductions,
        'has_deductions': any(a.is_contra_account for a in accounts),
    }


@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def trial_balance(request):
    """
    ميزان المراجعة — رصيد كل حساب من قيوده المباشرة فقط (حسابات طرفية + أي مبالغ مرحلة على
    الحسابات الرئيسية) بحيث لا يتكرر أي مبلغ، مع إجمالي مدين = إجمالي دائن.
    استعلامان فقط بغض النظر عن عدد الحسابات.
    """
    accounts = _report_accounts(Account.objects.all().order_by('tree_id', 'lft'))

    rows = []
    total_debit = Decimal('0.00')
    total_credit = Decimal('0.00')
    for acc in accounts:
        net = (acc.own_debit - acc.own_credit).quantize(Decimal('0.01'))
        if net == 0:
            continue
        acc.tb_debit = net if net > 0 else Decimal('0.00')
        acc.tb_credit = -net if net < 0 else Decimal('0.00')
        total_debit += acc.tb_debit
        total_credit += acc.tb_credit
        rows.append(acc)

    context = {
        'accounts': rows,
        'total_debit': total_debit,
        'total_credit': total_credit,
        'is_balanced': total_debit == total_credit,
        'difference': total_debit - total_credit,
        'title': 'ميزان المراجعة',
    }
    return render(request, 'accounting/trial_balance.html', context)

@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def income_statement(request):
    """
    قائمة الدخل — ديناميكية تماماً، تعتمد على نوع الحساب والأعلام فقط.
    أي حساب مصروف أو إيراد جديد يُضاف لشجرة الحسابات سيظهر هنا تلقائياً.

    الإيرادات المعاكسة (مرتجعات/خصومات، is_contra_revenue) حسابات من نوع REVENUE رصيدها
    مدين، أي أن current/own balance لها سالب. نعرضها كمبلغ موجب (contra_amount) ونطرحها:
        صافي المبيعات = المبيعات − |المرتجعات والخصومات|
    وهو نفس ما تفعله الميزانية العمومية (جمع الأرصدة الموقّعة لكل حسابات الإيرادات).
    """
    accounts = _report_accounts(
        Account.objects.filter(account_type__in=[Account.REVENUE, Account.EXPENSE]).order_by('code')
    )
    revenue_accounts = [acc for acc in accounts if acc.account_type == Account.REVENUE and acc.own_balance != 0]
    expense_accounts = [acc for acc in accounts if acc.account_type == Account.EXPENSE and acc.own_balance != 0]

    # ── إيرادات المبيعات الأساسية (REVENUE وليس معاكساً)
    sales_accounts = [acc for acc in revenue_accounts if not acc.is_contra_account]
    total_sales = sum((acc.own_balance for acc in sales_accounts), Decimal('0.00'))

    # ── الإيرادات المعاكسة (مرتجعات، خصومات) — كمبلغ موجب يُطرح
    contra_revenue_accounts = [acc for acc in revenue_accounts if acc.is_contra_account]
    total_contra_revenue = sum((acc.contra_amount for acc in contra_revenue_accounts), Decimal('0.00'))

    net_sales = total_sales - total_contra_revenue

    # ── تكلفة البضاعة المباعة COGS
    # حسابات المصروفات المعاكسة (is_contra، مثل 5110 مردودات ومسموحات المشتريات) رصيدها دائن،
    # فتُعرض كمبلغ موجب يُخصم من تكلفة المبيعات/المشتريات بدلاً من ظهورها كمصروف تشغيلي سالب.
    cogs_accounts = [acc for acc in expense_accounts if acc.is_cogs and not acc.is_contra_account]
    contra_cogs_accounts = [acc for acc in expense_accounts if acc.is_contra_account]
    gross_cogs = sum((acc.own_balance for acc in cogs_accounts), Decimal('0.00'))
    total_contra_cogs = sum((acc.contra_amount for acc in contra_cogs_accounts), Decimal('0.00'))
    total_cogs = gross_cogs - total_contra_cogs

    gross_profit = net_sales - total_cogs

    # ── مصروفات التشغيل (كل مصروف ليس COGS وليس معاكساً)
    op_expense_accounts = [acc for acc in expense_accounts if not acc.is_cogs and not acc.is_contra_account]
    total_op_expense = sum((acc.own_balance for acc in op_expense_accounts), Decimal('0.00'))

    total_expense = total_cogs + total_op_expense
    net_income = net_sales - total_expense

    context = {
        'sales_accounts': sales_accounts,
        'contra_revenue_accounts': contra_revenue_accounts,
        'cogs_accounts': cogs_accounts,
        'contra_cogs_accounts': contra_cogs_accounts,
        'gross_cogs': gross_cogs,
        'total_contra_cogs': total_contra_cogs,
        'op_expense_accounts': op_expense_accounts,
        'total_sales': total_sales,
        'total_contra_revenue': total_contra_revenue,
        'net_sales': net_sales,
        'total_cogs': total_cogs,
        'gross_profit': gross_profit,
        'total_op_expense': total_op_expense,
        'total_expense': total_expense,
        'net_income': net_income,
        'title': 'قائمة الدخل',
    }
    return render(request, 'accounting/income_statement.html', context)

@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def balance_sheet(request):
    """
    الميزانية العمومية — أرصدة مباشرة لكل الحسابات (طرفية ورئيسية) في استعلامين فقط.
    """
    accounts = [acc for acc in _report_accounts(Account.objects.all().order_by('code')) if acc.own_balance != 0]

    asset_accounts     = [acc for acc in accounts if acc.account_type == Account.ASSET]
    liability_accounts = [acc for acc in accounts if acc.account_type == Account.LIABILITY]
    equity_accounts    = [acc for acc in accounts if acc.account_type == Account.EQUITY]
    revenue_accounts   = [acc for acc in accounts if acc.account_type == Account.REVENUE]
    expense_accounts   = [acc for acc in accounts if acc.account_type == Account.EXPENSE]

    # ── تصنيف الأصول بالكود (11xx = متداولة، 12xx = ثابتة، غيرهم = أخرى)
    current_assets      = [acc for acc in asset_accounts if acc.code.startswith('11')]
    fixed_assets        = [acc for acc in asset_accounts if acc.code.startswith('12')]
    other_assets        = [acc for acc in asset_accounts if not acc.code.startswith('11') and not acc.code.startswith('12')]

    # ── تصنيف الخصوم بالكود (21xx = متداولة، 22xx = طويلة الأجل، غيرهم = أخرى)
    current_liabilities      = [acc for acc in liability_accounts if acc.code.startswith('21')]
    long_term_liabilities    = [acc for acc in liability_accounts if acc.code.startswith('22')]
    other_liabilities        = [acc for acc in liability_accounts if not acc.code.startswith('21') and not acc.code.startswith('22')]

    total_assets      = sum((acc.own_balance for acc in asset_accounts), Decimal('0.00'))
    total_liabilities = sum((acc.own_balance for acc in liability_accounts), Decimal('0.00'))
    total_equity      = sum((acc.own_balance for acc in equity_accounts), Decimal('0.00'))

    # ── صافي الدخل يُضاف لحقوق الملكية (المرتجعات المعاكسة رصيدها سالب فتُطرح تلقائياً)
    net_income = (
        sum((acc.own_balance for acc in revenue_accounts), Decimal('0.00')) -
        sum((acc.own_balance for acc in expense_accounts), Decimal('0.00'))
    )

    total_liabilities_and_equity = total_liabilities + total_equity + net_income

    context = {
        # gross / deductions (contra accounts as positive amounts) / net per section
        'current_assets_summary': _section(current_assets),
        'fixed_assets_summary': _section(fixed_assets),
        'other_assets_summary': _section(other_assets),
        'liabilities_summary': _section(liability_accounts),
        'equity_summary': _section(equity_accounts),
        'current_assets': current_assets,
        'fixed_assets': fixed_assets,
        'other_assets': other_assets,
        'liability_accounts': liability_accounts,
        'current_liabilities': current_liabilities,
        'long_term_liabilities': long_term_liabilities,
        'other_liabilities': other_liabilities,
        'equity_accounts': equity_accounts,
        'total_assets': total_assets,
        'total_liabilities': total_liabilities,
        'total_equity': total_equity,
        'net_income': net_income,
        'total_liabilities_and_equity': total_liabilities_and_equity,
        'title': 'الميزانية العمومية',
    }
    return render(request, 'accounting/balance_sheet.html', context)

# -----------------------------------------------------------------------------
# Expenses
# -----------------------------------------------------------------------------
from .models import Expense
from .forms import ExpenseForm

class ExpenseListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_expense'

    model = Expense
    template_name = 'accounting/expense_list.html'
    context_object_name = 'expenses'
    paginate_by = 10
    
    def get_queryset(self):
        qs = scope_expenses(super().get_queryset(), self.request.user)
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        search_query = self.request.GET.get('search_query')
        status = self.request.GET.get('status')
        
        if search_query:
            qs = qs.filter(Q(expense_number__icontains=search_query) | Q(description__icontains=search_query))
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
        if status:
            qs = qs.filter(status=status)
            
        return qs.order_by('-date', '-created_at')
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'المصروفات'
        return context

class ExpenseDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_expense'

    model = Expense
    template_name = 'accounting/expense_detail.html'
    context_object_name = 'expense'

    def get_queryset(self):
        return scope_expenses(Expense.objects.all(), self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f'تفاصيل المصروف {self.object.expense_number}'
        return context

class ExpenseCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_expense'

    model = Expense
    form_class = ExpenseForm
    template_name = 'accounting/expense_form.html'
    success_url = reverse_lazy('accounting:expense_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs
    
    def get_context_data(self, **kwargs):
        from .forms import ExpenseItemFormSet
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة مصروف جديد'
        if self.request.POST:
            context['items_formset'] = ExpenseItemFormSet(self.request.POST, instance=self.object)
        else:
            context['items_formset'] = ExpenseItemFormSet(instance=self.object)
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        items_formset = context['items_formset']
        
        if form.is_valid() and items_formset.is_valid():
            self.object = form.save(commit=False)
            self.object.created_by = self.request.user
            
            # We don't save yet, we need to calculate total amount from formset
            total_amount = 0
            for item_form in items_formset:
                if item_form.cleaned_data and not item_form.cleaned_data.get('DELETE', False):
                    amount = item_form.cleaned_data.get('amount', 0)
                    total_amount += amount or 0

            if total_amount <= 0:
                form.add_error(None, 'يجب إدخال بند مصروف واحد على الأقل بمبلغ أكبر من صفر.')
                return self.render_to_response(self.get_context_data(form=form))

            self.object.amount = total_amount
            self.object.save()
            
            items_formset.instance = self.object
            items_formset.save()
            
            # Re-save to trigger tax calculations in Expense.save() based on new amount
            self.object.save()
            
            messages.success(self.request, 'تم إنشاء مسودة المصروف بنجاح.')
            return redirect(self.success_url)
        else:
            return self.render_to_response(self.get_context_data(form=form))

class ExpenseConfirmView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_expense'

    def post(self, request, pk):
        expense = get_object_or_404(scope_expenses(Expense.objects.all(), request.user), pk=pk)
        if expense.status == Expense.DRAFT:
            try:
                expense.confirm_expense()
                messages.success(request, 'تم اعتماد المصروف وتوليد القيد المحاسبي بنجاح.')
            except Exception as e:
                messages.error(request, f'خطأ أثناء اعتماد المصروف: {str(e)}')
        return redirect('accounting:expense_detail', pk=expense.pk)

class ExpenseDeleteView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.delete_expense'

    def post(self, request, pk):
        expense = get_object_or_404(scope_expenses(Expense.objects.all(), request.user), pk=pk)
        if expense.status == Expense.DRAFT:
            expense.delete()
            messages.success(request, 'تم حذف المصروف بنجاح.')
        else:
            messages.error(request, 'لا يمكن حذف مصروف إلا إذا كان مسودة.')
        return redirect('accounting:expense_list')

# ---------------------------------------------------------------------------
# Vouchers Views
# ---------------------------------------------------------------------------

class ReceiptListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_voucher'

    model = Voucher
    template_name = 'accounting/voucher_list.html'
    context_object_name = 'vouchers'
    paginate_by = 20

    def get_queryset(self):
        return scope_vouchers(Voucher.objects.all(), self.request.user).filter(voucher_type=Voucher.RECEIPT).order_by('-date', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'سندات القبض'
        context['voucher_type'] = Voucher.RECEIPT
        context['create_url'] = reverse_lazy('accounting:receipt_create')
        return context

class PaymentListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_voucher'

    model = Voucher
    template_name = 'accounting/voucher_list.html'
    context_object_name = 'vouchers'
    paginate_by = 20

    def get_queryset(self):
        return scope_vouchers(Voucher.objects.all(), self.request.user).filter(voucher_type=Voucher.PAYMENT).order_by('-date', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'سندات الصرف'
        context['voucher_type'] = Voucher.PAYMENT
        context['create_url'] = reverse_lazy('accounting:payment_create')
        return context

class VoucherCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_voucher'

    model = Voucher
    form_class = VoucherForm
    template_name = 'accounting/voucher_form.html'

    def get_voucher_type(self):
        return Voucher.RECEIPT if 'receipt' in self.request.path else Voucher.PAYMENT

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.voucher_type = self.get_voucher_type()
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        v_type = self.get_voucher_type()
        context['title'] = 'إضافة سند قبض' if v_type == Voucher.RECEIPT else 'إضافة سند صرف'
        context['cancel_url'] = reverse_lazy('accounting:receipt_list') if v_type == Voucher.RECEIPT else reverse_lazy('accounting:payment_list')
        context['voucher_type'] = v_type
        return context

    def form_valid(self, form):
        form.instance.voucher_type = self.get_voucher_type()
        from apps.tenant.invoicing.models import POSSession
        from apps.tenant.core.models import SystemSetting
        sys_setting = SystemSetting.get_settings()

        open_session = POSSession.objects.filter(user=self.request.user, status=POSSession.OPEN).first()
        if open_session:
            form.instance.pos_session = open_session
            if not form.instance.treasury and open_session.treasury and form.instance.payment_method == Voucher.CASH:
                form.instance.treasury = open_session.treasury

        if not form.instance.treasury and form.instance.payment_method == Voucher.CASH:
            if sys_setting and sys_setting.default_pos_treasury:
                form.instance.treasury = sys_setting.default_pos_treasury
            elif Treasury.objects.count() == 1:
                form.instance.treasury = Treasury.objects.first()

        if not form.instance.bank_account and form.instance.payment_method == Voucher.CARD:
            if sys_setting and sys_setting.default_pos_bank_account:
                form.instance.bank_account = sys_setting.default_pos_bank_account
            elif BankAccount.objects.count() == 1:
                form.instance.bank_account = BankAccount.objects.first()

        if form.instance.payment_method == Voucher.BANK_TRANSFER:
            if not form.instance.bank_account and form.instance.transfer_bank_account:
                form.instance.bank_account = form.instance.transfer_bank_account
            elif not form.instance.transfer_bank_account and form.instance.bank_account:
                form.instance.transfer_bank_account = form.instance.bank_account

            if not form.instance.bank_account:
                if sys_setting and sys_setting.default_pos_instapay_account:
                    form.instance.bank_account = sys_setting.default_pos_instapay_account
                    form.instance.transfer_bank_account = sys_setting.default_pos_instapay_account
                elif sys_setting and sys_setting.default_pos_bank_account:
                    form.instance.bank_account = sys_setting.default_pos_bank_account
                    form.instance.transfer_bank_account = sys_setting.default_pos_bank_account
                elif BankAccount.objects.count() == 1:
                    first_bank = BankAccount.objects.first()
                    form.instance.bank_account = first_bank
                    form.instance.transfer_bank_account = first_bank

        if not form.instance.ewallet and form.instance.payment_method == Voucher.EWALLET:
            if sys_setting and sys_setting.default_pos_ewallet:
                form.instance.ewallet = sys_setting.default_pos_ewallet
            elif EWallet.objects.count() == 1:
                form.instance.ewallet = EWallet.objects.first()

        if form.instance.payment_method == Voucher.SPLIT:
            if form.instance.paid_cash > 0 and not form.instance.treasury:
                if open_session and open_session.treasury:
                    form.instance.treasury = open_session.treasury
                elif sys_setting and sys_setting.default_pos_treasury:
                    form.instance.treasury = sys_setting.default_pos_treasury
                elif Treasury.objects.count() == 1:
                    form.instance.treasury = Treasury.objects.first()

            if form.instance.paid_card > 0 and not form.instance.bank_account:
                if sys_setting and sys_setting.default_pos_bank_account:
                    form.instance.bank_account = sys_setting.default_pos_bank_account
                elif BankAccount.objects.count() == 1:
                    form.instance.bank_account = BankAccount.objects.first()

            if form.instance.paid_ewallet > 0 and not form.instance.ewallet:
                if sys_setting and sys_setting.default_pos_ewallet:
                    form.instance.ewallet = sys_setting.default_pos_ewallet
                elif EWallet.objects.count() == 1:
                    form.instance.ewallet = EWallet.objects.first()

            if form.instance.paid_bank > 0 and not form.instance.transfer_bank_account:
                if sys_setting and sys_setting.default_pos_instapay_account:
                    form.instance.transfer_bank_account = sys_setting.default_pos_instapay_account
                elif sys_setting and sys_setting.default_pos_bank_account:
                    form.instance.transfer_bank_account = sys_setting.default_pos_bank_account
                elif BankAccount.objects.count() == 1:
                    form.instance.transfer_bank_account = BankAccount.objects.first()

        # Branch scoping: non-admins may only use their own branch's treasuries (the defaults above
        # may point to another branch's treasury) and the voucher is attributed to their branch.
        if not is_admin_user(self.request.user):
            user_branch = _user_branch(self.request.user)
            if form.instance.treasury_id and (user_branch is None or form.instance.treasury.branch_id != user_branch.id):
                form.add_error('treasury', 'لا يمكن استخدام خزينة تابعة لفرع آخر.')
                return self.form_invalid(form)
            if not form.instance.branch_id and user_branch is not None:
                form.instance.branch = user_branch

        response = super().form_valid(form)

        # Auto-confirm and post the voucher if valid so it immediately reflects in the shift
        try:
            if self.object.status == Voucher.DRAFT and self.object.payment_account:
                self.object.confirm_voucher()
                messages.success(self.request, f'تم حفظ وتأكيد وترحيل {self.object.get_voucher_type_display()} بنجاح.')
            else:
                messages.success(self.request, 'تم حفظ مسودة السند بنجاح.')
        except Exception as e:
            messages.warning(self.request, f'تم حفظ السند كمسودة ولكن تعذر الترحيل التلقائي: {str(e)}')

        return response

    def get_success_url(self):
        return reverse_lazy('accounting:voucher_detail', kwargs={'pk': self.object.pk})

class VoucherDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_voucher'

    model = Voucher
    template_name = 'accounting/voucher_detail.html'
    context_object_name = 'voucher'

    def get_queryset(self):
        return scope_vouchers(Voucher.objects.all(), self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        v_type = self.object.voucher_type
        context['title'] = f"تفاصيل {self.object.get_voucher_type_display()} {self.object.voucher_number}"
        if v_type == Voucher.RECEIPT:
            context['list_url'] = reverse_lazy('accounting:receipt_list')
            context['list_name'] = 'سندات القبض'
            context['create_url'] = reverse_lazy('accounting:receipt_create')
            context['create_name'] = 'إضافة سند قبض جديد'
        else:
            context['list_url'] = reverse_lazy('accounting:payment_list')
            context['list_name'] = 'سندات الصرف'
            context['create_url'] = reverse_lazy('accounting:payment_create')
            context['create_name'] = 'إضافة سند صرف جديد'
        return context

class VoucherConfirmView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_voucher'

    def post(self, request, pk):
        voucher = get_object_or_404(scope_vouchers(Voucher.objects.all(), request.user), pk=pk)
        if voucher.status == Voucher.DRAFT:
            try:
                from apps.tenant.core.models import SystemSetting
                sys_setting = SystemSetting.get_settings()

                # If cash voucher is missing a treasury, attempt auto-assign fallback
                if voucher.payment_method == Voucher.CASH and not voucher.treasury:
                    from apps.tenant.invoicing.models import POSSession
                    open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
                    if open_session and open_session.treasury:
                        voucher.treasury = open_session.treasury
                        voucher.save(update_fields=['treasury'])
                    elif sys_setting and sys_setting.default_pos_treasury:
                        voucher.treasury = sys_setting.default_pos_treasury
                        voucher.save(update_fields=['treasury'])
                    elif Treasury.objects.count() == 1:
                        voucher.treasury = Treasury.objects.first()
                        voucher.save(update_fields=['treasury'])

                if voucher.payment_method == Voucher.CARD and not voucher.bank_account:
                    if sys_setting and sys_setting.default_pos_bank_account:
                        voucher.bank_account = sys_setting.default_pos_bank_account
                        voucher.save(update_fields=['bank_account'])
                    elif BankAccount.objects.count() == 1:
                        voucher.bank_account = BankAccount.objects.first()
                        voucher.save(update_fields=['bank_account'])

                if voucher.payment_method == Voucher.BANK_TRANSFER:
                    if not voucher.bank_account and voucher.transfer_bank_account:
                        voucher.bank_account = voucher.transfer_bank_account
                        voucher.save(update_fields=['bank_account'])
                    elif not voucher.transfer_bank_account and voucher.bank_account:
                        voucher.transfer_bank_account = voucher.bank_account
                        voucher.save(update_fields=['transfer_bank_account'])

                    if not voucher.bank_account:
                        if sys_setting and sys_setting.default_pos_instapay_account:
                            voucher.bank_account = sys_setting.default_pos_instapay_account
                            voucher.transfer_bank_account = sys_setting.default_pos_instapay_account
                            voucher.save(update_fields=['bank_account', 'transfer_bank_account'])
                        elif sys_setting and sys_setting.default_pos_bank_account:
                            voucher.bank_account = sys_setting.default_pos_bank_account
                            voucher.transfer_bank_account = sys_setting.default_pos_bank_account
                            voucher.save(update_fields=['bank_account', 'transfer_bank_account'])
                        elif BankAccount.objects.count() == 1:
                            first_bank = BankAccount.objects.first()
                            voucher.bank_account = first_bank
                            voucher.transfer_bank_account = first_bank
                            voucher.save(update_fields=['bank_account', 'transfer_bank_account'])

                if voucher.payment_method == Voucher.EWALLET and not voucher.ewallet:
                    if sys_setting and sys_setting.default_pos_ewallet:
                        voucher.ewallet = sys_setting.default_pos_ewallet
                        voucher.save(update_fields=['ewallet'])
                    elif EWallet.objects.count() == 1:
                        voucher.ewallet = EWallet.objects.first()
                        voucher.save(update_fields=['ewallet'])

                if voucher.payment_method == Voucher.SPLIT:
                    updated_fields = []
                    if voucher.paid_cash > 0 and not voucher.treasury:
                        from apps.tenant.invoicing.models import POSSession
                        open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
                        if open_session and open_session.treasury:
                            voucher.treasury = open_session.treasury
                            updated_fields.append('treasury')
                        elif sys_setting and sys_setting.default_pos_treasury:
                            voucher.treasury = sys_setting.default_pos_treasury
                            updated_fields.append('treasury')
                        elif Treasury.objects.count() == 1:
                            voucher.treasury = Treasury.objects.first()
                            updated_fields.append('treasury')

                    if voucher.paid_card > 0 and not voucher.bank_account:
                        if sys_setting and sys_setting.default_pos_bank_account:
                            voucher.bank_account = sys_setting.default_pos_bank_account
                            updated_fields.append('bank_account')
                        elif BankAccount.objects.count() == 1:
                            voucher.bank_account = BankAccount.objects.first()
                            updated_fields.append('bank_account')

                    if voucher.paid_ewallet > 0 and not voucher.ewallet:
                        if sys_setting and sys_setting.default_pos_ewallet:
                            voucher.ewallet = sys_setting.default_pos_ewallet
                            updated_fields.append('ewallet')
                        elif EWallet.objects.count() == 1:
                            voucher.ewallet = EWallet.objects.first()
                            updated_fields.append('ewallet')

                    if voucher.paid_bank > 0 and not voucher.transfer_bank_account:
                        if sys_setting and sys_setting.default_pos_instapay_account:
                            voucher.transfer_bank_account = sys_setting.default_pos_instapay_account
                            updated_fields.append('transfer_bank_account')
                        elif sys_setting and sys_setting.default_pos_bank_account:
                            voucher.transfer_bank_account = sys_setting.default_pos_bank_account
                            updated_fields.append('transfer_bank_account')
                        elif BankAccount.objects.count() == 1:
                            voucher.transfer_bank_account = BankAccount.objects.first()
                            updated_fields.append('transfer_bank_account')

                    if updated_fields:
                        voucher.save(update_fields=updated_fields)

                if not voucher.payment_account:
                    raise ValidationError("برجاء الضغط على 'تعديل السند' وتحديد خزينة أو وسيلة السداد أولاً.")

                voucher.confirm_voucher()
                messages.success(request, 'تم اعتماد السند وتوليد القيد بنجاح.')
            except Exception as e:
                messages.error(request, f'حدث خطأ أثناء اعتماد السند: {str(e)}')
        return redirect('accounting:voucher_detail', pk=voucher.pk)

class VoucherUpdateView(CustomPermissionRequiredMixin, UpdateView):
    permission_required = 'accounting.change_voucher'
    model = Voucher
    form_class = VoucherForm
    template_name = 'accounting/voucher_form.html'

    def get_queryset(self):
        return scope_vouchers(Voucher.objects.all(), self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.voucher_type = self.object.voucher_type
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"تعديل {self.object.get_voucher_type_display()} {self.object.voucher_number}"
        context['cancel_url'] = reverse_lazy('accounting:voucher_detail', kwargs={'pk': self.object.pk})
        context['voucher_type'] = self.object.voucher_type
        return context

    def form_valid(self, form):
        """
        Editing a posted voucher = reverse its entry + save + repost, all in ONE transaction:
        the old posted entry is kept for the audit trail and neutralised by a reversal entry,
        then a fresh entry is posted for the edited voucher. If reposting fails everything is
        rolled back (old entry stays active, no reversal) and the error is shown.
        """
        from .models import _ensure_pos_session_open
        from apps.tenant.services.journal_service import reverse_entry_if_active
        try:
            with transaction.atomic():
                locked = Voucher.objects.select_for_update().select_related('pos_session', 'journal_entry').get(pk=self.object.pk)
                if locked.status == Voucher.CANCELLED:
                    raise ValidationError('لا يمكن تعديل سند ملغي.')
                was_posted = (locked.status == Voucher.POSTED)
                if was_posted:
                    _ensure_pos_session_open(locked.pos_session, 'تعديل')
                    if locked.journal_entry_id:
                        reverse_entry_if_active(
                            locked.journal_entry,
                            reason=f'تعديل السند {locked.voucher_number}',
                            user=self.request.user,
                        )
                    form.instance.journal_entry = None
                    form.instance.status = Voucher.DRAFT

                self.object = form.save()

                if was_posted:
                    self.object.confirm_voucher()  # raises -> whole edit is rolled back
        except (ValidationError, ValueError) as e:
            form.add_error(None, f'تعذر حفظ التعديل: {_error_text(e)}')
            # restore in-memory state of the instance after rollback
            self.object.refresh_from_db()
            return self.form_invalid(form)

        if was_posted:
            messages.success(self.request, 'تم تعديل السند وإعادة ترحيل القيد المحاسبي المربوط بنجاح.')
        else:
            messages.success(self.request, 'تم تعديل السند بنجاح.')
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse_lazy('accounting:voucher_detail', kwargs={'pk': self.object.pk})

class VoucherCancelView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_voucher'

    def post(self, request, pk):
        voucher = get_object_or_404(scope_vouchers(Voucher.objects.all(), request.user), pk=pk)
        try:
            voucher.cancel_voucher(user=request.user)
            messages.success(request, 'تم إلغاء السند وتسجيل قيد عكسي للقيد المحاسبي المربوط به بنجاح.')
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء إلغاء السند: {str(e)}')
        return redirect('accounting:voucher_detail', pk=voucher.pk)


# ===========================================================================
# POS Machines (ماكينات الدفع الإلكتروني)
# ===========================================================================

@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def pos_machine_list(request):
    """List all POS machines with current balances."""
    from .models import POSMachine
    machines = POSMachine.objects.select_related('branch', 'account').all()
    context = {
        'machines': machines,
        'title': 'ماكينات الدفع الإلكتروني',
    }
    return render(request, 'accounting/pos_machines/machine_list.html', context)


@login_required
@custom_permission_required('accounting.add_account', redirect_url='core:main_screen')
def pos_machine_create(request):
    """Create a new POS machine."""
    from .models import POSMachine
    from apps.tenant.core.models import Branch
    branches = Branch.objects.filter(is_active=True)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        machine_type = request.POST.get('machine_type', POSMachine.OTHER)
        branch_id = request.POST.get('branch_id')
        opening_balance = request.POST.get('opening_balance', '0') or '0'
        notes = request.POST.get('notes', '')
        if not name or not branch_id:
            messages.error(request, 'برجاء إدخال اسم الماكينة والفرع.')
        else:
            try:
                from apps.tenant.core.models import Branch
                branch = Branch.objects.get(id=branch_id)
                machine = POSMachine.objects.create(
                    name=name,
                    machine_type=machine_type,
                    branch=branch,
                    opening_balance=Decimal(opening_balance),
                    notes=notes,
                )
                messages.success(request, f'تم إنشاء الماكينة "{machine.name}" بنجاح وتم إنشاء حسابها المحاسبي تلقائياً.')
                return redirect('accounting:pos_machine_list')
            except Exception as e:
                messages.error(request, f'حدث خطأ: {str(e)}')
    context = {
        'branches': branches,
        'machine_types': POSMachine.MACHINE_TYPE_CHOICES,
        'title': 'إضافة ماكينة دفع جديدة',
    }
    return render(request, 'accounting/pos_machines/machine_form.html', context)


@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def pos_machine_detail(request, pk):
    """Show machine details with its transaction history."""
    from .models import POSMachine, EServiceTransaction
    machine = get_object_or_404(POSMachine, pk=pk)
    transactions = EServiceTransaction.objects.filter(
        pos_machine=machine
    ).order_by('-date', '-created_at')[:50]
    context = {
        'machine': machine,
        'transactions': transactions,
        'title': f'تفاصيل: {machine.name}',
    }
    return render(request, 'accounting/pos_machines/machine_detail.html', context)


# ===========================================================================
# E-Service Center (مركز خدمات الدفع)
# ===========================================================================

@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def eservice_center(request):
    """Main e-service center for handling recharge/withdrawal/transfer operations."""
    from .models import POSMachine, EWallet, Treasury, EServiceTransaction

    machines = POSMachine.objects.filter(is_active=True).select_related('branch')
    if not is_admin_user(request.user):
        user_branch = _user_branch(request.user)
        machines = machines.filter(branch=user_branch) if user_branch else machines.none()
    ewallets = EWallet.objects.all()
    treasuries = scope_treasuries(Treasury.objects.all(), request.user)

    if request.method == 'POST':
        # Posting cash movements requires the add permission (viewing the screen only needs view)
        if not (is_admin_user(request.user) or request.user.has_perm('accounting.add_eservicetransaction')):
            messages.error(request, 'عفواً، لا تملك صلاحية تنفيذ عمليات خدمات الدفع.')
            return redirect('accounting:eservice_center')

        transaction_type = request.POST.get('transaction_type')
        source_type = request.POST.get('source_type', 'MACHINE')
        pos_machine_id = request.POST.get('pos_machine_id') or None
        ewallet_id = request.POST.get('ewallet_id') or None
        treasury_id = request.POST.get('treasury_id')
        description = request.POST.get('description', '')

        error = None
        principal = commission_rev = commission_exp = None
        try:
            principal = Decimal(request.POST.get('principal_amount', '0') or '0')
            commission_rev = Decimal(request.POST.get('commission_revenue', '0') or '0')
            commission_exp = Decimal(request.POST.get('commission_expense', '0') or '0')
            if not all(x.is_finite() for x in (principal, commission_rev, commission_exp)):
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError):
            error = 'برجاء إدخال مبالغ رقمية صحيحة.'

        valid_types = {t for t, _ in EServiceTransaction.TRANSACTION_TYPE_CHOICES}
        if error is None:
            if transaction_type not in valid_types:
                error = 'نوع العملية غير صحيح.'
            elif source_type not in ('MACHINE', 'EWALLET'):
                error = 'مصدر العملية غير صحيح.'
            elif not treasury_id:
                error = 'برجاء اختيار الخزينة / الدرج.'
            elif principal <= 0:
                error = 'برجاء إدخال مبلغ صحيح.'
            elif commission_rev < 0 or commission_exp < 0:
                error = 'العمولات لا يمكن أن تكون سالبة.'
            elif commission_rev > principal or commission_exp > principal:
                error = 'العمولة لا يمكن أن تتجاوز المبلغ الأصلي.'
            elif source_type == 'MACHINE' and not pos_machine_id:
                error = 'برجاء اختيار الماكينة.'
            elif source_type == 'EWALLET' and not ewallet_id:
                error = 'برجاء اختيار المحفظة الإلكترونية.'

        if error:
            messages.error(request, error)
        else:
            try:
                treasury = treasuries.get(id=treasury_id)
                with transaction.atomic():
                    txn = EServiceTransaction(
                        transaction_type=transaction_type,
                        source_type=source_type,
                        treasury=treasury,
                        principal_amount=principal,
                        commission_revenue=commission_rev,
                        commission_expense=commission_exp,
                        description=description,
                        created_by=request.user,
                    )
                    if source_type == 'MACHINE':
                        txn.pos_machine = machines.get(id=pos_machine_id)
                    else:
                        txn.ewallet = EWallet.objects.get(id=ewallet_id)
                    txn.save()
                    txn.post_transaction()
                messages.success(request, f'✅ تم تنفيذ العملية {txn.transaction_number} بنجاح وتم إنشاء القيد المحاسبي.')
                return redirect('accounting:eservice_center')
            except (Treasury.DoesNotExist, POSMachine.DoesNotExist, EWallet.DoesNotExist, ValueError):
                messages.error(request, 'الخزينة أو الماكينة أو المحفظة المحددة غير متاحة.')
            except ValidationError as e:
                messages.error(request, _error_text(e))
            except Exception as e:
                messages.error(request, f'حدث خطأ: {str(e)}')

    context = {
        'machines': machines,
        'ewallets': ewallets,
        'treasuries': treasuries,
        'transaction_types': EServiceTransaction.TRANSACTION_TYPE_CHOICES,
        'title': 'مركز خدمات الدفع الإلكتروني',
    }
    return render(request, 'accounting/pos_machines/service_center.html', context)


@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def eservice_history(request):
    """List all e-service transactions."""
    from .models import EServiceTransaction
    transactions = EServiceTransaction.objects.select_related(
        'pos_machine', 'ewallet', 'treasury', 'created_by'
    ).order_by('-date', '-created_at')
    context = {
        'transactions': transactions,
        'title': 'سجل حركات خدمات الدفع',
    }
    return render(request, 'accounting/pos_machines/eservice_history.html', context)


@login_required
@custom_permission_required('accounting.view_account', redirect_url='core:main_screen')
def eservice_detail(request, pk):
    from .models import EServiceTransaction
    txn = get_object_or_404(EServiceTransaction, pk=pk)
    context = {
        'transaction': txn,
        'title': f'تفاصيل الحركة: {txn.transaction_number}',
    }
    return render(request, 'accounting/pos_machines/eservice_detail.html', context)


@login_required
@custom_permission_required('accounting.add_journalentry', redirect_url='core:main_screen')
def eservice_post(request, pk):
    if request.method == 'POST':
        from .models import EServiceTransaction
        from django.core.exceptions import ValidationError
        txn = get_object_or_404(EServiceTransaction, pk=pk, status=EServiceTransaction.DRAFT)
        try:
            txn.post_transaction()
            messages.success(request, f'تم ترحيل الحركة {txn.transaction_number} بنجاح.')
        except ValidationError as e:
            messages.error(request, str(e.message))
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء الترحيل: {str(e)}')
    return redirect('accounting:eservice_history')

from decimal import Decimal
from django.views.generic import CreateView
from django.urls import reverse_lazy
from .forms import JournalEntryForm, JournalItemFormSet

class ManualJournalEntryCreateView(CustomPermissionRequiredMixin, CreateView):
    model = JournalEntry
    form_class = JournalEntryForm
    template_name = 'accounting/journal_entry_form.html'
    permission_required = 'accounting.add_journalentry'
    success_url = reverse_lazy('accounting:journal_list')

    def dispatch(self, request, *args, **kwargs):
        if not is_admin_user(request.user):
            messages.error(request, 'عفواً، إنشاء القيود المحاسبية اليدوية مقتصر على الأدمن والمديرين فقط.')
            return redirect('core:main_screen')
        return super().dispatch(request, *args, **kwargs)


    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['items_formset'] = JournalItemFormSet(self.request.POST)
        else:
            context['items_formset'] = JournalItemFormSet()
        context['title'] = 'إنشاء قيد يومية يدوي'
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        items_formset = context['items_formset']
        if items_formset.is_valid():
            with transaction.atomic():
                self.object = form.save(commit=False)
                self.object.created_by = self.request.user
                self.object.status = JournalEntry.DRAFT
                self.object.save()
                items_formset.instance = self.object
                items_formset.save()
                
                if self.object.is_balanced():
                    try:
                        self.object.post()
                        messages.success(self.request, f'تم حفظ وترحيل القيد المحاسبي {self.object.reference} بنجاح.')
                    except ValidationError as e:
                        messages.warning(self.request, f'تم حفظ القيد كمسودة. تعذر الترحيل التلقائي: {str(e.message)}')
                else:
                    messages.warning(self.request, 'تم حفظ القيد كمسودة نظراً لعدم اتزان المدين والدائن.')
            return super().form_valid(form)
        else:
            return self.form_invalid(form)


# ===========================================================================
# Internal Transfers (تحويلات الأموال بين الحسابات)
# ===========================================================================

class InternalTransferListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_internaltransfer'
    model = InternalTransfer
    template_name = 'accounting/internal_transfer_list.html'
    context_object_name = 'transfers'
    paginate_by = 20

    def get_queryset(self):
        return scope_transfers(InternalTransfer.objects.all(), self.request.user).order_by('-date', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تحويلات الأموال بين الحسابات'
        return context


class InternalTransferCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_internaltransfer'
    model = InternalTransfer
    form_class = InternalTransferForm
    template_name = 'accounting/internal_transfer_form.html'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة تحويل مالي جديد'
        context['cancel_url'] = reverse_lazy('accounting:transfer_list')

        # Auto-heal and prepare live balance data for frontend display
        from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
        import json

        for t in Treasury.objects.filter(account__isnull=True):
            t.ensure_account()
        for b in BankAccount.objects.filter(account__isnull=True):
            b.ensure_account()
        for w in EWallet.objects.filter(account__isnull=True):
            w.ensure_account()

        accounts_data = {
            'TREASURY': {str(t.id): {'name': t.name, 'balance': float(t.current_balance)} for t in scope_treasuries(Treasury.objects.all(), self.request.user)},
            'BANK': {str(b.id): {'name': b.name, 'balance': float(b.current_balance)} for b in BankAccount.objects.all()},
            'EWALLET': {str(w.id): {'name': w.name, 'balance': float(w.current_balance)} for w in EWallet.objects.all()},
        }
        context['accounts_data_json'] = json.dumps(accounts_data, ensure_ascii=False)
        context['treasuries_list'] = scope_treasuries(Treasury.objects.all(), self.request.user)
        context['banks_list'] = BankAccount.objects.all()
        context['ewallets_list'] = EWallet.objects.all()
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        from apps.tenant.invoicing.models import POSSession
        open_session = POSSession.objects.filter(user=self.request.user, status=POSSession.OPEN).first()
        if open_session:
            form.instance.pos_session = open_session

        response = super().form_valid(form)
        messages.success(self.request, 'تم حفظ مسودة التحويل بنجاح.')
        return response

    def get_success_url(self):
        return reverse_lazy('accounting:transfer_detail', kwargs={'pk': self.object.pk})


class InternalTransferDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_internaltransfer'
    model = InternalTransfer
    template_name = 'accounting/internal_transfer_detail.html'
    context_object_name = 'transfer'

    def get_queryset(self):
        return scope_transfers(InternalTransfer.objects.all(), self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"تفاصيل التحويل المالي #{self.object.transfer_number}"
        return context


class InternalTransferConfirmView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_internaltransfer'

    def post(self, request, pk):
        transfer = get_object_or_404(scope_transfers(InternalTransfer.objects.all(), request.user), pk=pk)
        if transfer.status == InternalTransfer.DRAFT:
            try:
                transfer.confirm_transfer()
                messages.success(request, 'تم اعتماد التحويل وتوليد القيد المحاسبي بنجاح.')
            except Exception as e:
                messages.error(request, f'حدث خطأ أثناء الاعتماد: {str(e)}')
        return redirect('accounting:transfer_detail', pk=transfer.pk)


class InternalTransferCancelView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_internaltransfer'

    def post(self, request, pk):
        transfer = get_object_or_404(scope_transfers(InternalTransfer.objects.all(), request.user), pk=pk)
        try:
            transfer.cancel_transfer(user=request.user)
            messages.success(request, 'تم إلغاء التحويل وتسجيل قيد عكسي للقيد المحاسبي المربوط به بنجاح.')
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء الإلغاء: {str(e)}')
        return redirect('accounting:transfer_detail', pk=transfer.pk)


# ---------------------------------------------------------------------------
# Cheque Management Views (أوراق القبض وأوراق الدفع)
# ---------------------------------------------------------------------------

class ChequeListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_cheque'
    model = Cheque
    template_name = 'accounting/cheque_list.html'
    context_object_name = 'cheques'
    paginate_by = 25

    def get_queryset(self):
        qs = Cheque.objects.select_related('partner', 'bank_account', 'treasury', 'collecting_bank').order_by('-received_date', '-created_at')

        # Filters
        c_type = self.request.GET.get('type')
        status = self.request.GET.get('status')
        partner_id = self.request.GET.get('partner')
        q = self.request.GET.get('q')
        due_filter = self.request.GET.get('due')  # 'today', 'week', 'overdue'

        if c_type in (ChequeType.RECEIVABLE, ChequeType.PAYABLE):
            qs = qs.filter(cheque_type=c_type)

        if status:
            qs = qs.filter(status=status)

        if partner_id:
            qs = qs.filter(partner_id=partner_id)

        if q:
            from django.db.models import Q
            qs = qs.filter(
                Q(cheque_number__icontains=q) |
                Q(partner__name__icontains=q) |
                Q(payee_name__icontains=q) |
                Q(drawer_bank_name__icontains=q) |
                Q(notes__icontains=q)
            )

        import datetime
        today = datetime.date.today()
        if due_filter == 'today':
            qs = qs.filter(due_date=today)
        elif due_filter == 'week':
            week_later = today + datetime.timedelta(days=7)
            qs = qs.filter(due_date__gte=today, due_date__lte=week_later)
        elif due_filter == 'overdue':
            qs = qs.filter(
                due_date__lt=today,
                status__in=[ChequeStatus.IN_PORTFOLIO, ChequeStatus.UNDER_COLLECTION, ChequeStatus.ISSUED]
            )

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.db.models import Sum, Count, Q
        import datetime
        today = datetime.date.today()
        week_later = today + datetime.timedelta(days=7)

        # Quick stats
        all_chqs = Cheque.objects.all()

        rec_under_coll = all_chqs.filter(cheque_type=ChequeType.RECEIVABLE, status__in=[ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO])
        rec_collected = all_chqs.filter(cheque_type=ChequeType.RECEIVABLE, status=ChequeStatus.COLLECTED)
        rec_bounced = all_chqs.filter(cheque_type=ChequeType.RECEIVABLE, status=ChequeStatus.BOUNCED)

        pay_issued = all_chqs.filter(cheque_type=ChequeType.PAYABLE, status=ChequeStatus.ISSUED)
        pay_cleared = all_chqs.filter(cheque_type=ChequeType.PAYABLE, status=ChequeStatus.CLEARED)
        pay_cancelled = all_chqs.filter(cheque_type=ChequeType.PAYABLE, status=ChequeStatus.CANCELLED)

        overdue_chqs = all_chqs.filter(
            due_date__lt=today,
            status__in=[ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO, ChequeStatus.ISSUED]
        )
        due_week_chqs = all_chqs.filter(
            due_date__gte=today, due_date__lte=week_later,
            status__in=[ChequeStatus.UNDER_COLLECTION, ChequeStatus.IN_PORTFOLIO, ChequeStatus.ISSUED]
        )

        context['stats'] = {
            'under_collection_amount': rec_under_coll.aggregate(s=Sum('amount'))['s'] or Decimal('0.00'),
            'under_collection_count': rec_under_coll.count(),
            'collected_amount': rec_collected.aggregate(s=Sum('amount'))['s'] or Decimal('0.00'),
            'collected_count': rec_collected.count(),
            'bounced_count': rec_bounced.count(),
            'bounced_amount': rec_bounced.aggregate(s=Sum('amount'))['s'] or Decimal('0.00'),
            'payable_issued_amount': pay_issued.aggregate(s=Sum('amount'))['s'] or Decimal('0.00'),
            'payable_issued_count': pay_issued.count(),
            'payable_cleared_amount': pay_cleared.aggregate(s=Sum('amount'))['s'] or Decimal('0.00'),
            'payable_cleared_count': pay_cleared.count(),
            'payable_cancelled_count': pay_cancelled.count(),
            'overdue_count': overdue_chqs.count(),
            'overdue_amount': overdue_chqs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00'),
            'due_week_count': due_week_chqs.count(),
            'due_week_amount': due_week_chqs.aggregate(s=Sum('amount'))['s'] or Decimal('0.00'),
        }

        # Filter presets
        context['current_type'] = self.request.GET.get('type', '')
        context['current_status'] = self.request.GET.get('status', '')
        context['current_due'] = self.request.GET.get('due', '')
        context['current_partner'] = self.request.GET.get('partner', '')
        context['search_query'] = self.request.GET.get('q', '')
        from apps.tenant.partners.models import Partner
        context['partners'] = Partner.objects.filter(is_active=True).order_by('name')
        context['title'] = 'إدارة الشيكات (أوراق القبض وأوراق الدفع)'
        return context


class ReceivableChequeCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_cheque'
    model = Cheque
    form_class = ReceivableChequeForm
    template_name = 'accounting/cheque_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'استلام شيك قبض جديد (ورقة قبض)'
        context['cheque_type'] = ChequeType.RECEIVABLE
        context['cancel_url'] = reverse_lazy('accounting:cheque_list')
        return context

    def form_valid(self, form):
        form.instance.cheque_type = ChequeType.RECEIVABLE
        form.instance.created_by = self.request.user
        cheque = form.save()
        try:
            cheque.action_receive(user=self.request.user)
            messages.success(self.request, f"تم استلام ورقة القبض شيك #{cheque.cheque_number} بمبلغ {cheque.amount:,.2f} ج.م وإثبات القيد المحاسبي بنجاح.")
        except Exception as e:
            messages.error(self.request, f"تم حفظ الشيك ولكن تعذر إنشاء القيد المحاسبي: {str(e)}")
        return redirect('accounting:cheque_detail', pk=cheque.pk)


class PayableChequeCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_cheque'
    model = Cheque
    form_class = PayableChequeForm
    template_name = 'accounting/cheque_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تحرير شيك دفع جديد لمورد (ورقة دفع)'
        context['cheque_type'] = ChequeType.PAYABLE
        context['cancel_url'] = reverse_lazy('accounting:cheque_list')
        return context

    def form_valid(self, form):
        form.instance.cheque_type = ChequeType.PAYABLE
        form.instance.created_by = self.request.user
        cheque = form.save()
        try:
            cheque.action_issue(user=self.request.user)
            messages.success(self.request, f"تم تحرير ورقة الدفع شيك #{cheque.cheque_number} بمبلغ {cheque.amount:,.2f} ج.م للمورد {cheque.partner.name} وتوليد القيد بنجاح.")
        except Exception as e:
            messages.error(self.request, f"تم حفظ الشيك ولكن تعذر توليد القيد المحاسبي: {str(e)}")
        return redirect('accounting:cheque_detail', pk=cheque.pk)


class ChequeDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_cheque'
    model = Cheque
    template_name = 'accounting/cheque_detail.html'
    context_object_name = 'cheque'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"تفاصيل {self.object.get_cheque_type_display()} #{self.object.cheque_number}"
        context['collect_form'] = ChequeCollectForm()
        context['bounce_form'] = ChequeBounceForm()
        context['clear_payable_form'] = ChequeClearPayableForm()
        context['cancel_payable_form'] = ChequeCancelPayableForm()
        context['action_logs'] = self.object.action_logs.select_related('user').all()
        return context


class ChequeCollectActionView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_cheque'

    def post(self, request, pk):
        cheque = get_object_or_404(Cheque, pk=pk)
        form = ChequeCollectForm(request.POST)
        if form.is_valid():
            dest_type = form.cleaned_data['destination_type']
            destination = form.cleaned_data['bank_account'] if dest_type == 'BANK' else form.cleaned_data['treasury']
            clearance_date = form.cleaned_data['clearance_date']
            bank_fee = form.cleaned_data['bank_fee'] or Decimal('0.00')
            try:
                cheque.action_confirm_collection(
                    destination=destination,
                    clearance_date=clearance_date,
                    bank_fee=bank_fee,
                    user=request.user
                )
                dest_name = destination.name if destination else "الحساب المحدد"
                messages.success(request, f"تم تحصيل الشيك #{cheque.cheque_number} وإيداعه بنجاح في {dest_name} وإقفال الحساب الوسيط.")
            except Exception as e:
                messages.error(request, f"تعذر تنفيذ التحصيل: {str(e)}")
        else:
            errors = " | ".join([f"{f}: {e[0]}" for f, e in form.errors.items()])
            messages.error(request, f"بيانات التحصيل غير صالحة: {errors}")
        return redirect('accounting:cheque_detail', pk=cheque.pk)


class ChequeBounceActionView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_cheque'

    def post(self, request, pk):
        cheque = get_object_or_404(Cheque, pk=pk)
        form = ChequeBounceForm(request.POST)
        if form.is_valid():
            reason = form.cleaned_data['bounce_reason']
            date = form.cleaned_data['bounce_date']
            try:
                cheque.action_bounce(bounce_reason=reason, bounce_date=date, user=request.user)
                messages.warning(request, f"تم إثبات ارتداد الشيك #{cheque.cheque_number} وإعادة فتح مديونية العميل {cheque.partner.name} بالقيد المحاسبي.")
            except Exception as e:
                messages.error(request, f"تعذر إثبات الارتداد: {str(e)}")
        else:
            messages.error(request, "برجاء كتابة سبب الارتداد وتاريخه بشكل صحيح.")
        return redirect('accounting:cheque_detail', pk=cheque.pk)


class ChequeClearPayableActionView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_cheque'

    def post(self, request, pk):
        cheque = get_object_or_404(Cheque, pk=pk)
        form = ChequeClearPayableForm(request.POST)
        if form.is_valid():
            date = form.cleaned_data['clearance_date']
            try:
                cheque.action_clear_payable(clearance_date=date, user=request.user)
                messages.success(request, f"تم إثبات صرف الشيك وخصمه من رصيد بنك {cheque.bank_account.name} وإقفال أوراق الدفع بالقيد المحاسبي.")
            except Exception as e:
                messages.error(request, f"تعذر إثبات صرف الشيك: {str(e)}")
        else:
            messages.error(request, "تاريخ الصرف غير صالح.")
        return redirect('accounting:cheque_detail', pk=cheque.pk)


class ChequeCancelPayableActionView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_cheque'

    def post(self, request, pk):
        cheque = get_object_or_404(Cheque, pk=pk)
        form = ChequeCancelPayableForm(request.POST)
        if form.is_valid():
            reason = form.cleaned_data['cancel_reason']
        else:
            reason = request.POST.get('cancel_reason', 'إلغاء بناء على طلب الإدارة')
        try:
            cheque.action_cancel_payable(cancel_reason=reason, user=request.user)
            messages.warning(request, f"تم إلغاء شيك الدفع وإعادة إثبات التزام المورد {cheque.partner.name} بالقيد المحاسبي.")
        except Exception as e:
            messages.error(request, f"تعذر إلغاء الشيك: {str(e)}")
        return redirect('accounting:cheque_detail', pk=cheque.pk)


class ChequeBookListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_chequebook'
    model = ChequeBook
    queryset = ChequeBook.objects.all().order_by('-id')
    template_name = 'accounting/cheque_book_list.html'
    context_object_name = 'books'
    paginate_by = 25

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'دفاتر الشيكات البنكية'
        return context


class ChequeBookCreateView(CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_chequebook'
    model = ChequeBook
    form_class = ChequeBookForm
    template_name = 'accounting/cheque_book_form.html'
    success_url = reverse_lazy('accounting:cheque_book_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة دفتر شيكات بنكي جديد'
        context['cancel_url'] = reverse_lazy('accounting:cheque_book_list')
        return context

    def form_valid(self, form):
        messages.success(self.request, f"تمت إضافة دفتر الشيكات {form.instance.book_number} بنجاح.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Fixed Assets Views (واجهات الأصول الثابتة والإهلاك)
# ---------------------------------------------------------------------------

class FixedAssetListView(CustomPermissionRequiredMixin, ListView):
    """قائمة الأصول الثابتة مع بطاقات إحصائية وتصفية"""
    permission_required = 'accounting.view_fixedasset'
    model = FixedAsset
    template_name = 'accounting/fixed_asset_list.html'
    context_object_name = 'assets'
    paginate_by = 25

    def get_queryset(self):
        qs = FixedAsset.objects.all().select_related('branch', 'asset_account', 'accumulated_depreciation_account')
        q = self.request.GET.get('q')
        cat = self.request.GET.get('category')
        st = self.request.GET.get('status')
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(location__icontains=q))
        if cat:
            qs = qs.filter(category=cat)
        if st:
            qs = qs.filter(status=st)
        return qs.order_by('-purchase_date', '-id')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.db.models import Sum, Count
        from django.db.models.functions import Coalesce
        active_assets = FixedAsset.objects.exclude(status=FixedAsset.AssetStatus.DISPOSED)
        asset_agg = active_assets.aggregate(
            total_cost=Coalesce(Sum('purchase_cost'), Decimal('0.00')),
            opening_dep=Coalesce(Sum('opening_accumulated_depreciation'), Decimal('0.00')),
            active_count=Count('id', filter=Q(status=FixedAsset.AssetStatus.ACTIVE)),
        )
        total_acc_dep = AssetDepreciationLog.objects.filter(
            asset__in=active_assets
        ).aggregate(dep=Coalesce(Sum('amount'), Decimal('0.00')))['dep'] + asset_agg['opening_dep']

        total_cost = asset_agg['total_cost']
        total_book_value = total_cost - total_acc_dep

        context.update({
            'title': 'إدارة الأصول الثابتة والإهلاك',
            'categories': FixedAsset.AssetCategory.choices,
            'statuses': FixedAsset.AssetStatus.choices,
            'selected_category': self.request.GET.get('category', ''),
            'selected_status': self.request.GET.get('status', ''),
            'search_query': self.request.GET.get('q', ''),
            'total_cost': total_cost,
            'total_accumulated_depreciation': total_acc_dep,
            'total_net_book_value': total_book_value,
            'active_count': asset_agg['active_count'],
            'total_count': FixedAsset.objects.count(),
        })
        return context


class FixedAssetCreateView(CustomPermissionRequiredMixin, CreateView):
    """إضافة أصل ثابت جديد مع توليد قيد الشراء التلقائي"""
    permission_required = 'accounting.add_fixedasset'
    model = FixedAsset
    form_class = FixedAssetForm
    template_name = 'accounting/fixed_asset_form.html'
    success_url = reverse_lazy('accounting:fixed_asset_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة أصل ثابت جديد'
        context['cancel_url'] = reverse_lazy('accounting:fixed_asset_list')
        return context

    def form_valid(self, form):
        from apps.tenant.services.journal_service import _get_system_account
        from django.utils import timezone

        with transaction.atomic():
            asset = form.save(commit=False)
            pay_type = form.cleaned_data.get('payment_method_type')
            treasury = form.cleaned_data.get('treasury')
            bank_account = form.cleaned_data.get('bank_account')
            supplier = form.cleaned_data.get('supplier')

            asset.save()

            # إثبات قيد الشراء في حال الشراء الجديد
            if not asset.is_opening_balance and pay_type != 'OPENING':
                ref = _unique_reference(f"AST-PUR-{asset.purchase_date.year}-{asset.id:04d}")

                credit_acc = None
                desc_suffix = ""
                if pay_type == 'TREASURY' and treasury:
                    credit_acc = treasury.ensure_account()
                    desc_suffix = f"سداد نقدي من {treasury.name}"
                elif pay_type == 'BANK' and bank_account:
                    credit_acc = bank_account.ensure_account()
                    desc_suffix = f"سداد بنكي من {bank_account.name}"
                elif pay_type == 'SUPPLIER' and supplier:
                    credit_acc = supplier.account or _get_system_account('2110')
                    desc_suffix = f"شراء آجل من المورد {supplier.name}"

                if credit_acc:
                    entry = JournalEntry.objects.create(
                        date=asset.purchase_date,
                        reference=ref,
                        description=f"إثبات شراء أصل ثابت {asset.code} - {asset.name} ({desc_suffix})",
                        status=JournalEntry.POSTED
                    )
                    JournalItem.objects.create(
                        entry=entry, account=asset.asset_account, debit=asset.purchase_cost, credit=Decimal('0.00'),
                        description=f"تكلفة شراء أصل {asset.name}"
                    )
                    JournalItem.objects.create(
                        entry=entry, account=credit_acc, debit=Decimal('0.00'), credit=asset.purchase_cost,
                        description=f"سداد تكلفة أصل {asset.name}"
                    )
                    entry.post()
                    asset.purchase_entry = entry
                    asset.save(update_fields=['purchase_entry'])

            messages.success(self.request, f"تمت إضافة الأصل {asset.name} ({asset.code}) بنجاح.")
            return redirect('accounting:fixed_asset_detail', pk=asset.pk)


class FixedAssetUpdateView(CustomPermissionRequiredMixin, UpdateView):
    """تعديل بيانات الأصل"""
    permission_required = 'accounting.change_fixedasset'
    model = FixedAsset
    form_class = FixedAssetForm
    template_name = 'accounting/fixed_asset_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"تعديل بيانات الأصل: {self.object.name}"
        context['cancel_url'] = reverse_lazy('accounting:fixed_asset_detail', kwargs={'pk': self.object.pk})
        return context

    def form_valid(self, form):
        messages.success(self.request, f"تم تعديل بيانات الأصل {form.instance.name} بنجاح.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy('accounting:fixed_asset_detail', kwargs={'pk': self.object.pk})


class FixedAssetDetailView(CustomPermissionRequiredMixin, DetailView):
    """عرض تفاصيل الأصل، مجمع إهلاكه، وسجل أقساطه"""
    permission_required = 'accounting.view_fixedasset'
    model = FixedAsset
    template_name = 'accounting/fixed_asset_detail.html'
    context_object_name = 'asset'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"الأصل: {self.object.name}"
        context['disposal_form'] = FixedAssetDisposalForm()
        context['logs'] = self.object.depreciation_logs.all().select_related('entry')
        return context


class FixedAssetDisposalView(CustomPermissionRequiredMixin, View):
    """استبعاد أو بيع الأصل الثابت وتوليد قيد الإقفال والأرباح/الخسائر"""
    permission_required = 'accounting.change_fixedasset'

    def post(self, request, pk):
        from apps.tenant.services.journal_service import _get_system_account
        from django.utils import timezone

        asset = get_object_or_404(FixedAsset, pk=pk)
        if asset.status == FixedAsset.AssetStatus.DISPOSED:
            messages.error(request, 'هذا الأصل تم استبعاده دفترياً بالفعل مسبقاً.')
            return redirect('accounting:fixed_asset_detail', pk=asset.pk)

        form = FixedAssetDisposalForm(request.POST)
        if not form.is_valid():
            for error_list in form.errors.values():
                for error in error_list:
                    messages.error(request, error)
            return redirect('accounting:fixed_asset_detail', pk=asset.pk)

        disposal_date = form.cleaned_data['disposal_date']
        disposal_amount = form.cleaned_data['disposal_amount'] or Decimal('0.00')
        dest_type = form.cleaned_data['destination_type']
        treasury = form.cleaned_data.get('treasury')
        bank_account = form.cleaned_data.get('bank_account')
        notes = form.cleaned_data.get('disposal_notes') or ''

        with transaction.atomic():
            # Re-fetch under a row lock and re-check: two concurrent submits can't both dispose
            asset = FixedAsset.objects.select_for_update().get(pk=asset.pk)
            if asset.status == FixedAsset.AssetStatus.DISPOSED or asset.disposal_entry_id:
                messages.error(request, 'هذا الأصل تم استبعاده دفترياً بالفعل مسبقاً.')
                return redirect('accounting:fixed_asset_detail', pk=asset.pk)

            asset_acc = asset.asset_account
            acc_dep_acc = asset.accumulated_depreciation_account

            dest_acc = None
            if dest_type == 'TREASURY' and treasury:
                dest_acc = treasury.ensure_account()
            elif dest_type == 'BANK' and bank_account:
                dest_acc = bank_account.ensure_account()

            cost = asset.purchase_cost
            accum_dep = asset.total_accumulated_depreciation
            net_book_val = max(cost - accum_dep, Decimal('0.00'))

            # Net gain or loss
            gain_or_loss = disposal_amount - net_book_val

            # one disposal per asset -> the reference is unique by construction (no time-suffix hack)
            ref = f"AST-DISP-{disposal_date.year}-{asset.id:04d}"
            if JournalEntry.objects.filter(reference=ref).exists():
                messages.error(request, f'يوجد قيد استبعاد سابق بالمرجع {ref} لهذا الأصل. برجاء مراجعته قبل إعادة الاستبعاد.')
                return redirect('accounting:fixed_asset_detail', pk=asset.pk)

            entry = JournalEntry.objects.create(
                date=disposal_date,
                reference=ref,
                description=f"استبعاد / بيع أصل ثابت {asset.code} - {asset.name}. {notes}".strip(),
                status=JournalEntry.POSTED
            )

            # دائن: حساب الأصل (إقفال كامل التكلفة التاريخية)
            JournalItem.objects.create(
                entry=entry, account=asset_acc, debit=Decimal('0.00'), credit=cost,
                description=f"إقفال تكلفة الأصل {asset.name} عند الاستبعاد"
            )

            # مدين: مجمع الإهلاك (إقفال كامل المجمع المتراكم)
            if accum_dep > Decimal('0.00'):
                JournalItem.objects.create(
                    entry=entry, account=acc_dep_acc, debit=accum_dep, credit=Decimal('0.00'),
                    description=f"إقفال مجمع إهلاك الأصل {asset.name}"
                )

            # مدين: النقدية / البنك بالمبلغ المحصل
            if disposal_amount > Decimal('0.00') and dest_acc:
                JournalItem.objects.create(
                    entry=entry, account=dest_acc, debit=disposal_amount, credit=Decimal('0.00'),
                    description=f"متحصلات بيع الأصل {asset.name}"
                )

            # الفرق (أرباح أو خسائر بيع أصول ثابتة)
            if gain_or_loss > Decimal('0.00'):
                gain_acc = _get_system_account('4230')
                JournalItem.objects.create(
                    entry=entry, account=gain_acc, debit=Decimal('0.00'), credit=gain_or_loss,
                    description=f"أرباح رأسمالية من بيع الأصل {asset.name}"
                )
            elif gain_or_loss < Decimal('0.00'):
                loss_acc = _get_system_account('5370')
                loss_amt = abs(gain_or_loss)
                JournalItem.objects.create(
                    entry=entry, account=loss_acc, debit=loss_amt, credit=Decimal('0.00'),
                    description=f"خسائر رأسمالية من استبعاد الأصل {asset.name}"
                )

            entry.post()

            asset.status = FixedAsset.AssetStatus.DISPOSED
            asset.disposal_date = disposal_date
            asset.disposal_amount = disposal_amount
            asset.disposal_entry = entry
            asset.disposal_notes = notes
            asset.save()

            messages.success(request, f"تم استبعاد / بيع الأصل {asset.name} بنجاح وترحيل القيد المحاسبي {ref}.")
            return redirect('accounting:fixed_asset_detail', pk=asset.pk)


class FixedAssetDepreciationRunView(CustomPermissionRequiredMixin, View):
    """تشغيل الإهلاك الشهري بضغطة زر لجميع الأصول النشطة"""
    permission_required = 'accounting.change_fixedasset'
    template_name = 'accounting/fixed_asset_depreciation_run.html'

    def get(self, request):
        today = datetime.date.today()
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))

        form = FixedAssetDepreciationRunForm(initial={
            'year': year,
            'month': month,
            'run_date': today,
            'notes': f"إهلاك الأصول الثابتة عن شهر {month:02d} / {year}"
        })

        all_active_assets = FixedAsset.objects.filter(status=FixedAsset.AssetStatus.ACTIVE)
        already_run_asset_ids = set(
            AssetDepreciationLog.objects.filter(year=year, month=month).values_list('asset_id', flat=True)
        )

        preview_items = []
        total_run_depreciation = Decimal('0.00')

        for asset in all_active_assets:
            is_already_run = asset.id in already_run_asset_ids
            monthly_amount = asset.monthly_depreciation_amount
            remaining = max(asset.depreciable_base - asset.total_accumulated_depreciation, Decimal('0.00'))
            amount_to_depreciate = min(monthly_amount, remaining)

            can_run = not is_already_run and amount_to_depreciate > Decimal('0.00') and asset.purchase_date <= today

            if can_run:
                total_run_depreciation += amount_to_depreciate

            preview_items.append({
                'asset': asset,
                'monthly_amount': monthly_amount,
                'amount_to_depreciate': amount_to_depreciate,
                'is_already_run': is_already_run,
                'can_run': can_run,
                'book_value_after': max(asset.current_book_value - amount_to_depreciate, asset.salvage_value or Decimal('0.00')),
            })

        context = {
            'title': f'تشغيل الإهلاك الشهري ({year}/{month:02d})',
            'form': form,
            'year': year,
            'month': month,
            'preview_items': preview_items,
            'total_run_depreciation': total_run_depreciation,
            'eligible_count': sum(1 for it in preview_items if it['can_run']),
            'already_run_count': len(already_run_asset_ids),
        }
        return render(request, self.template_name, context)

    def post(self, request):
        from django.utils import timezone

        form = FixedAssetDepreciationRunForm(request.POST)
        if not form.is_valid():
            error_details = []
            for field_name, errors in form.errors.items():
                label = form.fields[field_name].label if field_name in form.fields else field_name
                for err in errors:
                    error_details.append(f"{label}: {err}")
            messages.error(request, 'تعذر احتساب الإهلاك بسبب: ' + ' | '.join(error_details))
            year = request.POST.get('year', datetime.date.today().year)
            month = request.POST.get('month', datetime.date.today().month)
            return redirect(f"{reverse_lazy('accounting:fixed_asset_depreciation_run')}?year={year}&month={month}")

        year = form.cleaned_data['year']
        month = int(form.cleaned_data['month'])
        run_date = form.cleaned_data['run_date']
        notes = form.cleaned_data['notes'] or f"إهلاك الأصول الثابتة عن شهر {month:02d}/{year}"

        all_active_assets = FixedAsset.objects.filter(status=FixedAsset.AssetStatus.ACTIVE)
        already_run_asset_ids = set(
            AssetDepreciationLog.objects.filter(year=year, month=month).values_list('asset_id', flat=True)
        )

        assets_to_depreciate = []
        for asset in all_active_assets:
            if asset.id in already_run_asset_ids:
                continue
            if asset.purchase_date > run_date:
                continue
            monthly_amount = asset.monthly_depreciation_amount
            remaining = max(asset.depreciable_base - asset.total_accumulated_depreciation, Decimal('0.00'))
            amount = min(monthly_amount, remaining)
            if amount > Decimal('0.00'):
                assets_to_depreciate.append((asset, amount))

        if not assets_to_depreciate:
            messages.warning(request, f"لا توجد أي أصول مستحقة للإهلاك عن شهر {month:02d}/{year}.")
            return redirect(f"{reverse_lazy('accounting:fixed_asset_depreciation_run')}?year={year}&month={month}")

        try:
            with transaction.atomic():
                # a 2nd run in the same month (only for assets not yet depreciated) gets -2, -3 ...
                ref = _unique_reference(f"AST-DEP-{year}-{month:02d}")

                entry = JournalEntry.objects.create(
                    date=run_date,
                    reference=ref,
                    description=f"{notes} - لعدد {len(assets_to_depreciate)} أصل",
                    status=JournalEntry.POSTED
                )

                debit_by_account = {}
                credit_by_account = {}

                for asset, amt in assets_to_depreciate:
                    exp_acc = asset.depreciation_expense_account
                    acc_dep_acc = asset.accumulated_depreciation_account

                    debit_by_account[exp_acc] = debit_by_account.get(exp_acc, Decimal('0.00')) + amt
                    credit_by_account[acc_dep_acc] = credit_by_account.get(acc_dep_acc, Decimal('0.00')) + amt

                    # computed BEFORE the log is created (total_accumulated_depreciation reads the logs)
                    accumulated_after = asset.total_accumulated_depreciation + amt
                    new_book_val = max(asset.current_book_value - amt, asset.salvage_value or Decimal('0.00'))
                    AssetDepreciationLog.objects.create(
                        asset=asset,
                        year=year,
                        month=month,
                        date=run_date,
                        amount=amt,
                        book_value_after=new_book_val,
                        entry=entry,
                        notes=notes,
                        created_by=request.user
                    )

                    if accumulated_after >= asset.depreciable_base:
                        asset.status = FixedAsset.AssetStatus.FULLY_DEPRECIATED
                        asset.save(update_fields=['status', 'updated_at'])

                for acc, total_debit in debit_by_account.items():
                    JournalItem.objects.create(
                        entry=entry, account=acc, debit=total_debit, credit=Decimal('0.00'),
                        description=f"إهلاك أصول شهر {month:02d}/{year}"
                    )
                for acc, total_credit in credit_by_account.items():
                    JournalItem.objects.create(
                        entry=entry, account=acc, debit=Decimal('0.00'), credit=total_credit,
                        description=f"مجمع إهلاك أصول شهر {month:02d}/{year}"
                    )

                entry.post()
        except IntegrityError:
            messages.error(request, f"تم احتساب إهلاك بعض هذه الأصول عن شهر {month:02d}/{year} بالتزامن من مستخدم آخر. برجاء مراجعة الشاشة وإعادة المحاولة.")
            return redirect(f"{reverse_lazy('accounting:fixed_asset_depreciation_run')}?year={year}&month={month}")

        messages.success(request, f"تم بنجاح احتساب وترحيل إهلاك {len(assets_to_depreciate)} أصل بالقيد {ref}.")
        return redirect('accounting:fixed_asset_list')


# ---------------------------------------------------------------------------
# Loans & Financing Views (واجهات القروض والتسهيلات وسداد الأقساط)
# ---------------------------------------------------------------------------

class LoanListView(CustomPermissionRequiredMixin, ListView):
    """قائمة القروض والتسهيلات مع بطاقات إحصائية وتصفية"""
    permission_required = 'accounting.view_loan'
    model = Loan
    template_name = 'accounting/loan_list.html'
    context_object_name = 'loans'
    paginate_by = 25

    def get_queryset(self):
        qs = Loan.objects.all().select_related('branch', 'loan_account')
        q = self.request.GET.get('q')
        lt = self.request.GET.get('loan_type')
        st = self.request.GET.get('status')
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(lender_name__icontains=q))
        if lt:
            qs = qs.filter(loan_type=lt)
        if st:
            qs = qs.filter(status=st)
        return qs.order_by('-start_date', '-id')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.db.models import Sum, Count, Q
        from django.db.models.functions import Coalesce
        from .models import LoanInstallment

        loans_agg = Loan.objects.aggregate(
            total_principal=Coalesce(Sum('principal_amount'), Decimal('0.00')),
            active_principal=Coalesce(Sum('principal_amount', filter=Q(status=Loan.LoanStatus.ACTIVE)), Decimal('0.00')),
            active_count=Count('id', filter=Q(status=Loan.LoanStatus.ACTIVE)),
        )
        total_principal = loans_agg['total_principal']

        # إجمالي المسدد وأصل الدين المتبقي في استعلام تجميعي واحد
        paid_agg = LoanInstallment.objects.filter(is_paid=True).aggregate(
            total_paid_principal=Coalesce(Sum('principal_amount'), Decimal('0.00')),
            total_paid_interest=Coalesce(Sum('interest_amount'), Decimal('0.00')),
            active_paid_principal=Coalesce(Sum('principal_amount', filter=Q(loan__status=Loan.LoanStatus.ACTIVE)), Decimal('0.00')),
        )
        total_paid = paid_agg['total_paid_principal'] + paid_agg['total_paid_interest']
        total_remaining_principal = max(loans_agg['active_principal'] - paid_agg['active_paid_principal'], Decimal('0.00'))

        # الأقساط المتأخرة
        today = timezone.now().date()
        total_overdue = LoanInstallment.objects.filter(
            loan__status=Loan.LoanStatus.ACTIVE,
            is_paid=False,
            due_date__lt=today
        ).count()

        context.update({
            'title': 'إدارة القروض والتسهيلات البنكية',
            'loan_types': Loan.LoanType.choices,
            'statuses': Loan.LoanStatus.choices,
            'selected_type': self.request.GET.get('loan_type', ''),
            'selected_status': self.request.GET.get('status', ''),
            'search_query': self.request.GET.get('q', ''),
            'total_principal': total_principal,
            'total_remaining_principal': total_remaining_principal,
            'total_paid': total_paid,
            'active_count': loans_agg['active_count'],
            'total_overdue': total_overdue,
        })
        return context


class LoanCreateView(CustomPermissionRequiredMixin, CreateView):
    """تسجيل قرض جديد مع قيد الاستلام وتوليد جدول الأقساط تلقائياً"""
    permission_required = 'accounting.add_loan'
    model = Loan
    form_class = LoanForm
    template_name = 'accounting/loan_form.html'
    success_url = reverse_lazy('accounting:loan_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تسجيل قرض / تمويل بنكي جديد'
        context['cancel_url'] = reverse_lazy('accounting:loan_list')
        return context

    def form_valid(self, form):
        from apps.tenant.services.journal_service import _get_system_account
        from django.utils import timezone

        with transaction.atomic():
            loan = form.save(commit=False)
            dest = form.cleaned_data.get('disbursement_destination')
            bank_acc = form.cleaned_data.get('bank_account')
            treasury = form.cleaned_data.get('treasury')
            bank_fee = form.cleaned_data.get('bank_fee') or Decimal('0.00')

            loan.save()

            # توليد قيد استلام وصرف مبلغ القرض في البنك أو الخزينة
            if dest != 'OPENING':
                ref = _unique_reference(f"LOAN-REC-{loan.start_date.year}-{loan.id:04d}")

                receiving_acc = None
                dest_name = ""
                if dest == 'BANK' and bank_acc:
                    receiving_acc = bank_acc.ensure_account()
                    dest_name = f"حساب {bank_acc.name}"
                elif dest == 'TREASURY' and treasury:
                    receiving_acc = treasury.ensure_account()
                    dest_name = f"خزينة {treasury.name}"

                if receiving_acc:
                    net_received = loan.principal_amount - bank_fee
                    entry = JournalEntry.objects.create(
                        date=loan.start_date,
                        reference=ref,
                        description=f"استلام أصل مبلغ القرض {loan.code} - {loan.name} من {loan.lender_name} في {dest_name}",
                        status=JournalEntry.POSTED
                    )

                    JournalItem.objects.create(
                        entry=entry, account=receiving_acc, debit=net_received, credit=Decimal('0.00'),
                        description=f"صافي استلام القرض {loan.name}"
                    )

                    if bank_fee > Decimal('0.00'):
                        fee_acc = Cheque.get_bank_expense_account()  # 5305 bank commissions
                        JournalItem.objects.create(
                            entry=entry, account=fee_acc, debit=bank_fee, credit=Decimal('0.00'),
                            description=f"مصاريف إدارية بنكية لقرض {loan.name}"
                        )

                    JournalItem.objects.create(
                        entry=entry, account=loan.loan_account, debit=Decimal('0.00'), credit=loan.principal_amount,
                        description=f"إثبات التزام قرض {loan.name} لصالح {loan.lender_name}"
                    )

                    entry.post()
                    loan.disbursement_entry = entry
                    loan.save(update_fields=['disbursement_entry'])

            # توليد جدول الأقساط كاملاً
            loan.generate_installments_schedule()

        messages.success(self.request, f"تم بنجاح تسجيل القرض {loan.name} وتوليد جدول {loan.installments_count} قسط.")
        return redirect('accounting:loan_detail', pk=loan.pk)


class LoanDetailView(CustomPermissionRequiredMixin, DetailView):
    """عرض تفاصيل القرض وجدول الأقساط الكامل وسداد الأقساط"""
    permission_required = 'accounting.view_loan'
    model = Loan
    template_name = 'accounting/loan_detail.html'
    context_object_name = 'loan'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"تفاصيل القرض: {self.object.name}"
        context['installments'] = self.object.installments.all().order_by('installment_number')
        context['pay_form'] = LoanInstallmentPayForm()
        return context


class LoanInstallmentPayView(CustomPermissionRequiredMixin, View):
    """سداد قسط القرض الفردي وتوليد قيد السداد المركب تلقائياً"""
    permission_required = 'accounting.change_loan'

    def post(self, request, pk):
        installment = get_object_or_404(LoanInstallment, pk=pk)
        loan = installment.loan

        if installment.is_paid:
            messages.warning(request, f"القسط #{installment.installment_number} مسدد بالفعل مسبقاً.")
            return redirect('accounting:loan_detail', pk=loan.pk)

        form = LoanInstallmentPayForm(request.POST)
        if not form.is_valid():
            for error_list in form.errors.values():
                for error in error_list:
                    messages.error(request, error)
            return redirect('accounting:loan_detail', pk=loan.pk)

        pay_date = form.cleaned_data['payment_date']
        dest = form.cleaned_data['payment_destination']
        bank_acc = form.cleaned_data.get('bank_account')
        treasury = form.cleaned_data.get('treasury')
        notes = form.cleaned_data.get('payment_notes') or ''

        with transaction.atomic():
            # Re-fetch under a row lock and re-check: a double submit can't pay the installment twice
            installment = LoanInstallment.objects.select_for_update().select_related('loan').get(pk=installment.pk)
            loan = installment.loan
            if installment.is_paid or installment.payment_entry_id:
                messages.warning(request, f"القسط #{installment.installment_number} مسدد بالفعل مسبقاً.")
                return redirect('accounting:loan_detail', pk=loan.pk)

            payment_account = None
            dest_name = ""
            if dest == 'BANK' and bank_acc:
                payment_account = bank_acc.ensure_account()
                dest_name = f"بنك {bank_acc.name}"
                installment.paid_bank = bank_acc
            elif dest == 'TREASURY' and treasury:
                payment_account = treasury.ensure_account()
                dest_name = f"خزينة {treasury.name}"
                installment.paid_treasury = treasury

            if payment_account is None:
                messages.error(request, 'برجاء تحديد البنك أو الخزينة المسدد منها القسط.')
                return redirect('accounting:loan_detail', pk=loan.pk)

            # one payment per installment -> unique by construction (no time-suffix hack)
            ref = f"LOAN-PAY-{pay_date.year}-{loan.id:04d}-{installment.installment_number:02d}"
            if JournalEntry.objects.filter(reference=ref).exists():
                messages.error(request, f'يوجد قيد سداد سابق بالمرجع {ref} لهذا القسط. برجاء مراجعته.')
                return redirect('accounting:loan_detail', pk=loan.pk)

            entry = JournalEntry.objects.create(
                date=pay_date,
                reference=ref,
                description=f"سداد قسط #{installment.installment_number} لقرض {loan.code} - {loan.name} من {dest_name}. {notes}".strip(),
                status=JournalEntry.POSTED
            )

            # 1. مدين: حساب التزام القرض (بأصل القسط - يخفض مديونية القرض)
            JournalItem.objects.create(
                entry=entry, account=loan.loan_account, debit=installment.principal_amount, credit=Decimal('0.00'),
                description=f"أصل قسط #{installment.installment_number} لقرض {loan.name}"
            )

            # 2. مدين: حساب مصروف الفوائد (بفائدة القسط - يظهر في قائمة الدخل)
            if installment.interest_amount > Decimal('0.00'):
                JournalItem.objects.create(
                    entry=entry, account=loan.interest_expense_account, debit=installment.interest_amount, credit=Decimal('0.00'),
                    description=f"فائدة قسط #{installment.installment_number} لقرض {loan.name}"
                )

            # 3. دائن: حساب البنك أو الخزينة (بإجمالي القسط المسدد)
            JournalItem.objects.create(
                entry=entry, account=payment_account, debit=Decimal('0.00'), credit=installment.total_amount,
                description=f"سداد قسط #{installment.installment_number} لقرض {loan.name}"
            )

            entry.post()

            installment.is_paid = True
            installment.payment_date = pay_date
            installment.payment_entry = entry
            installment.payment_notes = notes
            installment.save()

            all_paid = not loan.installments.filter(is_paid=False).exists()
            if all_paid:
                loan.status = Loan.LoanStatus.FULLY_PAID
                loan.save(update_fields=['status'])
                messages.success(request, f"تهانينا! تم سداد القسط #{installment.installment_number} واكتمل سداد القرض {loan.name} بالكامل.")
            else:
                messages.success(request, f"تم بنجاح سداد القسط #{installment.installment_number} بقيمة {installment.total_amount} ج.م بالقيد {ref}.")

        return redirect('accounting:loan_detail', pk=loan.pk)




