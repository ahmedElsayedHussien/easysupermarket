from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.urls import reverse_lazy
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from apps.tenant.core.mixins import CustomPermissionRequiredMixin

from django.views.generic import ListView, DetailView, CreateView, UpdateView, View
from .models import JournalEntry, Account, PaymentMethod, Treasury, BankAccount, EWallet, Expense, Voucher
from .forms import TreasuryForm, BankAccountForm, EWalletForm, ExpenseForm, ExpenseItemFormSet, VoucherForm



class JournalEntryListView(CustomPermissionRequiredMixin, ListView):
    permission_required = 'accounting.view_journalentry'

    model = JournalEntry
    template_name = 'accounting/journal_list.html'
    context_object_name = 'entries'
    paginate_by = 20
    
    def get_queryset(self):
        queryset = JournalEntry.objects.all().order_by('-date', '-created_at')
        
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

@login_required
def chart_of_accounts(request):
    accounts = Account.objects.all()
    has_accounts = accounts.exists()
    context = {'accounts': accounts, 'has_accounts': has_accounts, 'title': 'شجرة الحسابات'}
    return render(request, 'accounting/chart_of_accounts.html', context)

@login_required
def setup_default_accounts(request):
    from django.contrib import messages
    from django.core.management import call_command
    from django.shortcuts import redirect
    
    if Account.objects.exists():
        messages.error(request, 'لا يمكن إنشاء شجرة الحسابات لأنها موجودة بالفعل!')
        return redirect('accounting:chart_of_accounts')
        
    try:
        call_command('setup_chart_of_accounts', schema=request.tenant.schema_name)
        messages.success(request, 'تم زراعة شجرة الحسابات الافتراضية بنجاح!')
    except Exception as e:
        messages.error(request, f'حدث خطأ أثناء الإنشاء: {str(e)}')
        
    return redirect('accounting:chart_of_accounts')

@login_required
def trial_balance(request):
    accounts = Account.objects.all()
    # Logic to calculate trial balance
    context = {'accounts': accounts, 'title': 'ميزان المراجعة'}
    return render(request, 'accounting/trial_balance.html', context)

@login_required
def income_statement(request):
    revenue_accounts = Account.objects.filter(account_type=Account.REVENUE)
    expense_accounts = Account.objects.filter(account_type=Account.EXPENSE)
    
    total_revenue = sum(acc.current_balance for acc in revenue_accounts if not acc.has_children)
    total_expense = sum(acc.current_balance for acc in expense_accounts if not acc.has_children)
    net_income = total_revenue - total_expense

    context = {
        'revenue_accounts': revenue_accounts,
        'expense_accounts': expense_accounts,
        'total_revenue': total_revenue,
        'total_expense': total_expense,
        'net_income': net_income,
        'title': 'قائمة الدخل'
    }
    return render(request, 'accounting/income_statement.html', context)

