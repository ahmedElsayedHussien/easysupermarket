from django import forms
from .models import Tax, Treasury, BankAccount, EWallet

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

class TaxForm(BaseGlassForm):
    class Meta:
        model = Tax
        fields = ['name', 'rate', 'account', 'is_active']

class TreasuryForm(BaseGlassForm):
    class Meta:
        model = Treasury
        fields = ['name', 'branch', 'opening_balance', 'account']

class BankAccountForm(BaseGlassForm):
    class Meta:
        model = BankAccount
        fields = ['name', 'branch', 'opening_balance', 'account']

class EWalletForm(BaseGlassForm):
    class Meta:
        model = EWallet
        fields = ['name', 'branch', 'opening_balance', 'account']
