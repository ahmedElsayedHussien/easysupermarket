from django.shortcuts import render, get_object_or_404
from django.urls import reverse_lazy
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, DetailView, CreateView, UpdateView
from .models import JournalEntry, Account, Tax, PaymentMethod, Treasury, BankAccount, EWallet
from .forms import TaxForm, TreasuryForm, BankAccountForm, EWalletForm

class JournalEntryListView(LoginRequiredMixin, ListView):
    model = JournalEntry
    template_name = 'accounting/journal_list.html'
    context_object_name = 'entries'
    
    def get_queryset(self):
        return JournalEntry.objects.all().order_by('-date', '-created_at')
        
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'قيود اليومية'
        return context

class JournalEntryDetailView(LoginRequiredMixin, DetailView):
    model = JournalEntry
    template_name = 'accounting/journal_detail.html'
    context_object_name = 'entry'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f'تفاصيل القيد {self.object.reference}'
        return context

# Tax Views
class TaxListView(LoginRequiredMixin, ListView):
    model = Tax
    template_name = 'accounting/tax_list.html'
    context_object_name = 'taxes'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'الضرائب'
        return context

class TaxCreateView(LoginRequiredMixin, CreateView):
    model = Tax
    form_class = TaxForm
    template_name = 'accounting/tax_form.html'
    success_url = reverse_lazy('accounting:tax_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة ضريبة'
        context['cancel_url'] = self.success_url
        return context

class TaxUpdateView(LoginRequiredMixin, UpdateView):
    model = Tax
    form_class = TaxForm
    template_name = 'accounting/tax_form.html'
    success_url = reverse_lazy('accounting:tax_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل ضريبة'
        context['cancel_url'] = self.success_url
        return context

# Treasury Views
class TreasuryListView(LoginRequiredMixin, ListView):
    model = Treasury
    template_name = 'accounting/treasury_list.html'
    context_object_name = 'treasuries'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'الخزائن'
        return context

class TreasuryCreateView(LoginRequiredMixin, CreateView):
    model = Treasury
    form_class = TreasuryForm
    template_name = 'accounting/treasury_form.html'
    success_url = reverse_lazy('accounting:treasury_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة خزينة'
        context['cancel_url'] = self.success_url
        return context

class TreasuryUpdateView(LoginRequiredMixin, UpdateView):
    model = Treasury
    form_class = TreasuryForm
    template_name = 'accounting/treasury_form.html'
    success_url = reverse_lazy('accounting:treasury_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل خزينة'
        context['cancel_url'] = self.success_url

# BankAccount Views
class BankAccountListView(LoginRequiredMixin, ListView):
    model = BankAccount
    template_name = 'accounting/bank_list.html'
    context_object_name = 'banks'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'حسابات البنوك'
        return context

class BankAccountCreateView(LoginRequiredMixin, CreateView):
    model = BankAccount
    form_class = BankAccountForm
    template_name = 'accounting/bank_form.html'
    success_url = reverse_lazy('accounting:bank_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة حساب بنكي'
        context['cancel_url'] = self.success_url
        return context

class BankAccountUpdateView(LoginRequiredMixin, UpdateView):
    model = BankAccount
    form_class = BankAccountForm
    template_name = 'accounting/bank_form.html'
    success_url = reverse_lazy('accounting:bank_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل حساب بنكي'
        context['cancel_url'] = self.success_url
        return context

# EWallet Views
class EWalletListView(LoginRequiredMixin, ListView):
    model = EWallet
    template_name = 'accounting/ewallet_list.html'
    context_object_name = 'ewallets'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'المحافظ الإلكترونية'
        return context

class EWalletCreateView(LoginRequiredMixin, CreateView):
    model = EWallet
    form_class = EWalletForm
    template_name = 'accounting/ewallet_form.html'
    success_url = reverse_lazy('accounting:ewallet_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة محفظة إلكترونية'
        context['cancel_url'] = self.success_url
        return context

class EWalletUpdateView(LoginRequiredMixin, UpdateView):
    model = EWallet
    form_class = EWalletForm
    template_name = 'accounting/ewallet_form.html'
    success_url = reverse_lazy('accounting:ewallet_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'تعديل محفظة إلكترونية'
        context['cancel_url'] = self.success_url
        return context
        return context

# Payment Method Views
class PaymentMethodListView(LoginRequiredMixin, ListView):
    model = PaymentMethod
    template_name = 'core/generic_list.html'
    context_object_name = 'objects'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'طرق الدفع'
        context['create_url'] = reverse_lazy('accounting:payment_method_create')
        context['update_url_name'] = 'accounting:payment_method_update'
        return context

class PaymentMethodCreateView(LoginRequiredMixin, CreateView):
    model = PaymentMethod
    fields = ['name', 'account', 'is_active']
    template_name = 'core/generic_form.html'
    success_url = reverse_lazy('accounting:payment_method_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'إضافة طريقة دفع'
        context['cancel_url'] = self.success_url
        return context

class PaymentMethodUpdateView(LoginRequiredMixin, UpdateView):
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