@login_required
def balance_sheet(request):
    asset_accounts = Account.objects.filter(account_type=Account.ASSET)
    liability_accounts = Account.objects.filter(account_type=Account.LIABILITY)
    equity_accounts = Account.objects.filter(account_type=Account.EQUITY)
    
    total_assets = sum(acc.current_balance for acc in asset_accounts if not acc.has_children)
    total_liabilities = sum(acc.current_balance for acc in liability_accounts if not acc.has_children)
    total_equity = sum(acc.current_balance for acc in equity_accounts if not acc.has_children)
    
    revenue_accounts = Account.objects.filter(account_type=Account.REVENUE)
    expense_accounts = Account.objects.filter(account_type=Account.EXPENSE)
    
    net_income = sum(acc.current_balance for acc in revenue_accounts if not acc.has_children) - sum(acc.current_balance for acc in expense_accounts if not acc.has_children)
    
    total_liabilities_and_equity = total_liabilities + total_equity + net_income

    context = {
        'asset_accounts': asset_accounts,
        'liability_accounts': liability_accounts,
        'equity_accounts': equity_accounts,
        'total_assets': total_assets,
        'total_liabilities': total_liabilities,
        'total_equity': total_equity,
        'net_income': net_income,
        'total_liabilities_and_equity': total_liabilities_and_equity,
        'title': 'الميزانية العمومية'
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
        qs = super().get_queryset()
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
            
        return qs
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'المصروفات'
        return context

class ExpenseDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_expense'

    model = Expense
    template_name = 'accounting/expense_detail.html'
    context_object_name = 'expense'
    
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
                    total_amount += amount
            
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
        expense = get_object_or_404(Expense, pk=pk)
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
        expense = get_object_or_404(Expense, pk=pk)
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
        return Voucher.objects.filter(voucher_type=Voucher.RECEIPT).order_by('-date', '-created_at')

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
        return Voucher.objects.filter(voucher_type=Voucher.PAYMENT).order_by('-date', '-created_at')

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
        messages.success(self.request, 'تم حفظ مسودة السند بنجاح.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy('accounting:voucher_detail', kwargs={'pk': self.object.pk})

class VoucherDetailView(CustomPermissionRequiredMixin, DetailView):
    permission_required = 'accounting.view_voucher'

    model = Voucher
    template_name = 'accounting/voucher_detail.html'
    context_object_name = 'voucher'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"تفاصيل {self.object.get_voucher_type_display()} {self.object.voucher_number}"
        context['list_url'] = reverse_lazy('accounting:receipt_list') if self.object.voucher_type == Voucher.RECEIPT else reverse_lazy('accounting:payment_list')
        return context

class VoucherConfirmView(CustomPermissionRequiredMixin, View):
    permission_required = 'accounting.change_voucher'

    def post(self, request, pk):
        voucher = get_object_or_404(Voucher, pk=pk)
        if voucher.status == Voucher.DRAFT:
            try:
                voucher.confirm_voucher()
                messages.success(request, 'تم اعتماد السند وتوليد القيد بنجاح.')
            except Exception as e:
                messages.error(request, f'حدث خطأ: {str(e)}')
        return redirect('accounting:voucher_detail', pk=voucher.pk)


# ===========================================================================
# POS Machines (ماكينات الدفع الإلكتروني)
# ===========================================================================

@login_required
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
def eservice_center(request):
    """Main e-service center for handling recharge/withdrawal/transfer operations."""
    from .models import POSMachine, EWallet, Treasury, EServiceTransaction
    from apps.tenant.core.models import Branch

    machines = POSMachine.objects.filter(is_active=True).select_related('branch')
    ewallets = EWallet.objects.all()
    treasuries = Treasury.objects.all()

    if request.method == 'POST':
        transaction_type = request.POST.get('transaction_type')
        source_type = request.POST.get('source_type', 'MACHINE')
        pos_machine_id = request.POST.get('pos_machine_id') or None
        ewallet_id = request.POST.get('ewallet_id') or None
        treasury_id = request.POST.get('treasury_id')
        principal_amount = request.POST.get('principal_amount', '0')
        commission_revenue = request.POST.get('commission_revenue', '0') or '0'
        commission_expense = request.POST.get('commission_expense', '0') or '0'
        description = request.POST.get('description', '')

        if not treasury_id:
            messages.error(request, 'برجاء اختيار الخزينة / الدرج.')
        elif not principal_amount or Decimal(principal_amount) <= 0:
            messages.error(request, 'برجاء إدخال مبلغ صحيح.')
        elif source_type == 'MACHINE' and not pos_machine_id:
            messages.error(request, 'برجاء اختيار الماكينة.')
        elif source_type == 'EWALLET' and not ewallet_id:
            messages.error(request, 'برجاء اختيار المحفظة الإلكترونية.')
        else:
            try:
                treasury = Treasury.objects.get(id=treasury_id)
                txn = EServiceTransaction(
                    transaction_type=transaction_type,
                    source_type=source_type,
                    treasury=treasury,
                    principal_amount=Decimal(principal_amount),
                    commission_revenue=Decimal(commission_revenue),
                    commission_expense=Decimal(commission_expense),
                    description=description,
                    created_by=request.user,
                )
                if source_type == 'MACHINE' and pos_machine_id:
                    txn.pos_machine = POSMachine.objects.get(id=pos_machine_id)
                if source_type == 'EWALLET' and ewallet_id:
                    txn.ewallet = EWallet.objects.get(id=ewallet_id)
                txn.save()
                txn.post_transaction()
                messages.success(request, f'✅ تم تنفيذ العملية {txn.transaction_number} بنجاح وتم إنشاء القيد المحاسبي.')
                return redirect('accounting:eservice_center')
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
def eservice_detail(request, pk):
    from .models import EServiceTransaction
    txn = get_object_or_404(EServiceTransaction, pk=pk)
    context = {
        'transaction': txn,
        'title': f'تفاصيل الحركة: {txn.transaction_number}',
    }
    return render(request, 'accounting/pos_machines/eservice_detail.html', context)


@login_required
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
