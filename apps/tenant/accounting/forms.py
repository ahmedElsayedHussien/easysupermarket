from django import forms
from django.forms import inlineformset_factory
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
        # Filter payment accounts to show ASSET type (mainly cash, banks, etc)
        # For a better UX, maybe just show cash and bank accounts. Let's just use ASSET for now.
        self.fields['payment_account'].queryset = Account.objects.filter(account_type=Account.ASSET, parent__isnull=False)

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
        # Limit accounts to logical choices (not root accounts)
        self.fields['account'].queryset = Account.objects.filter(is_active=True).exclude(level=0)
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
