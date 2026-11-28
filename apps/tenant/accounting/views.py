from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.urls import reverse_lazy
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
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
from apps.tenant.core.money import money


# ---------------------------------------------------------------------------
# Branch scoping helpers (non-admin users only see / act on their own branch)
# ---------------------------------------------------------------------------

def _user_branch(user):
    emp = getattr(user, 'employee_profile', None)
    return getattr(emp, 'branch', None) if emp else None


def _branch_scoped(qs, user, make_q):
    """
    Reading lists: admins follow the header branch (all rows when the header is
    on "all branches"); other users only their own branch (none without one).
    """
    if is_admin_user(user):
        scope = getattr(user, 'branch_read_scope', None) if getattr(user, 'has_branch_context', False) else None
        return qs.filter(make_q(scope)).distinct() if scope is not None else qs
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
    """Treasuries usable for a money operation: the current branch's only."""
    from .forms import scoped_treasuries
    return qs.filter(pk__in=scoped_treasuries(user).values('pk'))


def scope_treasuries_for_admin(qs, user):
    """Treasuries shown on the treasury screens (list / edit / delete): the branch's, reps' boxes included."""
    from .forms import branch_treasuries
    return qs.filter(pk__in=branch_treasuries(user).values('pk'))


def scope_company_accounts(qs, request):
    """Company-wide accounts (banks/wallets): visible/editable only for unrestricted (admin) users."""
    from apps.tenant.core.access import is_branch_restricted
    return qs.none() if is_branch_restricted(request) else qs


TREASURY_BRANCH_ERROR = 'لا يمكن استخدام خزينة خارج الفرع الحالي. اختر خزينة من خزائن الفرع المفتوح.'


def _check_treasury_branch(user, treasuries=(), accounts=()):
    """Raise ValidationError if any treasury (or treasury-backed ledger account)
    is not in the current branch (core.access working branch)."""
    from .forms import scoped_treasuries
    allowed = scoped_treasuries(user)
    found = [t for t in treasuries if t is not None]
    for acc in accounts:
        if acc is not None:
            found.extend(Treasury.objects.filter(account=acc))
    for t in found:
        if not allowed.filter(pk=t.pk).exists():
            raise ValidationError(TREASURY_BRANCH_ERROR)


def _pos_defaults(request):
    """Per-branch POS payment defaults of the request's working branch
    (branch override, else the global SystemSetting default)."""
    from apps.tenant.core.access import get_working_branch
    from apps.tenant.core.settings_helpers import get_pos_defaults
    return get_pos_defaults(get_working_branch(request))


def _default_cash_treasury(request, defaults=None):
    """Default cash treasury for a voucher: the working branch's default (per-branch,
    else global) only if it is usable in the current branch, else the branch's only
    treasury, else None (user must pick)."""
    allowed = scope_treasuries(Treasury.objects.all(), request.user)
    default = (defaults if defaults is not None else _pos_defaults(request))['treasury']
    if default is not None and allowed.filter(pk=default.pk).exists():
        return default
    return allowed.first() if allowed.count() == 1 else None


DRAFT_SAVED_MESSAGE = 'تم الحفظ كمسودة — اعتمده من صفحة التفاصيل.'


def _auto_post_enabled():
    """SystemSetting.auto_post_journals: confirm/post vouchers and expenses right after saving."""
    from apps.tenant.core.settings_helpers import setting
    return bool(setting('auto_post_journals', True))


def _lock_context():
    """{'lock_date': date|None} for templates showing the closed-period notice."""
    from apps.tenant.core.settings_helpers import accounting_lock_date
    return {'lock_date': accounting_lock_date()}


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
        from django.db.models import DecimalField, Sum, Value
        from django.db.models.functions import Coalesce
        money_field = DecimalField(max_digits=20, decimal_places=2)
        queryset = (JournalEntry.objects.select_related('reversal_of', 'reversed_by')
                    .annotate(total_debit=Coalesce(Sum('items__debit'), Value(Decimal('0')), output_field=money_field),
                              total_credit=Coalesce(Sum('items__credit'), Value(Decimal('0')), output_field=money_field))
                    .order_by('-date', '-created_at'))
        
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
        context.update(_lock_context())
        return context

class JournalEntryDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_journalentry'

    model = JournalEntry
    template_name = 'accounting/journal_detail.html'
    context_object_name = 'entry'

    def get_queryset(self):
        return (JournalEntry.objects.select_related('reversal_of', 'reversed_by', 'created_by')
                .prefetch_related('items__account'))

    def dispatch(self, request, *args, **kwargs):
        if not is_admin_user(request.user):
            messages.error(request, 'عفواً، الدخول إلى تفاصيل القيد المحاسبي مقتصر على الأدمن والمديرين فقط.')
            return redirect('core:main_screen')
        return super().dispatch(request, *args, **kwargs)

    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f'تفاصيل القيد {self.object.reference}'
        context.update(_lock_context())
        from apps.tenant.core.settings_helpers import is_date_locked
        context['entry_locked'] = is_date_locked(self.object.date)
        e = self.object
        context['can_reverse'] = (e.status == JournalEntry.POSTED and not e.content_type_id
                                  and not e.reversal_of_id and e.reversing_entry is None)
        return context


@login_required
@require_POST
def journal_reverse(request, pk):
    """Manual entries only: a document's entry is reversed by cancelling the document itself."""
    from django.utils.dateparse import parse_date
    from apps.tenant.services.journal_service import reverse_journal_entry
    if not is_admin_user(request.user):
        messages.error(request, 'عفواً، عكس القيود للأدمن والمديرين بس.')
        return redirect('core:main_screen')
    entry = get_object_or_404(JournalEntry, pk=pk)
    reason = (request.POST.get('reason') or '').strip()
    raw = request.POST.get('date') or ''
    date = parse_date(raw) if raw else None
    try:
        if entry.content_type_id:
            raise ValidationError('القيد ده تبع مستند — اتلغي من المستند نفسه (إلغاء الفاتورة/السند) مش من هنا.')
        if len(reason) < 3:
            raise ValidationError('اكتب سبب عكس القيد.')
        if raw and date is None:
            raise ValidationError('تاريخ غير صالح.')
        with transaction.atomic():
            rev = reverse_journal_entry(entry, reason=reason, user=request.user, date=date)
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        return redirect('accounting:journal_detail', pk=pk)
    messages.success(request, f'اتعمل القيد العكسي {rev.reference}.')
    return redirect('accounting:journal_detail', pk=rev.pk)

