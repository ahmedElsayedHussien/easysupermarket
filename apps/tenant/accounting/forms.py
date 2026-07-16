from django import forms
from django.forms import inlineformset_factory
from django.db.models import F
import datetime
from .models import Treasury, BankAccount, EWallet, Expense, ExpenseItem, Account, Voucher

class BaseGlassForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = 'form-check-input bg-dark-glass'
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs['class'] = 'form-select bg-dark-glass text-white'
            else:
                field.widget.attrs['class'] = 'form-control bg-dark-glass text-white'
                
            if field_name == 'opening_balance' and self.instance and self.instance.pk:
                field.disabled = True
                field.help_text = 'لا يمكن تعديل الرصيد الافتتاحي بعد الإنشاء. قم بعمل قيد تسوية بدلاً من ذلك.'

class EWalletForm(BaseGlassForm):
    class Meta:
        model = EWallet
        fields = ['name', 'opening_balance']

class ExpenseItemForm(BaseGlassForm):
    class Meta:
        model = ExpenseItem
        fields = ['description', 'amount']
        widgets = {
            'description': forms.TextInput(attrs={'placeholder': 'بيان البند'}),
            'amount': forms.NumberInput(attrs={'class': 'item-amount'}),
        }

ExpenseItemFormSet = inlineformset_factory(
    Expense, 
    ExpenseItem, 
    form=ExpenseItemForm, 
    extra=1, 
    can_delete=True
)

class ExpenseForm(BaseGlassForm):
    class Meta:
        model = Expense
        fields = ['date', 'branch', 'expense_account', 'payment_account', 'amount', 'vat_percent', 'withholding_tax_percent', 'description']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['amount'].widget.attrs['readonly'] = True
        self.fields['amount'].widget.attrs['class'] += ' text-success fw-bold bg-dark'
        # Filter expense accounts to only show those of type EXPENSE
        self.fields['expense_account'].queryset = Account.objects.filter(account_type=Account.EXPENSE, parent__isnull=False)
        # Filter payment accounts to show only Treasuries and EWallets
        treasury_account_ids = Treasury.objects.values_list('account_id', flat=True)
        ewallet_account_ids = EWallet.objects.values_list('account_id', flat=True)
        allowed_account_ids = list(treasury_account_ids) + list(ewallet_account_ids)
        self.fields['payment_account'].queryset = Account.objects.filter(id__in=allowed_account_ids)

class TreasuryForm(BaseGlassForm):
    class Meta:
        model = Treasury
        fields = ['name', 'branch', 'opening_balance']

class BankAccountForm(BaseGlassForm):
    class Meta:
        model = BankAccount
        fields = ['name', 'opening_balance']

class EWalletForm(BaseGlassForm):
    class Meta:
        model = EWallet
        fields = ['name', 'opening_balance']

class VoucherForm(BaseGlassForm):
    class Meta:
        model = Voucher
        fields = [
            'date', 'amount', 'partner', 'account',
            'payment_method', 'treasury', 'bank_account', 'ewallet', 'description'
        ]
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Limit accounts to leaf nodes (accounts that can receive transactions)
        self.fields['account'].queryset = Account.objects.filter(
            is_active=True, 
            rght=F('lft') + 1
        )
        self.fields['account'].required = False
        
    def clean(self):
        cleaned_data = super().clean()
        partner = cleaned_data.get('partner')
        account = cleaned_data.get('account')

        if partner:
            # Auto-assign account based on partner type
            if partner.partner_type == 'SUPPLIER':
                if not partner.payable_account:
                    raise forms.ValidationError({'partner': f'المورد {partner.name} ليس له حساب موردين مربوط.'})
                cleaned_data['account'] = partner.payable_account
            elif partner.partner_type == 'CUSTOMER':
                if not partner.receivable_account:
                    raise forms.ValidationError({'partner': f'العميل {partner.name} ليس له حساب عملاء مربوط.'})
                cleaned_data['account'] = partner.receivable_account
            else:
                if self.voucher_type == Voucher.RECEIPT:
                    if not partner.receivable_account:
                        raise forms.ValidationError({'partner': f'الشريك {partner.name} ليس له حساب عملاء مربوط.'})
                    cleaned_data['account'] = partner.receivable_account
                else:
                    if not partner.payable_account:
                        raise forms.ValidationError({'partner': f'الشريك {partner.name} ليس له حساب موردين مربوط.'})
                    cleaned_data['account'] = partner.payable_account
        else:
            if not account:
                raise forms.ValidationError({'account': 'يجب اختيار الحساب المقابل في حالة عدم اختيار شريك.'})
        
        return cleaned_data

from .models import JournalEntry, JournalItem
from django.db.models import F

class JournalEntryForm(BaseGlassForm):
    date = forms.DateField(
        label='التاريخ',
        widget=forms.DateInput(attrs={'type': 'date'}),
        initial=datetime.date.today
    )
    
    class Meta:
        model = JournalEntry
        fields = ['date', 'description']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 2}),
        }

class JournalItemForm(BaseGlassForm):
    class Meta:
        model = JournalItem
        fields = ['account', 'description', 'debit', 'credit']
        widgets = {
            'description': forms.TextInput(attrs={'placeholder': 'البيان (اختياري)'}),
            'debit': forms.NumberInput(attrs={'class': 'item-debit', 'step': '0.0001'}),
            'credit': forms.NumberInput(attrs={'class': 'item-credit', 'step': '0.0001'}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Prevent selecting parent accounts (only leaf nodes)
        self.fields['account'].queryset = Account.objects.filter(is_active=True, rght=F('lft') + 1)
        self.fields['debit'].required = False
        self.fields['credit'].required = False

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get('debit'):
            cleaned_data['debit'] = 0
        if not cleaned_data.get('credit'):
            cleaned_data['credit'] = 0
        return cleaned_data

from django.forms import BaseInlineFormSet

class BaseJournalItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
            
        total_debit = 0
        total_credit = 0
        debit_accounts = set()
        credit_accounts = set()
        
        for form in self.forms:
            if self.can_delete and self._should_delete_form(form):
                continue
            
            account = form.cleaned_data.get('account')
            debit = form.cleaned_data.get('debit', 0) or 0
            credit = form.cleaned_data.get('credit', 0) or 0
            
            if account:
                total_debit += debit
                total_credit += credit
                if debit > 0:
                    debit_accounts.add(account.id)
                if credit > 0:
                    credit_accounts.add(account.id)
                    
        if abs(total_debit - total_credit) >= 0.0001:
            raise forms.ValidationError(f"القيد غير متزن! إجمالي المدين ({total_debit}) لا يساوي إجمالي الدائن ({total_credit}).")
            
        common_accounts = debit_accounts.intersection(credit_accounts)
        if common_accounts:
            raise forms.ValidationError("لا يمكن استخدام نفس الحساب في الجانبين المدين والدائن في نفس القيد.")

JournalItemFormSet = inlineformset_factory(
    JournalEntry,
    JournalItem,
    form=JournalItemForm,
    formset=BaseJournalItemFormSet,
    extra=2,
    min_num=2,
    validate_min=True,
    can_delete=True
)
