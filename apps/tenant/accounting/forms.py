from django import forms
from django.forms import inlineformset_factory
from django.db.models import Q, F
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

class AccountForm(BaseGlassForm):
    class Meta:
        model = Account
        fields = ['code', 'name', 'account_type', 'parent', 'allow_reconcile', 'is_cogs', 'is_contra_revenue', 'description']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.db.models import Q
        # Show main/group accounts only (accounts with children OR main category levels 0, 1, 2)
        qs = Account.objects.filter(is_active=True).filter(
            Q(children__isnull=False) | Q(level__lte=2)
        ).distinct()
        
        if self.instance and self.instance.pk:
            # Exclude current account and its descendants to prevent circular hierarchy
            descendants_ids = self.instance.get_descendants(include_self=True).values_list('pk', flat=True)
            qs = qs.exclude(pk__in=descendants_ids)
            
        self.fields['parent'].queryset = qs

    def parent_type_map_json(self):
        import json
        parents = self.fields['parent'].queryset.values('id', 'account_type')
        return json.dumps({str(p['id']): p['account_type'] for p in parents})

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
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['amount'].widget.attrs['readonly'] = True
        self.fields['amount'].widget.attrs['class'] += ' text-success fw-bold bg-dark'

        # Filter branch choices: Admin sees all branches; regular employee sees their own branch only
        from apps.tenant.core.models import Branch
        is_admin = False
        user_branch = None

        if self.user:
            if self.user.is_superuser:
                is_admin = True
            elif hasattr(self.user, 'employee_profile'):
                emp = self.user.employee_profile
                is_admin = emp.is_admin()
                user_branch = emp.branch

        if is_admin:
            self.fields['branch'].queryset = Branch.objects.filter(is_active=True)
            self.fields['branch'].empty_label = "-- اختر الفرع --"
        else:
            if user_branch:
                self.fields['branch'].queryset = Branch.objects.filter(id=user_branch.id, is_active=True)
                self.fields['branch'].initial = user_branch
                self.fields['branch'].empty_label = None
            else:
                self.fields['branch'].queryset = Branch.objects.none()

        # Filter expense accounts for manual expense entry:
        # Exclude COGS, system-automated accounts (COGS, purchase returns, shift discrepancy),
        # and parent accounts that have child sub-accounts beneath them.
        self.fields['expense_account'].queryset = Account.objects.filter(
            account_type=Account.EXPENSE,
            is_active=True,
            parent__isnull=False
        ).exclude(
            is_cogs=True
        ).exclude(
            code__in=['5100', '5105', '5110', '5400']
        ).exclude(
            children__isnull=False
        ).order_by('code')
        # Filter payment accounts to show ONLY active Treasuries, Bank Accounts, and EWallets
        treasury_account_ids = list(Treasury.objects.filter(account__isnull=False).values_list('account_id', flat=True))
        bank_account_ids = list(BankAccount.objects.filter(account__isnull=False).values_list('account_id', flat=True))
        ewallet_account_ids = list(EWallet.objects.filter(account__isnull=False).values_list('account_id', flat=True))
        allowed_account_ids = set(treasury_account_ids + bank_account_ids + ewallet_account_ids)

        self.fields['payment_account'].queryset = Account.objects.filter(
            id__in=allowed_account_ids,
            is_active=True
        ).order_by('code')

    def clean_branch(self):
        branch = self.cleaned_data.get('branch')
        if self.user and not self.user.is_superuser:
            if hasattr(self.user, 'employee_profile'):
                emp = self.user.employee_profile
                if not emp.is_admin() and emp.branch:
                    return emp.branch
        return branch

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
        # Limit opposite accounts in VoucherForm to only active Treasuries, Bank Accounts, and E-Wallets
        treasury_account_ids = list(Treasury.objects.filter(account__isnull=False).values_list('account_id', flat=True))
        bank_account_ids = list(BankAccount.objects.filter(account__isnull=False).values_list('account_id', flat=True))
        ewallet_account_ids = list(EWallet.objects.filter(account__isnull=False).values_list('account_id', flat=True))
        allowed_account_ids = set(treasury_account_ids + bank_account_ids + ewallet_account_ids)

        self.fields['account'].queryset = Account.objects.filter(
            id__in=allowed_account_ids,
            is_active=True
        ).order_by('code')
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
        selected_accounts = []
        valid_rows_count = 0
        
        for form in self.forms:
            if self.can_delete and self._should_delete_form(form):
                continue
            
            account = form.cleaned_data.get('account')
            debit = form.cleaned_data.get('debit', 0) or 0
            credit = form.cleaned_data.get('credit', 0) or 0
            
            if account and (debit > 0 or credit > 0):
                valid_rows_count += 1
                # Prevent selecting the same account twice in the same entry
                if account.id in selected_accounts:
                    raise forms.ValidationError(f"لا يمكن اختيار نفس الحساب المحاسبي ({account}) أكثر من مرة في نفس القيد.")
                selected_accounts.append(account.id)
                
                total_debit += debit
                total_credit += credit

        if valid_rows_count < 2:
            raise forms.ValidationError("يجب إدخال سطرين على الأقل في القيد المحاسبي.")
            
        if total_debit <= 0 or total_credit <= 0:
            raise forms.ValidationError("يجب إدخال مبالغ مدينة ودائنة أكبر من الصفر.")

        if abs(total_debit - total_credit) >= 0.0001:
            diff = abs(total_debit - total_credit)
            raise forms.ValidationError(f"القيد غير متزن! إجمالي الطرف المدين ({total_debit:.2f}) لا يساوي إجمالي الطرف الدائن ({total_credit:.2f}). الفرق: {diff:.2f}")

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