# Treasury Views
class TreasuryListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_treasury'

    model = Treasury
    queryset = Treasury.objects.select_related('branch', 'account')
    template_name = 'accounting/treasury_list.html'
    context_object_name = 'treasuries'

    def get_queryset(self):
        return scope_treasuries_for_admin(super().get_queryset(), self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'الخزائن'
        return context

class LedgerSaveErrorsMixin:
    """A ledger error while saving (opening-balance entry refused, missing chart account…) is shown on the form
    — never a server error page. The model save is atomic, so nothing half-saved stays."""

    def form_valid(self, form):
        from django.core.exceptions import ValidationError as _VE
        try:
            return super().form_valid(form)
        except (_VE, ValueError) as e:
            form.add_error(None, ' '.join(getattr(e, 'messages', None) or [str(e)]))
            self.object = None
            return self.form_invalid(form)


class TreasuryCreateView(LedgerSaveErrorsMixin, CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_treasury'

    model = Treasury
    form_class = TreasuryForm
    template_name = 'accounting/treasury_form.html'
    success_url = reverse_lazy('accounting:liquidity_dashboard')
    
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
    success_url = reverse_lazy('accounting:liquidity_dashboard')

    def get_queryset(self):
        return scope_treasuries_for_admin(super().get_queryset(), self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل خزينة'
        context['cancel_url'] = self.success_url
        return context

class TreasuryDeleteView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.delete_treasury'

    def post(self, request, pk):
        treasury = get_object_or_404(scope_treasuries_for_admin(Treasury.objects.all(), request.user), pk=pk)
        
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
                messages.error(request, f'حدث خطأ أثناء الحذف: {_error_text(e)}')
                
        return redirect('accounting:liquidity_dashboard')

# BankAccount Views
class BankAccountListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_bankaccount'

    model = BankAccount
    queryset = BankAccount.objects.select_related('account')
    template_name = 'accounting/bank_list.html'
    context_object_name = 'banks'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'حسابات البنوك'
        return context

class BankAccountCreateView(LedgerSaveErrorsMixin, CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_bankaccount'

    model = BankAccount
    form_class = BankAccountForm
    template_name = 'accounting/bank_form.html'
    success_url = reverse_lazy('accounting:liquidity_dashboard')
    
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
    success_url = reverse_lazy('accounting:liquidity_dashboard')

    def get_queryset(self):
        return scope_company_accounts(super().get_queryset(), self.request)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل حساب بنكي'
        context['cancel_url'] = self.success_url
        return context

class BankAccountDeleteView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.delete_bankaccount'

    def post(self, request, pk):
        bank_account = get_object_or_404(scope_company_accounts(BankAccount.objects.all(), request), pk=pk)
        
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
                messages.error(request, f'حدث خطأ أثناء الحذف: {_error_text(e)}')
                
        return redirect('accounting:liquidity_dashboard')

# EWallet Views
class EWalletListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_ewallet'

    model = EWallet
    queryset = EWallet.objects.select_related('account')
    template_name = 'accounting/ewallet_list.html'
    context_object_name = 'ewallets'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'المحافظ الإلكترونية'
        return context

class EWalletCreateView(LedgerSaveErrorsMixin, CustomPermissionRequiredMixin, CreateView):
    permission_required = 'accounting.add_ewallet'

    model = EWallet
    form_class = EWalletForm
    template_name = 'accounting/ewallet_form.html'
    success_url = reverse_lazy('accounting:liquidity_dashboard')
    
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
    success_url = reverse_lazy('accounting:liquidity_dashboard')

    def get_queryset(self):
        return scope_company_accounts(super().get_queryset(), self.request)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل محفظة إلكترونية'
        context['cancel_url'] = self.success_url
        return context

class EWalletDeleteView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.delete_ewallet'

    def post(self, request, pk):
        ewallet = get_object_or_404(scope_company_accounts(EWallet.objects.all(), request), pk=pk)
        
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
                messages.error(request, f'حدث خطأ أثناء الحذف: {_error_text(e)}')
                
        return redirect('accounting:liquidity_dashboard')

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
    from apps.tenant.services.journal_service import batch_get_account_balances
    # every balance in a fixed number of queries (not 2 aggregates per account in the tree)
    accounts = list(Account.objects.order_by('tree_id', 'lft'))
    batch_get_account_balances(account_qs=accounts)
    has_accounts = bool(accounts)
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
        messages.error(request, f'حدث خطأ أثناء الإنشاء: {_error_text(e)}')
        
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
        net = money(acc.own_debit - acc.own_credit)
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
        qs = scope_expenses(super().get_queryset().select_related('branch', 'expense_account'), self.request.user)
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

            # SystemSetting.auto_post_journals: approve + post right away, else keep the DRAFT
            if not _auto_post_enabled():
                messages.success(self.request, DRAFT_SAVED_MESSAGE)
                return redirect('accounting:expense_detail', pk=self.object.pk)
            try:
                _check_treasury_branch(self.request.user, accounts=[self.object.payment_account])
                self.object.confirm_expense()
                messages.success(self.request, 'تم حفظ واعتماد المصروف وترحيل القيد المحاسبي بنجاح.')
            except Exception as e:
                self.object.refresh_from_db()
                messages.warning(self.request, f'تم حفظ المصروف كمسودة ولكن تعذر الترحيل التلقائي: {_error_text(e)}')
            return redirect('accounting:expense_detail', pk=self.object.pk)
        else:
            return self.render_to_response(self.get_context_data(form=form))

class ExpenseConfirmView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_expense'

    def post(self, request, pk):
        expense = get_object_or_404(scope_expenses(Expense.objects.all(), request.user), pk=pk)
        if expense.status == Expense.DRAFT:
            try:
                _check_treasury_branch(request.user, accounts=[expense.payment_account])
                expense.confirm_expense()
                messages.success(request, 'تم اعتماد المصروف وتوليد القيد المحاسبي بنجاح.')
            except Exception as e:
                messages.error(request, f'خطأ أثناء اعتماد المصروف: {_error_text(e)}')
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
        return scope_vouchers(Voucher.objects.select_related('partner'), self.request.user).filter(voucher_type=Voucher.RECEIPT).order_by('-date', '-created_at')

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
        return scope_vouchers(Voucher.objects.select_related('partner'), self.request.user).filter(voucher_type=Voucher.PAYMENT).order_by('-date', '-created_at')

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

    def get_initial(self):
        """?partner=<id> pre-selects the partner (e.g. "collect" from the partners dashboard)."""
        initial = super().get_initial()
        partner_id = self.request.GET.get('partner', '')
        if partner_id.isdigit():
            initial['partner'] = int(partner_id)
        return initial

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
        from apps.tenant.core.idempotency import new_form_key
        context['idempotency_key'] = new_form_key(self.request)      # double-click on save = one voucher
        return context

    def form_valid(self, form):
        form.instance.voucher_type = self.get_voucher_type()
        from apps.tenant.invoicing.models import POSSession
        defaults = _pos_defaults(self.request)

        open_session = POSSession.objects.filter(user=self.request.user, status=POSSession.OPEN).first()
        if open_session:
            form.instance.pos_session = open_session
            if not form.instance.treasury and open_session.treasury and form.instance.payment_method == Voucher.CASH:
                form.instance.treasury = open_session.treasury

        if not form.instance.treasury and form.instance.payment_method == Voucher.CASH:
            if _default_cash_treasury(self.request, defaults):
                form.instance.treasury = _default_cash_treasury(self.request, defaults)

        if not form.instance.bank_account and form.instance.payment_method == Voucher.CARD:
            if defaults['bank_account']:
                form.instance.bank_account = defaults['bank_account']
            elif BankAccount.objects.count() == 1:
                form.instance.bank_account = BankAccount.objects.first()

        if form.instance.payment_method == Voucher.BANK_TRANSFER:
            if not form.instance.bank_account and form.instance.transfer_bank_account:
                form.instance.bank_account = form.instance.transfer_bank_account
            elif not form.instance.transfer_bank_account and form.instance.bank_account:
                form.instance.transfer_bank_account = form.instance.bank_account

            if not form.instance.bank_account:
                if defaults['instapay_account']:
                    form.instance.bank_account = defaults['instapay_account']
                    form.instance.transfer_bank_account = defaults['instapay_account']
                elif defaults['bank_account']:
                    form.instance.bank_account = defaults['bank_account']
                    form.instance.transfer_bank_account = defaults['bank_account']
                elif BankAccount.objects.count() == 1:
                    first_bank = BankAccount.objects.first()
                    form.instance.bank_account = first_bank
                    form.instance.transfer_bank_account = first_bank

        if not form.instance.ewallet and form.instance.payment_method == Voucher.EWALLET:
            if defaults['ewallet']:
                form.instance.ewallet = defaults['ewallet']
            elif EWallet.objects.count() == 1:
                form.instance.ewallet = EWallet.objects.first()

        if form.instance.payment_method == Voucher.SPLIT:
            if form.instance.paid_cash > 0 and not form.instance.treasury:
                if open_session and open_session.treasury:
                    form.instance.treasury = open_session.treasury
                elif _default_cash_treasury(self.request, defaults):
                    form.instance.treasury = _default_cash_treasury(self.request, defaults)

            if form.instance.paid_card > 0 and not form.instance.bank_account:
                if defaults['bank_account']:
                    form.instance.bank_account = defaults['bank_account']
                elif BankAccount.objects.count() == 1:
                    form.instance.bank_account = BankAccount.objects.first()

            if form.instance.paid_ewallet > 0 and not form.instance.ewallet:
                if defaults['ewallet']:
                    form.instance.ewallet = defaults['ewallet']
                elif EWallet.objects.count() == 1:
                    form.instance.ewallet = EWallet.objects.first()

            if form.instance.paid_bank > 0 and not form.instance.transfer_bank_account:
                if defaults['instapay_account']:
                    form.instance.transfer_bank_account = defaults['instapay_account']
                elif defaults['bank_account']:
                    form.instance.transfer_bank_account = defaults['bank_account']
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

        from apps.tenant.core.idempotency import find_by_idempotency_key, parse_idempotency_key
        idem_key = parse_idempotency_key(
            self.request.POST.get('idempotency_key') or self.request.headers.get('X-Idempotency-Key')
        )
        existing = find_by_idempotency_key(Voucher, idem_key, created_by=self.request.user)
        if existing is not None:
            self.object = existing
            return redirect(self.get_success_url())
        form.instance.idempotency_key = idem_key
        form.instance.created_by = self.request.user
        try:
            with transaction.atomic():
                response = super().form_valid(form)
        except IntegrityError:
            existing = find_by_idempotency_key(Voucher, idem_key, created_by=self.request.user)
            if existing is not None:
                self.object = existing
                return redirect(self.get_success_url())
            form.add_error(None, 'حصل تعارض في الحفظ — حدّث الصفحة وجرّب تاني.')
            return self.form_invalid(form)

        # SystemSetting.auto_post_journals: confirm and post right away (so it immediately
        # reflects in the shift), or leave a DRAFT to be approved from the detail page.
        if not _auto_post_enabled():
            messages.success(self.request, DRAFT_SAVED_MESSAGE)
            return response
        try:
            if self.object.status == Voucher.DRAFT and self.object.payment_account:
                self.object.confirm_voucher()
                messages.success(self.request, f'تم حفظ وتأكيد وترحيل {self.object.get_voucher_type_display()} بنجاح.')
            else:
                messages.success(self.request, 'تم حفظ مسودة السند بنجاح.')
        except Exception as e:
            messages.warning(self.request, f'تم حفظ السند كمسودة ولكن تعذر الترحيل التلقائي: {_error_text(e)}')
        self._save_invoice_allocations()
        return response

    def _save_invoice_allocations(self):
        """«ربط بالفواتير» rows of the form (alloc_<invoice id> = amount); none = oldest first."""
        from django.core.exceptions import ValidationError as DjangoValidationError
        from .allocation_services import save_allocations
        amounts = {}
        for key, value in self.request.POST.items():
            if key.startswith('alloc_') and key[6:].isdigit() and (value or '').strip():
                try:
                    amounts[key[6:]] = Decimal(value)
                except (InvalidOperation, ValueError):
                    pass
        if not amounts:
            return
        try:
            save_allocations(self.object, amounts)
        except DjangoValidationError as e:
            messages.warning(self.request, 'اتحفظ السند بس الربط بالفواتير ماتسجلش: ' + ' '.join(e.messages)
                             + ' — المبلغ هيتوزع على الفواتير الأقدم تلقائياً.')

    def get_success_url(self):
        return reverse_lazy('accounting:voucher_detail', kwargs={'pk': self.object.pk})

class VoucherDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_voucher'

    model = Voucher
    template_name = 'accounting/voucher_detail.html'
    context_object_name = 'voucher'

    def get_queryset(self):
        return scope_vouchers(Voucher.objects.prefetch_related('allocations__invoice'), self.request.user)

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
                defaults = _pos_defaults(request)

                # If cash voucher is missing a treasury, attempt auto-assign fallback
                if voucher.payment_method == Voucher.CASH and not voucher.treasury:
                    from apps.tenant.invoicing.models import POSSession
                    open_session = POSSession.objects.filter(user=request.user, status=POSSession.OPEN).first()
                    if open_session and open_session.treasury:
                        voucher.treasury = open_session.treasury
                        voucher.save(update_fields=['treasury'])
                    elif _default_cash_treasury(request, defaults):
                        voucher.treasury = _default_cash_treasury(request, defaults)
                        voucher.save(update_fields=['treasury'])

                if voucher.payment_method == Voucher.CARD and not voucher.bank_account:
                    if defaults['bank_account']:
                        voucher.bank_account = defaults['bank_account']
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
                        if defaults['instapay_account']:
                            voucher.bank_account = defaults['instapay_account']
                            voucher.transfer_bank_account = defaults['instapay_account']
                            voucher.save(update_fields=['bank_account', 'transfer_bank_account'])
                        elif defaults['bank_account']:
                            voucher.bank_account = defaults['bank_account']
                            voucher.transfer_bank_account = defaults['bank_account']
                            voucher.save(update_fields=['bank_account', 'transfer_bank_account'])
                        elif BankAccount.objects.count() == 1:
                            first_bank = BankAccount.objects.first()
                            voucher.bank_account = first_bank
                            voucher.transfer_bank_account = first_bank
                            voucher.save(update_fields=['bank_account', 'transfer_bank_account'])

                if voucher.payment_method == Voucher.EWALLET and not voucher.ewallet:
                    if defaults['ewallet']:
                        voucher.ewallet = defaults['ewallet']
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
                        elif _default_cash_treasury(request, defaults):
                            voucher.treasury = _default_cash_treasury(request, defaults)
                            updated_fields.append('treasury')

                    if voucher.paid_card > 0 and not voucher.bank_account:
                        if defaults['bank_account']:
                            voucher.bank_account = defaults['bank_account']
                            updated_fields.append('bank_account')
                        elif BankAccount.objects.count() == 1:
                            voucher.bank_account = BankAccount.objects.first()
                            updated_fields.append('bank_account')

                    if voucher.paid_ewallet > 0 and not voucher.ewallet:
                        if defaults['ewallet']:
                            voucher.ewallet = defaults['ewallet']
                            updated_fields.append('ewallet')
                        elif EWallet.objects.count() == 1:
                            voucher.ewallet = EWallet.objects.first()
                            updated_fields.append('ewallet')

                    if voucher.paid_bank > 0 and not voucher.transfer_bank_account:
                        if defaults['instapay_account']:
                            voucher.transfer_bank_account = defaults['instapay_account']
                            updated_fields.append('transfer_bank_account')
                        elif defaults['bank_account']:
                            voucher.transfer_bank_account = defaults['bank_account']
                            updated_fields.append('transfer_bank_account')
                        elif BankAccount.objects.count() == 1:
                            voucher.transfer_bank_account = BankAccount.objects.first()
                            updated_fields.append('transfer_bank_account')

                    if updated_fields:
                        voucher.save(update_fields=updated_fields)

                if not voucher.payment_account:
                    raise ValidationError("برجاء الضغط على 'تعديل السند' وتحديد خزينة أو وسيلة السداد أولاً.")

                _check_treasury_branch(request.user, treasuries=[voucher.treasury])
                voucher.confirm_voucher()
                messages.success(request, 'تم اعتماد السند وتوليد القيد بنجاح.')
            except Exception as e:
                messages.error(request, f'حدث خطأ أثناء اعتماد السند: {_error_text(e)}')
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
                locked = Voucher.objects.select_for_update(of=('self',)).select_related('pos_session', 'journal_entry').get(pk=self.object.pk)
                if locked.status == Voucher.CANCELLED:
                    raise ValidationError('لا يمكن تعديل سند ملغي.')
                was_posted = (locked.status == Voucher.POSTED)
                if was_posted:
                    # a posted voucher of a closed accounting period can't be edited (only cancelled
                    # by a reversal dated today)
                    from .models import ensure_open_period
                    ensure_open_period(locked.date)
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
            messages.error(request, f'حدث خطأ أثناء إلغاء السند: {_error_text(e)}')
        return redirect('accounting:voucher_detail', pk=voucher.pk)


# ===========================================================================
# POS Machines (ماكينات الدفع الإلكتروني)
# ===========================================================================

def _scoped_machines(request):
    """Machines the user may see: own branch (non-admins) / header branch (admins, all = everything)."""
    from apps.tenant.core.access import filter_by_branch
    from .models import POSMachine
    return filter_by_branch(POSMachine.objects.select_related('branch', 'account'), request, 'branch')


def _scoped_eservice_transactions(request):
    from apps.tenant.core.access import filter_by_branch
    from .models import EServiceTransaction
    return filter_by_branch(EServiceTransaction.objects.all(), request, 'treasury__branch')


def _query_without_page(request):
    params = request.GET.copy()
    params.pop('page', None)
    return params.urlencode()


def _parse_amount(raw, default=None):
    """'' -> default; otherwise a finite Decimal (raises InvalidOperation)."""
    raw = (raw or '').strip()
    if raw == '':
        return default
    value = Decimal(raw)
    if not value.is_finite():
        raise InvalidOperation
    return value


def _pos_machine_form_post(request, machine, branches):
    """Validates the machine form; returns (fields dict, error)."""
    from .models import POSMachine
    name = request.POST.get('name', '').strip()
    machine_type = request.POST.get('machine_type', POSMachine.OTHER)
    branch_id = request.POST.get('branch_id')
    if not name or not branch_id:
        return None, 'برجاء إدخال اسم الماكينة والفرع.'
    if machine_type not in {c for c, _ in POSMachine.MACHINE_TYPE_CHOICES}:
        return None, 'نوع الماكينة غير صحيح.'
    branch = branches.filter(id=branch_id).first() if str(branch_id).isdigit() else None
    if branch is None:
        return None, 'الفرع المحدد غير متاح.'
    try:
        min_alert = _parse_amount(request.POST.get('min_balance_alert'), Decimal('0'))
        opening = _parse_amount(request.POST.get('opening_balance'), Decimal('0'))
    except (InvalidOperation, ValueError):
        return None, 'برجاء إدخال مبالغ رقمية صحيحة.'
    if min_alert < 0 or opening < 0:
        return None, 'المبالغ لا يمكن أن تكون سالبة.'
    fields = {
        'name': name, 'machine_type': machine_type, 'branch': branch,
        'min_balance_alert': min_alert, 'notes': request.POST.get('notes', ''),
    }
    if machine is None:
        fields['opening_balance'] = opening
    else:
        fields['is_active'] = request.POST.get('is_active') == 'on'
    return fields, None


@login_required
@custom_permission_required('accounting.view_posmachine', redirect_url='core:main_screen')
def pos_machine_list(request):
    """POS machines of the user's branch scope with current balances."""
    from apps.tenant.services.journal_service import with_balances
    machines = with_balances(_scoped_machines(request))
    context = {
        'machines': machines,
        'low_balance_count': sum(1 for m in machines if m.is_active and m.is_low_balance),
        'title': 'ماكينات الدفع الإلكتروني',
    }
    return render(request, 'accounting/pos_machines/machine_list.html', context)


@login_required
@custom_permission_required('accounting.add_posmachine', redirect_url='core:main_screen')
def pos_machine_create(request):
    """Create a new POS machine (its ledger account under 1170 is created automatically)."""
    from .models import POSMachine
    from apps.tenant.core.access import allowed_branches, get_working_branch
    branches = allowed_branches(request)
    if request.method == 'POST':
        fields, error = _pos_machine_form_post(request, None, branches)
        if error:
            messages.error(request, error)
        else:
            try:
                machine = POSMachine.objects.create(**fields)
                messages.success(request, f'تم إنشاء الماكينة "{machine.name}" بنجاح وتم إنشاء حسابها المحاسبي تلقائياً.')
                return redirect('accounting:pos_machine_list')
            except Exception as e:
                messages.error(request, f'حدث خطأ: {_error_text(e)}')
    wb = get_working_branch(request)
    context = {
        'branches': branches,
        'machine_types': POSMachine.MACHINE_TYPE_CHOICES,
        'selected_branch_id': wb.id if wb else None,
        'title': 'إضافة ماكينة دفع جديدة',
    }
    return render(request, 'accounting/pos_machines/machine_form.html', context)


@login_required
@custom_permission_required('accounting.change_posmachine', redirect_url='core:main_screen')
def pos_machine_update(request, pk):
    """Edit a machine (name, type, branch, active, low-balance alert). The opening balance is fixed."""
    from .models import POSMachine
    from apps.tenant.core.access import allowed_branches
    machine = get_object_or_404(_scoped_machines(request), pk=pk)
    branches = allowed_branches(request)
    if request.method == 'POST':
        fields, error = _pos_machine_form_post(request, machine, branches)
        if error:
            messages.error(request, error)
        else:
            for k, v in fields.items():
                setattr(machine, k, v)
            machine.save()
            if machine.account_id and machine.account.name != machine.name:
                machine.account.name = machine.name
                machine.account.save(update_fields=['name'])
            messages.success(request, f'تم حفظ بيانات الماكينة "{machine.name}".')
            return redirect('accounting:pos_machine_detail', pk=machine.pk)
    context = {
        'machine': machine,
        'branches': branches,
        'machine_types': POSMachine.MACHINE_TYPE_CHOICES,
        'selected_branch_id': machine.branch_id,
        'title': f'تعديل الماكينة: {machine.name}',
    }
    return render(request, 'accounting/pos_machines/machine_form.html', context)


@login_required
@custom_permission_required('accounting.view_posmachine', redirect_url='core:main_screen')
def pos_machine_detail(request, pk):
    """Machine details with its latest operations."""
    machine = get_object_or_404(_scoped_machines(request), pk=pk)
    transactions = machine.transactions.select_related('treasury', 'agent').order_by('-date', '-created_at')[:50]
    context = {
        'machine': machine,
        'transactions': transactions,
        'rules': machine.commission_rules.all(),
        'title': f'تفاصيل: {machine.name}',
    }
    return render(request, 'accounting/pos_machines/machine_detail.html', context)


# ===========================================================================
# E-Service Center (مركز خدمات الدفع)
# ===========================================================================

def _open_shift(user):
    """The user's OPEN cashier shift (invoicing.POSSession), or None."""
    from apps.tenant.invoicing.models import POSSession
    return (POSSession.objects.filter(user=user, status=POSSession.OPEN)
            .select_related('treasury', 'branch').order_by('-start_time').first())


def _can_override_commission(user):
    return is_admin_user(user) or user.has_perm('accounting.override_eservice_commission')


def _agents_qs():
    from apps.tenant.partners.models import Partner
    return Partner.objects.filter(is_active=True, partner_type__in=(Partner.SUPPLIER, Partner.BOTH)).order_by('name')


@login_required
@custom_permission_required('accounting.view_eservicetransaction', redirect_url='core:main_screen')
def eservice_center(request):
    """Cash-in / cash-out for customers and machine recharge from an agent."""
    from .models import POSMachine, EWallet, Treasury, EServiceTransaction
    from apps.tenant.core.access import get_working_branch

    # Money operations use the CURRENT working branch only (header / open shift / own branch).
    working_branch = get_working_branch(request)
    from apps.tenant.services.journal_service import with_balances
    machines = POSMachine.objects.filter(is_active=True).select_related('branch', 'account')
    machines = machines.filter(branch=working_branch) if working_branch else machines.none()
    ewallets = EWallet.objects.all()
    treasuries = scope_treasuries(Treasury.objects.all(), request.user)
    shift = _open_shift(request.user)
    if shift is not None:
        # While a shift is open, cash moves through the shift's drawer only.
        treasuries = treasuries.filter(pk=shift.treasury_id)
    agents = _agents_qs()

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
        agent_id = request.POST.get('agent_id') or None
        paid_on_credit = request.POST.get('paid_on_credit') in ('1', 'on', 'true')
        description = request.POST.get('description', '')

        error = None
        principal = commission = None
        commission_key = 'commission_expense' if transaction_type == EServiceTransaction.MERCHANT_RECHARGE else 'commission_revenue'
        try:
            principal = _parse_amount(request.POST.get('principal_amount'), Decimal('0'))
            commission = _parse_amount(request.POST.get(commission_key), None)
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
            elif shift is not None and str(treasury_id) != str(shift.treasury_id):
                error = (f'لديك وردية مفتوحة على خزينة "{shift.treasury}". '
                         f'حركات النقدية تتم على خزينة الوردية فقط حتى إغلاقها.')
            elif principal <= 0:
                error = 'برجاء إدخال مبلغ صحيح.'
            elif commission is not None and commission < 0:
                error = 'العمولات لا يمكن أن تكون سالبة.'
            elif commission is not None and commission > principal:
                error = 'العمولة لا يمكن أن تتجاوز المبلغ الأصلي.'
            elif source_type == 'MACHINE' and not pos_machine_id:
                error = 'برجاء اختيار الماكينة.'
            elif source_type == 'EWALLET' and not ewallet_id:
                error = 'برجاء اختيار المحفظة الإلكترونية.'
            elif transaction_type == EServiceTransaction.MERCHANT_RECHARGE and paid_on_credit and not agent_id:
                error = 'الشحن الآجل يتطلب اختيار المندوب / المورد.'

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
                        description=description,
                        created_by=request.user,
                        pos_session=shift,
                    )
                    if source_type == 'MACHINE':
                        txn.pos_machine = machines.get(id=pos_machine_id)
                    else:
                        txn.ewallet = EWallet.objects.get(id=ewallet_id)
                    if transaction_type == EServiceTransaction.MERCHANT_RECHARGE:
                        if agent_id:
                            txn.agent = agents.get(id=agent_id)
                        txn.paid_on_credit = paid_on_credit
                    txn.apply_commission_rule(commission, can_override=_can_override_commission(request.user))
                    if txn.commission_amount > principal:
                        raise ValidationError('العمولة لا يمكن أن تتجاوز المبلغ الأصلي.')
                    txn.save()
                    txn.post_transaction(user=request.user)
                messages.success(request, f'تم تنفيذ العملية {txn.transaction_number} بنجاح وتم إنشاء القيد المحاسبي.')
                return redirect('accounting:eservice_center')
            except (Treasury.DoesNotExist, POSMachine.DoesNotExist, EWallet.DoesNotExist, ValueError):
                messages.error(request, 'الخزينة أو الماكينة أو المحفظة المحددة غير متاحة.')
            except ValidationError as e:
                messages.error(request, _error_text(e))
            except Exception as e:
                from apps.tenant.partners.models import Partner
                if isinstance(e, Partner.DoesNotExist):
                    messages.error(request, 'المندوب المحدد غير متاح أو ليس مورداً.')
                else:
                    messages.error(request, f'حدث خطأ: {_error_text(e)}')

    context = {
        # balances of all machines in one batch (the template shows them per machine)
        'machines': with_balances(machines),
        'ewallets': ewallets,
        'treasuries': treasuries,
        'agents': agents,
        'shift': shift,
        'default_treasury_id': shift.treasury_id if shift else (treasuries[0].id if len(treasuries) == 1 else None),
        'working_branch': working_branch,
        'can_override_commission': _can_override_commission(request.user),
        'transaction_types': EServiceTransaction.TRANSACTION_TYPE_CHOICES,
        # ?type=&machine= presets (e.g. "recharge" link from the liquidity dashboard)
        'preset_type': request.POST.get('transaction_type') or request.GET.get('type', ''),
        'preset_machine': request.POST.get('pos_machine_id') or request.GET.get('machine', ''),
        'title': 'مركز خدمات الدفع الإلكتروني',
    }
    return render(request, 'accounting/pos_machines/service_center.html', context)


@login_required
@custom_permission_required('accounting.view_eservicetransaction', redirect_url='core:main_screen')
def eservice_commission(request):
    """
    JSON: the commission computed by the matching rule.
    GET type=<CASH_IN|CASH_OUT|MERCHANT_RECHARGE>&machine=<id or empty>&amount=<principal>
    -> {has_rule, rule, field, commission}  (field = commission_revenue | commission_expense)
    """
    from django.http import JsonResponse
    from .models import EServiceTransaction, EServiceCommissionRule
    ttype = request.GET.get('type', '')
    if ttype not in {t for t, _ in EServiceTransaction.TRANSACTION_TYPE_CHOICES}:
        return JsonResponse({'error': 'نوع العملية غير صحيح.'}, status=400)
    try:
        amount = _parse_amount(request.GET.get('amount'), Decimal('0'))
    except (InvalidOperation, ValueError):
        return JsonResponse({'error': 'المبلغ غير صحيح.'}, status=400)
    machine = None
    machine_id = request.GET.get('machine') or ''
    if machine_id:
        machine = _scoped_machines(request).filter(pk=machine_id).first() if machine_id.isdigit() else None
        if machine is None:
            return JsonResponse({'error': 'الماكينة غير متاحة.'}, status=404)
    rule = EServiceCommissionRule.match(ttype, machine)
    field = 'commission_expense' if ttype == EServiceTransaction.MERCHANT_RECHARGE else 'commission_revenue'
    return JsonResponse({
        'has_rule': rule is not None,
        'rule': rule.name if rule else '',
        'field': field,
        'commission': str(rule.compute(amount)) if rule else None,
    })


@login_required
@custom_permission_required('accounting.view_eservicetransaction', redirect_url='core:main_screen')
def eservice_history(request):
    """Operations log: filters, totals and pagination."""
    from django.core.paginator import Paginator
    from django.db.models import Sum, Count
    from .models import EServiceTransaction

    qs = _scoped_eservice_transactions(request).select_related(
        'pos_machine', 'ewallet', 'treasury', 'created_by', 'agent', 'invoice'
    ).order_by('-date', '-created_at')

    f = {k: (request.GET.get(k) or '').strip() for k in ('date_from', 'date_to', 'type', 'machine', 'status', 'q')}
    try:
        if f['date_from']:
            qs = qs.filter(date__gte=datetime.date.fromisoformat(f['date_from']))
        if f['date_to']:
            qs = qs.filter(date__lte=datetime.date.fromisoformat(f['date_to']))
    except ValueError:
        messages.error(request, 'صيغة التاريخ غير صحيحة.')
    if f['type'] in {t for t, _ in EServiceTransaction.TRANSACTION_TYPE_CHOICES}:
        qs = qs.filter(transaction_type=f['type'])
    if f['machine'].isdigit():
        qs = qs.filter(pos_machine_id=int(f['machine']))
    if f['status'] in {s for s, _ in EServiceTransaction.STATUS_CHOICES}:
        qs = qs.filter(status=f['status'])
    if f['q']:
        qs = qs.filter(Q(transaction_number__icontains=f['q']) | Q(description__icontains=f['q']))

    posted = qs.filter(status=EServiceTransaction.POSTED)
    agg = posted.aggregate(principal=Sum('principal_amount'), earned=Sum('commission_revenue'),
                           fees=Sum('commission_expense'), cnt=Count('id'))
    net_cash = sum(
        (t.cash_effect for t in posted.select_related(None).only(
            'status', 'transaction_type', 'principal_amount', 'commission_revenue',
            'commission_expense', 'paid_on_credit').iterator()),
        Decimal('0'),
    )
    totals = {
        'count': qs.count(),
        'posted_count': agg['cnt'] or 0,
        'principal': agg['principal'] or Decimal('0'),
        'earned': agg['earned'] or Decimal('0'),
        'fees': agg['fees'] or Decimal('0'),
        'net_cash': net_cash,
    }

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get('page'))
    context = {
        'transactions': page_obj,
        'page_obj': page_obj,
        'page_range': paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1),
        'page_query': _query_without_page(request),
        'total_count': paginator.count,
        'item_label': 'عملية',
        'totals': totals,
        'filters': f,
        'machines': _scoped_machines(request),
        'transaction_types': EServiceTransaction.TRANSACTION_TYPE_CHOICES,
        'status_choices': EServiceTransaction.STATUS_CHOICES,
        'title': 'سجل حركات خدمات الدفع',
    }
    return render(request, 'accounting/pos_machines/eservice_history.html', context)


@login_required
@custom_permission_required('accounting.view_eservicetransaction', redirect_url='core:main_screen')
def eservice_detail(request, pk):
    txn = get_object_or_404(
        _scoped_eservice_transactions(request).select_related(
            'pos_machine', 'ewallet', 'treasury', 'agent', 'pos_session', 'journal_entry',
            'invoice', 'created_by', 'cancelled_by'),
        pk=pk,
    )
    user = request.user
    context = {
        'transaction': txn,
        'reversal_entry': txn.reversal_entry,
        'can_cancel': txn.status != txn.CANCELLED and (
            is_admin_user(user) or user.has_perm('accounting.cancel_eservicetransaction')),
        'can_post': txn.status == txn.DRAFT and (
            is_admin_user(user) or user.has_perm('accounting.change_eservicetransaction')),
        'title': f'تفاصيل الحركة: {txn.transaction_number}',
    }
    return render(request, 'accounting/pos_machines/eservice_detail.html', context)


@login_required
@custom_permission_required('accounting.change_eservicetransaction', redirect_url='core:main_screen')
def eservice_post(request, pk):
    if request.method == 'POST':
        from .models import EServiceTransaction
        txn = get_object_or_404(_scoped_eservice_transactions(request), pk=pk, status=EServiceTransaction.DRAFT)
        try:
            txn.post_transaction(user=request.user)
            messages.success(request, f'تم ترحيل الحركة {txn.transaction_number} بنجاح.')
        except ValidationError as e:
            messages.error(request, _error_text(e))
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء الترحيل: {_error_text(e)}')
    return redirect('accounting:eservice_history')


@login_required
@custom_permission_required('accounting.cancel_eservicetransaction', redirect_url='core:main_screen')
def eservice_cancel(request, pk):
    """POST only: cancel an operation (posted -> reversal entry dated today)."""
    from django.http import HttpResponseNotAllowed
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    txn = get_object_or_404(_scoped_eservice_transactions(request), pk=pk)
    try:
        txn.cancel_transaction(user=request.user, reason=request.POST.get('reason', ''))
        messages.success(request, f'تم إلغاء العملية {txn.transaction_number}'
                                  + (' وتسجيل قيد عكسي.' if txn.journal_entry_id else '.'))
    except ValidationError as e:
        messages.error(request, _error_text(e))
    except Exception as e:
        messages.error(request, f'حدث خطأ أثناء الإلغاء: {_error_text(e)}')
    return redirect('accounting:eservice_detail', pk=txn.pk)


# ---------------------------------------------------------------------------
# Commission rules (قواعد عمولات خدمات الدفع)
# ---------------------------------------------------------------------------

@login_required
@custom_permission_required('accounting.view_eservicecommissionrule', redirect_url='core:main_screen')
def eservice_rule_list(request):
    from .models import EServiceCommissionRule
    rules = EServiceCommissionRule.objects.select_related('pos_machine', 'pos_machine__branch').order_by(
        'transaction_type', 'name')
    context = {'rules': rules, 'title': 'قواعد عمولات خدمات الدفع'}
    return render(request, 'accounting/pos_machines/rule_list.html', context)


def _rule_form_view(request, rule=None):
    from .forms import EServiceCommissionRuleForm
    machines = _scoped_machines(request)
    form = EServiceCommissionRuleForm(request.POST or None, instance=rule, machines=machines)
    if request.method == 'POST' and form.is_valid():
        saved = form.save()
        messages.success(request, f'تم حفظ قاعدة العمولة "{saved.name}".')
        return redirect('accounting:eservice_rule_list')
    context = {
        'form': form,
        'rule': rule,
        'title': f'تعديل قاعدة: {rule.name}' if rule else 'إضافة قاعدة عمولة',
    }
    return render(request, 'accounting/pos_machines/rule_form.html', context)


@login_required
@custom_permission_required('accounting.add_eservicecommissionrule', redirect_url='core:main_screen')
def eservice_rule_create(request):
    return _rule_form_view(request)


@login_required
@custom_permission_required('accounting.change_eservicecommissionrule', redirect_url='core:main_screen')
def eservice_rule_update(request, pk):
    from .models import EServiceCommissionRule
    rule = get_object_or_404(EServiceCommissionRule, pk=pk)
    return _rule_form_view(request, rule)


@login_required
@custom_permission_required('accounting.delete_eservicecommissionrule', redirect_url='core:main_screen')
def eservice_rule_delete(request, pk):
    from django.http import HttpResponseNotAllowed
    from .models import EServiceCommissionRule
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    rule = get_object_or_404(EServiceCommissionRule, pk=pk)
    name = rule.name
    rule.delete()
    messages.success(request, f'تم حذف قاعدة العمولة "{name}".')
    return redirect('accounting:eservice_rule_list')

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
            try:
                with transaction.atomic():
                    self.object = form.save(commit=False)
                    self.object.created_by = self.request.user
                    self.object.status = JournalEntry.DRAFT
                    self.object.save()
                    items_formset.instance = self.object
                    items_formset.save()

                    if self.object.is_balanced():
                        try:
                            with transaction.atomic():
                                self.object.post()
                            messages.success(self.request, f'تم حفظ وترحيل القيد المحاسبي {self.object.reference} بنجاح.')
                        except ValidationError as e:
                            self.object.status = JournalEntry.DRAFT
                            messages.warning(self.request, f'تم حفظ القيد كمسودة. تعذر الترحيل التلقائي: {_error_text(e)}')
                    else:
                        messages.warning(self.request, 'تم حفظ القيد كمسودة نظراً لعدم اتزان المدين والدائن.')
            except ValidationError as e:
                # e.g. the date is in the closed accounting period (model-level lock)
                form.add_error(None, _error_text(e))
                return self.form_invalid(form)
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
        if self.request.GET.get('incoming'):
            return incoming_transfers(self.request.user).order_by('-date', '-created_at')
        return scope_transfers(InternalTransfer.objects.select_related(
            'from_treasury', 'from_bank_account', 'from_ewallet', 'to_treasury', 'to_bank_account', 'to_ewallet',
        ), self.request.user).order_by('-date', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['incoming_only'] = bool(self.request.GET.get('incoming'))
        context['incoming_count'] = incoming_transfers(self.request.user).count()
        context['title'] = 'تحويلات واردة بانتظار الاستلام' if context['incoming_only'] else 'تحويلات الأموال بين الحسابات'
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

    def get_initial(self):
        """Pre-fill from the liquidity dashboard: ?from_type=TREASURY&from_id=3 / ?to_type=...&to_id=..."""
        initial = super().get_initial()
        field_for = {InternalTransfer.TREASURY: 'treasury', InternalTransfer.BANK: 'bank_account',
                     InternalTransfer.EWALLET: 'ewallet'}
        for side in ('from', 'to'):
            kind = self.request.GET.get(f'{side}_type')
            obj_id = self.request.GET.get(f'{side}_id')
            if kind in field_for and obj_id and obj_id.isdigit():
                initial[f'{side}_account_type'] = kind
                initial[f'{side}_{field_for[kind]}'] = int(obj_id)
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة تحويل مالي جديد'
        context['cancel_url'] = reverse_lazy('accounting:transfer_list')
        from apps.tenant.core.idempotency import new_form_key
        context['idempotency_key'] = new_form_key(self.request)      # double-click on save = one transfer

        # Prepare live balance data for frontend display (read-only GET).
        from apps.tenant.accounting.models import Treasury, BankAccount, EWallet

        # one balance batch for all three lists, reused by the JSON and the template
        from apps.tenant.services.journal_service import with_balances
        treasuries = with_balances(scope_treasuries(Treasury.objects.select_related('account'), self.request.user))
        banks = with_balances(BankAccount.objects.select_related('account'))
        ewallets = with_balances(EWallet.objects.select_related('account'))
        accounts_data = {
            'TREASURY': {str(t.id): {'name': t.name, 'balance': str(t.current_balance)} for t in treasuries},
            'BANK': {str(b.id): {'name': b.name, 'balance': str(b.current_balance)} for b in banks},
            'EWALLET': {str(w.id): {'name': w.name, 'balance': str(w.current_balance)} for w in ewallets},
        }
        context['accounts_data'] = accounts_data
        context['treasuries_list'] = treasuries
        context['banks_list'] = banks
        context['ewallets_list'] = ewallets
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        from apps.tenant.invoicing.models import POSSession
        from apps.tenant.core.idempotency import find_by_idempotency_key, parse_idempotency_key
        open_session = POSSession.objects.filter(user=self.request.user, status=POSSession.OPEN).first()
        if open_session:
            form.instance.pos_session = open_session

        idem_key = parse_idempotency_key(
            self.request.POST.get('idempotency_key') or self.request.headers.get('X-Idempotency-Key')
        )
        existing = find_by_idempotency_key(InternalTransfer, idem_key, created_by=self.request.user)
        if existing is not None:
            self.object = existing
            return redirect(self.get_success_url())
        form.instance.idempotency_key = idem_key
        try:
            with transaction.atomic():
                response = super().form_valid(form)
        except IntegrityError:
            existing = find_by_idempotency_key(InternalTransfer, idem_key, created_by=self.request.user)
            if existing is not None:
                self.object = existing
                return redirect(self.get_success_url())
            form.add_error(None, 'حصل تعارض في الحفظ — حدّث الصفحة وجرّب تاني.')
            return self.form_invalid(form)
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
        t = self.object
        from .forms import scoped_treasuries
        mine = scoped_treasuries(self.request.user)
        is_sender = t.from_account_type != InternalTransfer.TREASURY or mine.filter(pk=t.from_treasury_id).exists()
        is_receiver = t.to_account_type == InternalTransfer.TREASURY and mine.filter(pk=t.to_treasury_id).exists()
        context['is_sender_branch'] = is_sender
        context['is_receiver_branch'] = is_receiver
        context['can_receive'] = t.status == InternalTransfer.IN_TRANSIT and is_receiver
        return context


class InternalTransferConfirmView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_internaltransfer'

    def post(self, request, pk):
        transfer = get_object_or_404(scope_transfers(InternalTransfer.objects.all(), request.user), pk=pk)
        if transfer.status == InternalTransfer.DRAFT:
            try:
                if transfer.from_account_type == InternalTransfer.TREASURY:
                    _check_treasury_branch(request.user, treasuries=[transfer.from_treasury])
                transfer.confirm_transfer(user=request.user)
                if transfer.status == InternalTransfer.IN_TRANSIT:
                    messages.success(request, f'تم إرسال التحويل إلى {transfer.destination_branch} — المبلغ الآن "نقدية بالطريق" حتى يؤكد الفرع المستلم الاستلام.')
                else:
                    messages.success(request, 'تم اعتماد التحويل وتوليد القيد المحاسبي بنجاح.')
            except Exception as e:
                messages.error(request, f'حدث خطأ أثناء الاعتماد: {_error_text(e)}')
        return redirect('accounting:transfer_detail', pk=transfer.pk)


class InternalTransferCancelView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_internaltransfer'

    def post(self, request, pk):
        transfer = get_object_or_404(scope_transfers(InternalTransfer.objects.all(), request.user), pk=pk)
        try:
            if transfer.status == InternalTransfer.IN_TRANSIT and transfer.from_account_type == InternalTransfer.TREASURY:
                _check_treasury_branch(request.user, treasuries=[transfer.from_treasury])
            transfer.cancel_transfer(user=request.user)
            messages.success(request, 'تم إلغاء التحويل وتسجيل قيد عكسي للقيد المحاسبي المربوط به بنجاح.')
        except Exception as e:
            messages.error(request, f'حدث خطأ أثناء الإلغاء: {_error_text(e)}')
        return redirect('accounting:transfer_detail', pk=transfer.pk)


def incoming_transfers(user):
    """Inter-branch transfers waiting for THIS branch (current branch's treasuries) to receive."""
    from .forms import scoped_treasuries
    return InternalTransfer.objects.filter(
        status=InternalTransfer.IN_TRANSIT,
        to_account_type=InternalTransfer.TREASURY,
        to_treasury__in=scoped_treasuries(user),
    ).select_related('from_treasury__branch', 'to_treasury__branch')


def _receiving_transfer_or_404(request, pk):
    return get_object_or_404(incoming_transfers(request.user), pk=pk)


class InternalTransferReceiveView(CustomPermissionRequiredMixin, View):
    """Receiving branch confirms the cash arrived (optionally a smaller amount)."""
    permission_required = 'accounting.change_internaltransfer'

    def post(self, request, pk):
        transfer = _receiving_transfer_or_404(request, pk)
        raw = (request.POST.get('received_amount') or '').strip().replace(',', '')
        note = (request.POST.get('receive_note') or '').strip()
        try:
            amount = Decimal(raw) if raw else None
        except Exception:
            messages.error(request, 'المبلغ المستلم غير صالح.')
            return redirect('accounting:transfer_detail', pk=transfer.pk)
        try:
            transfer.receive_transfer(user=request.user, received_amount=amount, note=note)
            if transfer.received_amount is not None and transfer.received_amount < transfer.amount:
                messages.warning(request, f'تم الاستلام مع تسجيل عجز {transfer.amount - transfer.received_amount:,.2f} ج.م.')
            else:
                messages.success(request, 'تم تأكيد استلام التحويل وإضافة المبلغ للخزينة.')
        except Exception as e:
            messages.error(request, f'تعذر تأكيد الاستلام: {_error_text(e)}')
        return redirect('accounting:transfer_detail', pk=transfer.pk)


class InternalTransferRejectView(CustomPermissionRequiredMixin, View):
    """Receiving branch refuses: the money goes back to the sending treasury."""
    permission_required = 'accounting.change_internaltransfer'

    def post(self, request, pk):
        transfer = _receiving_transfer_or_404(request, pk)
        note = (request.POST.get('receive_note') or '').strip()
        if not note:
            messages.error(request, 'برجاء كتابة سبب الرفض.')
            return redirect('accounting:transfer_detail', pk=transfer.pk)
        try:
            transfer.reject_transfer(user=request.user, note=note)
            messages.success(request, 'تم رفض التحويل وإعادة المبلغ لخزينة الفرع المرسل.')
        except Exception as e:
            messages.error(request, f'تعذر رفض التحويل: {_error_text(e)}')
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
            messages.error(self.request, f"تم حفظ الشيك ولكن تعذر إنشاء القيد المحاسبي: {_error_text(e)}")
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
            messages.error(self.request, f"تم حفظ الشيك ولكن تعذر توليد القيد المحاسبي: {_error_text(e)}")
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
                messages.error(request, f"تعذر تنفيذ التحصيل: {_error_text(e)}")
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
                messages.error(request, f"تعذر إثبات الارتداد: {_error_text(e)}")
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
                messages.error(request, f"تعذر إثبات صرف الشيك: {_error_text(e)}")
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
            messages.error(request, f"تعذر إلغاء الشيك: {_error_text(e)}")
        return redirect('accounting:cheque_detail', pk=cheque.pk)


class ChequeBookListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_chequebook'
    model = ChequeBook
    queryset = ChequeBook.objects.select_related('bank_account').order_by('-id')
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
        qs = FixedAsset.with_totals(FixedAsset.objects.select_related('branch', 'asset_account', 'accumulated_depreciation_account'))
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
        try:
            return self._form_valid_unlocked(form)
        except ValidationError as e:  # e.g. purchase date in the closed accounting period
            form.add_error(None, _error_text(e))
            return self.form_invalid(form)

    def _form_valid_unlocked(self, form):
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
        try:
            return self._post_unlocked(request, pk)
        except ValidationError as e:  # e.g. disposal date in the closed accounting period
            messages.error(request, _error_text(e))
            return redirect('accounting:fixed_asset_detail', pk=pk)

    def _post_unlocked(self, request, pk):
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
            asset = FixedAsset.objects.select_for_update(of=('self',)).get(pk=asset.pk)
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

        all_active_assets = FixedAsset.with_totals(FixedAsset.objects.filter(status=FixedAsset.AssetStatus.ACTIVE))
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
        try:
            return self._post_unlocked(request)
        except ValidationError as e:  # e.g. run date in the closed accounting period
            messages.error(request, _error_text(e))
            return redirect('accounting:fixed_asset_depreciation_run')

    def _post_unlocked(self, request):
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
        qs = Loan.with_totals(Loan.objects.select_related('branch', 'loan_account'))
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
        try:
            return self._form_valid_unlocked(form)
        except ValidationError as e:  # e.g. start date in the closed accounting period
            form.add_error(None, _error_text(e))
            return self.form_invalid(form)

    def _form_valid_unlocked(self, form):
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
        try:
            return self._post_unlocked(request, pk)
        except ValidationError as e:  # e.g. payment date in the closed accounting period
            messages.error(request, _error_text(e))
            installment = get_object_or_404(LoanInstallment, pk=pk)
            return redirect('accounting:loan_detail', pk=installment.loan_id)

    def _post_unlocked(self, request, pk):
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
            installment = LoanInstallment.objects.select_for_update(of=('self',)).select_related('loan').get(pk=installment.pk)
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


@login_required
@custom_permission_required('accounting.view_voucher', redirect_url='core:main_screen')
def partner_open_invoices(request):
    """Open credit invoices of a customer (receipt) / supplier (payment), oldest first —
    for the «ربط بالفواتير» panel of the voucher form."""
    from django.http import JsonResponse
    from apps.tenant.partners.models import Partner
    from .allocation_services import KIND_OF_VOUCHER, open_invoices
    partner = Partner.objects.filter(pk=request.GET.get('partner') or 0).first()
    kind = KIND_OF_VOUCHER.get(request.GET.get('type', ''))
    if partner is None or kind is None:
        return JsonResponse({'rows': []})
    d = open_invoices([partner], kind)[partner.pk]
    return JsonResponse({
        'rows': [{'id': r['invoice'].id, 'number': r['invoice'].invoice_number, 'date': str(r['invoice'].date),
                  'due': str(r['due']), 'days_overdue': r['days_overdue'], 'credit': str(r['credit']),
                  'outstanding': str(r['outstanding'])} for r in d['rows']],
        'open_total': str(d['open_total']), 'not_on_invoices': str(d['not_on_invoices']),
    })
