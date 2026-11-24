from django import forms
from django.forms import inlineformset_factory
from django.db.models import Q, F
from decimal import Decimal
import datetime
from .models import (
    Treasury, BankAccount, EWallet, Expense, ExpenseItem, Account, Voucher, InternalTransfer,
    Cheque, ChequeBook, ChequeType, ChequeStatus, FixedAsset, AssetDepreciationLog,
    Loan, LoanInstallment, EServiceCommissionRule
)
from apps.tenant.partners.models import Partner


def _branch_scope(user):
    """
    Returns (restricted, branch) for MONEY operations (which treasury may be used).

    Inside a request (BranchMiddleware attached the context) everyone — admins
    included — is restricted to the CURRENT working branch: the header branch for
    admins (locked to the open shift's branch while a shift is open), the own
    branch for users. A voucher/expense/transfer can't use another branch's treasury.

    Without request context (management commands, tests building forms directly):
    admins unrestricted, users limited to their employee branch (legacy rule).
    """
    if user is None:
        return False, None
    from apps.tenant.core.access import user_working_branch
    has_context, branch = user_working_branch(user)
    if has_context:
        return True, branch
    from apps.tenant.core.decorators import is_admin_user
    if is_admin_user(user):
        return False, None
    emp = getattr(user, 'employee_profile', None)
    return True, (getattr(emp, 'branch', None) if emp else None)


def working_branch_of(user):
    """The branch new money documents of ``user`` go to (see core.access.get_working_branch)."""
    if user is None:
        return None
    from apps.tenant.core.access import user_working_branch
    has_context, branch = user_working_branch(user)
    if has_context:
        return branch
    emp = getattr(user, 'employee_profile', None)
    return getattr(emp, 'branch', None) if emp else None


def pos_defaults_for(user):
    """Per-branch POS payment defaults for the user's working branch (settings_helpers.get_pos_defaults)."""
    from apps.tenant.core.settings_helpers import get_pos_defaults
    return get_pos_defaults(working_branch_of(user))


def scoped_treasuries(user):
    """Treasuries the user may move money from/into: the current branch's only."""
    restricted, branch = _branch_scope(user)
    qs = Treasury.objects.all()
    if restricted:
        qs = qs.filter(branch=branch) if branch else qs.none()
    return qs


def destination_treasuries(user):
    """
    Target treasuries of an internal transfer: any branch's treasury. Sending to
    another branch is a two-step transfer — the money sits in "cash in transit"
    until the RECEIVING branch confirms it (InternalTransfer.receive_transfer).
    """
    return Treasury.objects.all()


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

class OpenPeriodDateMixin:
    """clean_date(): refuse a date inside the closed accounting period (SystemSetting.accounting_lock_date)."""

    def clean_date(self):
        date = self.cleaned_data.get('date')
        from apps.tenant.core.settings_helpers import is_date_locked, lock_error_message
        if date and is_date_locked(date):
            raise forms.ValidationError(lock_error_message(date))
        return date


class AccountForm(BaseGlassForm):
    class Meta:
        model = Account
        fields = ['code', 'name', 'account_type', 'parent', 'allow_reconcile', 'is_cogs', 'is_contra_revenue', 'is_contra', 'allow_direct_payment', 'description']
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
            'amount': forms.NumberInput(attrs={'class': 'item-amount', 'min': '0.01', 'step': '0.01'}),
        }

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is not None and amount <= 0:
            raise forms.ValidationError('مبلغ البند يجب أن يكون أكبر من صفر.')
        return amount

ExpenseItemFormSet = inlineformset_factory(
    Expense, 
    ExpenseItem, 
    form=ExpenseItemForm, 
    extra=1, 
    can_delete=True
)

class ExpenseForm(OpenPeriodDateMixin, BaseGlassForm):
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

        # Inside a request everyone works on the CURRENT branch (header / open shift).
        from apps.tenant.core.access import user_working_branch
        has_context, working_branch = user_working_branch(self.user)
        if has_context:
            is_admin = False
            user_branch = working_branch

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
        # Auto-heal any Treasuries, BankAccounts, or EWallets lacking an account
        for t in Treasury.objects.filter(account__isnull=True):
            t.ensure_account()
        for b in BankAccount.objects.filter(account__isnull=True):
            b.ensure_account()
        for w in EWallet.objects.filter(account__isnull=True):
            w.ensure_account()

        # Filter payment accounts to show ONLY active Treasuries, Bank Accounts, and EWallets
        # (non-admin users only see their own branch's treasuries)
        treasury_account_ids = list(scoped_treasuries(self.user).filter(account__isnull=False).values_list('account_id', flat=True))
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

    def _clean_percent(self, field):
        value = self.cleaned_data.get(field)
        if value is not None and (value < 0 or value > 100):
            raise forms.ValidationError('النسبة يجب أن تكون بين 0 و 100.')
        return value

    def clean_vat_percent(self):
        return self._clean_percent('vat_percent')

    def clean_withholding_tax_percent(self):
        return self._clean_percent('withholding_tax_percent')

    def clean(self):
        cleaned_data = super().clean()
        payment_account = cleaned_data.get('payment_account')
        branch = cleaned_data.get('branch')
        # A treasury-based payment account must belong to the expense's branch for non-admin users
        restricted, _ = _branch_scope(self.user)
        if restricted and payment_account and branch:
            treasury = Treasury.objects.filter(account=payment_account).select_related('branch').first()
            if treasury and treasury.branch_id != branch.id:
                self.add_error('payment_account', 'لا يمكن الصرف من خزينة تابعة لفرع آخر.')
        return cleaned_data

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

class VoucherForm(OpenPeriodDateMixin, BaseGlassForm):
    expense_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        required=False,
        label='حساب المصروف المباشر',
        widget=forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'})
    )

    class Meta:
        model = Voucher
        fields = [
            'date', 'amount', 'partner', 'account',
            'payment_method', 'treasury', 'bank_account', 'ewallet', 'transfer_bank_account',
            'paid_cash', 'paid_card', 'paid_ewallet', 'paid_bank',
            'description'
        ]
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'paid_cash': forms.NumberInput(attrs={'class': 'form-control bg-dark-glass text-white text-center fw-bold', 'min': '0', 'step': '0.01', 'id': 'id_paid_cash'}),
            'paid_card': forms.NumberInput(attrs={'class': 'form-control bg-dark-glass text-white text-center fw-bold', 'min': '0', 'step': '0.01', 'id': 'id_paid_card'}),
            'paid_ewallet': forms.NumberInput(attrs={'class': 'form-control bg-dark-glass text-white text-center fw-bold', 'min': '0', 'step': '0.01', 'id': 'id_paid_ewallet'}),
            'paid_bank': forms.NumberInput(attrs={'class': 'form-control bg-dark-glass text-white text-center fw-bold', 'min': '0', 'step': '0.01', 'id': 'id_paid_bank'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        is_admin = False
        if self.user:
            if self.user.is_superuser:
                is_admin = True
            elif hasattr(self.user, 'employee_profile'):
                is_admin = self.user.employee_profile.is_admin()

        self.is_admin = is_admin

        # Populate partner queryset with active partners and informative search labels
        from apps.tenant.partners.models import Partner
        self.fields['partner'].queryset = Partner.objects.filter(is_active=True).order_by('name')
        self.fields['partner'].empty_label = "--- اختر الشريك (عميل / مورد) ---"
        self.fields['partner'].label_from_instance = lambda obj: (
            f"{obj.name} "
            f"{'(' + (obj.phone or obj.mobile) + ') ' if (obj.phone or obj.mobile) else ''}"
            f"[{obj.get_partner_type_display()}]"
        )

        # Populate expense accounts queryset (leaf nodes with direct payment enabled)
        self.fields['expense_account'].queryset = Account.objects.filter(
            account_type=Account.EXPENSE,
            is_active=True,
            allow_direct_payment=True,
            children__isnull=True
        ).order_by('code')
        self.fields['expense_account'].empty_label = "--- اختر حساب المصروف المباشر ---"
        self.fields['expense_account'].label_from_instance = lambda obj: f"{obj.code} - {obj.name}"

        # Auto-heal any Treasuries, BankAccounts, or EWallets lacking an account
        for t in Treasury.objects.filter(account__isnull=True):
            t.ensure_account()
        for b in BankAccount.objects.filter(account__isnull=True):
            b.ensure_account()
        for w in EWallet.objects.filter(account__isnull=True):
            w.ensure_account()

        # الحساب المقابل يتم توجيهه تلقائياً من الشريك أو حساب المصروف (التحويلات تتم عبر شاشة التحويلات المباشرة)
        self.fields['account'].widget = forms.HiddenInput()
        self.fields['account'].required = False

        # Non-admin users can only collect into / pay from their own branch's treasuries
        self.fields['treasury'].queryset = scoped_treasuries(self.user)
        self.fields['treasury'].label = "خزينة السداد / التحصيل"
        self.fields['treasury'].empty_label = "--- اختر خزينة السداد / التحصيل ---"
        self.fields['bank_account'].label = "ماكينة الفيزا / الحساب البنكي"
        self.fields['bank_account'].empty_label = "--- اختر الحساب البنكي / ماكينة الفيزا ---"
        self.fields['ewallet'].label = "المحفظة الإلكترونية"
        self.fields['ewallet'].empty_label = "--- اختر المحفظة الإلكترونية ---"
        self.fields['transfer_bank_account'].label = "حساب إنستاباي / التحويل البنكي"
        self.fields['transfer_bank_account'].empty_label = "--- اختر حساب إنستاباي / التحويل البنكي ---"

        for field_name in ['paid_cash', 'paid_card', 'paid_ewallet', 'paid_bank']:
            self.fields[field_name].required = False

        # Auto-select treasury / bank / wallet if only 1 exists or from user's open shift
        if not self.instance.pk:
            from apps.tenant.invoicing.models import POSSession
            open_session = POSSession.objects.filter(user=self.user, status=POSSession.OPEN).first() if self.user else None
            defaults = pos_defaults_for(self.user)

            if open_session and open_session.treasury:
                self.fields['treasury'].initial = open_session.treasury
            elif defaults['treasury'] and self.fields['treasury'].queryset.filter(pk=defaults['treasury'].pk).exists():
                self.fields['treasury'].initial = defaults['treasury']
            elif self.fields['treasury'].queryset.count() == 1:
                self.fields['treasury'].initial = self.fields['treasury'].queryset.first()

            if defaults['bank_account']:
                self.fields['bank_account'].initial = defaults['bank_account']
            elif BankAccount.objects.count() == 1:
                self.fields['bank_account'].initial = BankAccount.objects.first()

            if defaults['instapay_account']:
                self.fields['transfer_bank_account'].initial = defaults['instapay_account']
            elif defaults['bank_account']:
                self.fields['transfer_bank_account'].initial = defaults['bank_account']
            elif BankAccount.objects.count() == 1:
                self.fields['transfer_bank_account'].initial = BankAccount.objects.first()

            if defaults['ewallet']:
                self.fields['ewallet'].initial = defaults['ewallet']
            elif EWallet.objects.count() == 1:
                self.fields['ewallet'].initial = EWallet.objects.first()

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is None or amount <= 0:
            raise forms.ValidationError('مبلغ السند يجب أن يكون أكبر من صفر.')
        return amount

    def _clean_non_negative(self, field):
        value = self.cleaned_data.get(field)
        if value is not None and value < 0:
            raise forms.ValidationError('لا يمكن إدخال مبلغ سالب.')
        return value

    def clean_paid_cash(self):
        return self._clean_non_negative('paid_cash')

    def clean_paid_card(self):
        return self._clean_non_negative('paid_card')

    def clean_paid_ewallet(self):
        return self._clean_non_negative('paid_ewallet')

    def clean_paid_bank(self):
        return self._clean_non_negative('paid_bank')

    def clean(self):
        cleaned_data = self._clean_voucher()
        treasury = cleaned_data.get('treasury')
        restricted, _ = _branch_scope(self.user)
        if restricted and treasury and not scoped_treasuries(self.user).filter(pk=treasury.pk).exists():
            self.add_error('treasury', 'لا يمكن استخدام خزينة تابعة لفرع آخر.')
        return cleaned_data

    def _clean_voucher(self):
        cleaned_data = super().clean()
        partner = cleaned_data.get('partner')
        expense_account = cleaned_data.get('expense_account')
        v_type = getattr(self, 'voucher_type', None)

        if partner and expense_account:
            raise forms.ValidationError('يرجى اختيار إما الشريك (المورد/العميل) أو حساب المصروف وليس الاثنين معاً.')

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
                if v_type == Voucher.RECEIPT:
                    if not partner.receivable_account:
                        raise forms.ValidationError({'partner': f'الشريك {partner.name} ليس له حساب عملاء مربوط.'})
                    cleaned_data['account'] = partner.receivable_account
                else:
                    if not partner.payable_account:
                        raise forms.ValidationError({'partner': f'الشريك {partner.name} ليس له حساب موردين مربوط.'})
                    cleaned_data['account'] = partner.payable_account
        elif expense_account and v_type == Voucher.PAYMENT:
            cleaned_data['account'] = expense_account
        else:
            if v_type == Voucher.PAYMENT:
                raise forms.ValidationError({'partner': 'يجب اختيار الشريك (المورد/العميل) أو حساب المصروف المباشر لإصدار سند الصرف.'})
            else:
                raise forms.ValidationError({'partner': 'يجب اختيار الشريك (العميل أو المورد) لإصدار سند القبض.'})

        # Validate Payment Method and its specific funding account
        payment_method = cleaned_data.get('payment_method')
        treasury = cleaned_data.get('treasury')
        bank_account = cleaned_data.get('bank_account')
        ewallet = cleaned_data.get('ewallet')

        if payment_method == Voucher.CASH:
            if not treasury:
                from apps.tenant.invoicing.models import POSSession
                open_session = POSSession.objects.filter(user=self.user, status=POSSession.OPEN).first() if self.user else None
                default_treasury = pos_defaults_for(self.user)['treasury']
                if open_session and open_session.treasury:
                    cleaned_data['treasury'] = open_session.treasury
                elif default_treasury and self.fields['treasury'].queryset.filter(pk=default_treasury.pk).exists():
                    cleaned_data['treasury'] = default_treasury  # the working branch's default
                elif self.fields['treasury'].queryset.count() == 1:
                    cleaned_data['treasury'] = self.fields['treasury'].queryset.first()
                else:
                    self.add_error('treasury', 'برجاء اختيار خزينة السداد / التحصيل.')
            elif self.user:
                from apps.tenant.invoicing.models import POSSession
                open_session = POSSession.objects.filter(user=self.user, status=POSSession.OPEN).first()
                if not open_session and not self.is_admin:
                    raise forms.ValidationError({'payment_method': 'عذراً، يجب فتح وردية كاشير أولاً لتسجيل السندات النقدية (الكاش). يُرجى التوجه لشاشة نقطة البيع وفتح الوردية.'})

        elif payment_method in (Voucher.BANK_TRANSFER, Voucher.CARD):
            transfer_bank_account = cleaned_data.get('transfer_bank_account')
            if payment_method == Voucher.BANK_TRANSFER:
                if not bank_account and transfer_bank_account:
                    bank_account = transfer_bank_account
                    cleaned_data['bank_account'] = transfer_bank_account
                elif bank_account and not transfer_bank_account:
                    transfer_bank_account = bank_account
                    cleaned_data['transfer_bank_account'] = bank_account

            if not bank_account:
                defaults = pos_defaults_for(self.user)
                if payment_method == Voucher.CARD and defaults['bank_account']:
                    cleaned_data['bank_account'] = defaults['bank_account']
                elif payment_method == Voucher.BANK_TRANSFER and defaults['instapay_account']:
                    cleaned_data['bank_account'] = defaults['instapay_account']
                    cleaned_data['transfer_bank_account'] = defaults['instapay_account']
                elif payment_method == Voucher.BANK_TRANSFER and defaults['bank_account']:
                    cleaned_data['bank_account'] = defaults['bank_account']
                    cleaned_data['transfer_bank_account'] = defaults['bank_account']
                elif BankAccount.objects.count() == 1:
                    first_bank = BankAccount.objects.first()
                    cleaned_data['bank_account'] = first_bank
                    if payment_method == Voucher.BANK_TRANSFER:
                        cleaned_data['transfer_bank_account'] = first_bank
                else:
                    if payment_method == Voucher.CARD:
                        self.add_error('bank_account', 'برجاء اختيار ماكينة الفيزا / الحساب البنكي.')
                    else:
                        self.add_error('transfer_bank_account', 'برجاء اختيار حساب إنستاباي / التحويل البنكي.')

        elif payment_method == Voucher.EWALLET:
            if not ewallet:
                defaults = pos_defaults_for(self.user)
                if defaults['ewallet']:
                    cleaned_data['ewallet'] = defaults['ewallet']
                elif EWallet.objects.count() == 1:
                    cleaned_data['ewallet'] = EWallet.objects.first()
                else:
                    self.add_error('ewallet', 'برجاء اختيار المحفظة الإلكترونية.')

        elif payment_method == Voucher.SPLIT:
            amount = cleaned_data.get('amount') or Decimal('0')
            paid_cash = cleaned_data.get('paid_cash') or Decimal('0')
            paid_card = cleaned_data.get('paid_card') or Decimal('0')
            paid_ewallet = cleaned_data.get('paid_ewallet') or Decimal('0')
            paid_bank = cleaned_data.get('paid_bank') or Decimal('0')

            sum_paid = paid_cash + paid_card + paid_ewallet + paid_bank

            if sum_paid <= 0:
                raise forms.ValidationError({'paid_cash': 'يجب إدخال مبالغ السداد المقسم بحيث تساوي إجمالي مبلغ السند.'})

            if abs(sum_paid - amount) > Decimal('0.05'):
                raise forms.ValidationError(f'إجمالي مبالغ السداد المقسم ({sum_paid}) لا يساوي إجمالي مبلغ السند ({amount}).')

            defaults = pos_defaults_for(self.user)

            if paid_cash > 0 and not treasury:
                from apps.tenant.invoicing.models import POSSession
                open_session = POSSession.objects.filter(user=self.user, status=POSSession.OPEN).first() if self.user else None
                if open_session and open_session.treasury:
                    cleaned_data['treasury'] = open_session.treasury
                elif defaults['treasury']:
                    cleaned_data['treasury'] = defaults['treasury']
                elif self.fields['treasury'].queryset.count() == 1:
                    cleaned_data['treasury'] = self.fields['treasury'].queryset.first()
                else:
                    self.add_error('treasury', 'برجاء اختيار الخزينة للجزء النقدي.')

            if paid_card > 0 and not bank_account:
                if defaults['bank_account']:
                    cleaned_data['bank_account'] = defaults['bank_account']
                elif BankAccount.objects.count() == 1:
                    cleaned_data['bank_account'] = BankAccount.objects.first()
                else:
                    self.add_error('bank_account', 'برجاء اختيار ماكينة الفيزا / الحساب البنكي لجزء البطاقة.')

            if paid_ewallet > 0 and not ewallet:
                if defaults['ewallet']:
                    cleaned_data['ewallet'] = defaults['ewallet']
                elif EWallet.objects.count() == 1:
                    cleaned_data['ewallet'] = EWallet.objects.first()
                else:
                    self.add_error('ewallet', 'برجاء اختيار المحفظة الإلكترونية لجزء المحفظة.')

            transfer_bank_account = cleaned_data.get('transfer_bank_account')
            if paid_bank > 0 and not transfer_bank_account:
                if defaults['instapay_account']:
                    cleaned_data['transfer_bank_account'] = defaults['instapay_account']
                elif defaults['bank_account']:
                    cleaned_data['transfer_bank_account'] = defaults['bank_account']
                elif BankAccount.objects.count() == 1:
                    cleaned_data['transfer_bank_account'] = BankAccount.objects.first()
                else:
                    self.add_error('transfer_bank_account', 'برجاء اختيار حساب إنستاباي / التحويل البنكي لجزء التحويل.')

        return cleaned_data

from .models import JournalEntry, JournalItem
from django.db.models import F

class JournalEntryForm(OpenPeriodDateMixin, BaseGlassForm):
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


class InternalTransferForm(OpenPeriodDateMixin, BaseGlassForm):
    class Meta:
        model = InternalTransfer
        fields = [
            'date', 'amount',
            'from_account_type', 'from_treasury', 'from_bank_account', 'from_ewallet',
            'to_account_type', 'to_treasury', 'to_bank_account', 'to_ewallet',
            'description'
        ]
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        # Ensure all treasuries, bank accounts, and ewallets are linked to ledger accounts
        for t in Treasury.objects.filter(account__isnull=True):
            t.ensure_account()
        for b in BankAccount.objects.filter(account__isnull=True):
            b.ensure_account()
        for w in EWallet.objects.filter(account__isnull=True):
            w.ensure_account()

        self.fields['from_treasury'].queryset = scoped_treasuries(self.user)  # own branch only for non-admins
        self.fields['from_bank_account'].queryset = BankAccount.objects.all()
        self.fields['from_ewallet'].queryset = EWallet.objects.all()

        self.fields['to_treasury'].queryset = destination_treasuries(self.user)  # admins: any branch (inter-branch transfer)
        self.fields['to_bank_account'].queryset = BankAccount.objects.all()
        self.fields['to_ewallet'].queryset = EWallet.objects.all()

        def make_balance_label(obj):
            bal = getattr(obj, 'current_balance', Decimal('0.00'))
            return f"{obj.name} (الرصيد: {bal:,.2f} ج.م)"

        for f in ['from_treasury', 'from_bank_account', 'from_ewallet', 'to_treasury', 'to_bank_account', 'to_ewallet']:
            self.fields[f].required = False
            self.fields[f].label_from_instance = make_balance_label


    def clean(self):
        cleaned_data = super().clean()
        from_type = cleaned_data.get('from_account_type')
        to_type = cleaned_data.get('to_account_type')
        amount = cleaned_data.get('amount')

        if amount is not None and amount <= 0:
            raise forms.ValidationError({'amount': 'يجب أن يكون مبلغ التحويل أكبر من صفر.'})

        # Validate Source Account
        src_obj = None
        if from_type == InternalTransfer.TREASURY:
            src_obj = cleaned_data.get('from_treasury')
            if not src_obj:
                raise forms.ValidationError({'from_treasury': 'برجاء اختيار الخزينة المحول منها.'})
        elif from_type == InternalTransfer.BANK:
            src_obj = cleaned_data.get('from_bank_account')
            if not src_obj:
                raise forms.ValidationError({'from_bank_account': 'برجاء اختيار الحساب البنكي المحول منه.'})
        elif from_type == InternalTransfer.EWALLET:
            src_obj = cleaned_data.get('from_ewallet')
            if not src_obj:
                raise forms.ValidationError({'from_ewallet': 'برجاء اختيار المحفظة الإلكترونية المحول منها.'})

        # Validate Destination Account
        dst_obj = None
        if to_type == InternalTransfer.TREASURY:
            dst_obj = cleaned_data.get('to_treasury')
            if not dst_obj:
                raise forms.ValidationError({'to_treasury': 'برجاء اختيار الخزينة المحول إليها.'})
        elif to_type == InternalTransfer.BANK:
            dst_obj = cleaned_data.get('to_bank_account')
            if not dst_obj:
                raise forms.ValidationError({'to_bank_account': 'برجاء اختيار الحساب البنكي المحول إليه.'})
        elif to_type == InternalTransfer.EWALLET:
            dst_obj = cleaned_data.get('to_ewallet')
            if not dst_obj:
                raise forms.ValidationError({'to_ewallet': 'برجاء اختيار المحفظة الإلكترونية المحول إليها.'})

        # Prevent converting to same account
        if src_obj and dst_obj and type(src_obj) == type(dst_obj) and src_obj.id == dst_obj.id:
            raise forms.ValidationError('غير مسموح بالتحويل من وإلى نفس الحساب.')

        # Check insufficient balance
        if src_obj and amount:
            src_balance = getattr(src_obj, 'current_balance', Decimal('0.00'))
            if src_balance < amount:
                raise forms.ValidationError({
                    'amount': f"عفواً، رصيد {src_obj.name} الحالي ({src_balance:,.2f} ج.م) لا يسمح بتحويل مبلغ {amount:,.2f} ج.م."
                })

        return cleaned_data


# ---------------------------------------------------------------------------
# Cheque Forms (أوراق القبض وأوراق الدفع)
# ---------------------------------------------------------------------------

class ChequeBookForm(BaseGlassForm):
    class Meta:
        model = ChequeBook
        fields = [
            'bank_account', 'book_number', 'series_prefix',
            'start_number', 'end_number', 'current_number', 'is_active', 'notes'
        ]
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get('start_number')
        end = cleaned_data.get('end_number')
        current = cleaned_data.get('current_number')

        if start and end and start > end:
            raise forms.ValidationError({'end_number': 'رقم نهاية الدفتر يجب أن يكون أكبر من أو يساوي رقم البداية.'})

        if current and start and end:
            if current < start or current > (end + 1):
                raise forms.ValidationError({'current_number': 'الرقم الحالي يجب أن يكون بين رقم البداية والنهاية.'})

        return cleaned_data


class ReceivableChequeForm(BaseGlassForm):
    """نموذج استلام شيك قبض من عميل"""
    class Meta:
        model = Cheque
        fields = [
            'partner', 'cheque_number', 'amount', 'received_date', 'due_date',
            'drawer_bank_name', 'drawer_branch', 'treasury', 'attachment', 'notes'
        ]
        widgets = {
            'received_date': forms.DateInput(attrs={'type': 'date'}),
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['partner'].queryset = Partner.objects.filter(is_active=True).order_by('name')
        self.fields['partner'].label = 'العميل / الشريك المستلم منه الشيك'
        self.fields['treasury'].queryset = Treasury.objects.all()
        self.fields['treasury'].label = 'الخزينة / الحافظة لحفظ الشيك'
        if not self.initial.get('received_date'):
            self.initial['received_date'] = datetime.date.today()

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if not amount or amount <= 0:
            raise forms.ValidationError('يجب إدخال مبلغ صحيح أكبر من الصفر.')
        return amount


class PayableChequeForm(BaseGlassForm):
    """نموذج تحرير شيك دفع لمورد من حسابات بنك الشركة"""
    class Meta:
        model = Cheque
        fields = [
            'partner', 'bank_account', 'cheque_book', 'cheque_number',
            'amount', 'received_date', 'due_date', 'payee_name', 'attachment', 'notes'
        ]
        widgets = {
            'received_date': forms.DateInput(attrs={'type': 'date'}),
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['partner'].queryset = Partner.objects.filter(is_active=True).order_by('name')
        self.fields['partner'].label = 'المورد / الشريك المستفيد'
        self.fields['bank_account'].queryset = BankAccount.objects.all()
        self.fields['bank_account'].label = 'حساب بنك الشركة المسحوب منه'
        self.fields['cheque_book'].queryset = ChequeBook.objects.filter(is_active=True)
        self.fields['cheque_book'].label = 'دفتر الشيكات (اختياري)'
        self.fields['received_date'].label = 'تاريخ التحرير'
        self.fields['due_date'].label = 'تاريخ الاستحقاق'
        if not self.initial.get('received_date'):
            self.initial['received_date'] = datetime.date.today()

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if not amount or amount <= 0:
            raise forms.ValidationError('يجب إدخال مبلغ صحيح أكبر من الصفر.')
        return amount


class ChequeCollectForm(forms.Form):
    """نموذج تأكيد التحصيل الفعلي لشيك القبض (في البنك أو الخزينة)"""
    DESTINATION_CHOICES = [
        ('BANK', 'إيداع وتحصيل في حساب بنكي'),
        ('TREASURY', 'تحصيل نقدي في الخزينة'),
    ]
    destination_type = forms.ChoiceField(
        choices=DESTINATION_CHOICES,
        label='جهة التحصيل والإيداع',
        initial='BANK',
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary', 'id': 'id_collect_dest_type'})
    )
    bank_account = forms.ModelChoiceField(
        queryset=BankAccount.objects.all(),
        required=False,
        label='الحساب البنكي للإيداع',
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary', 'id': 'id_collect_bank'})
    )
    treasury = forms.ModelChoiceField(
        queryset=Treasury.objects.all(),
        required=False,
        label='الخزينة المستلمة للتحصيل',
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary', 'id': 'id_collect_treasury'})
    )
    clearance_date = forms.DateField(
        label='تاريخ التحصيل الفعلي',
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control bg-dark text-white border-secondary'})
    )
    bank_fee = forms.DecimalField(
        label='عمولة ومصاريف التحصيل البنكية (ج.م إن وجدت)',
        initial=Decimal('0.00'),
        min_value=0,
        required=False,
        widget=forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'step': '0.01'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['bank_account'].queryset = BankAccount.objects.all()
        self.fields['treasury'].queryset = Treasury.objects.all()

    def clean(self):
        cleaned_data = super().clean()
        dest_type = cleaned_data.get('destination_type')
        if dest_type == 'BANK' and not cleaned_data.get('bank_account'):
            raise forms.ValidationError({'bank_account': 'برجاء اختيار الحساب البنكي للإيداع والتحصيل.'})
        if dest_type == 'TREASURY' and not cleaned_data.get('treasury'):
            raise forms.ValidationError({'treasury': 'برجاء اختيار الخزينة للتحصيل.'})
        return cleaned_data


class ChequeBounceForm(forms.Form):
    """نموذج إثبات ارتداد ورفض الشيك"""
    bounce_date = forms.DateField(
        label='تاريخ الارتداد والرفض',
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control bg-dark text-white border-secondary'})
    )
    bounce_reason = forms.CharField(
        label='سبب الرفض والارتداد',
        widget=forms.Textarea(attrs={'class': 'form-control bg-dark text-white border-secondary', 'rows': 2, 'placeholder': 'مثال: عدم كفاية الرصيد، اختلاف في التوقيع، إلخ'})
    )


class ChequeClearPayableForm(forms.Form):
    """نموذج إثبات صرف شيك الدفع من حساب بنك الشركة"""
    clearance_date = forms.DateField(
        label='تاريخ الصرف والخصم من البنك',
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control bg-dark text-white border-secondary'})
    )


class ChequeCancelPayableForm(forms.Form):
    """نموذج إلغاء شيك الدفع"""
    cancel_reason = forms.CharField(
        label='سبب إلغاء الشيك',
        widget=forms.Textarea(attrs={'class': 'form-control bg-dark text-white border-secondary', 'rows': 2, 'placeholder': 'اكتب سبب إلغاء الشيك...'})
    )


# ---------------------------------------------------------------------------
# Fixed Assets Forms (نماذج الأصول الثابتة والإهلاك)
# ---------------------------------------------------------------------------

class FixedAssetForm(BaseGlassForm):
    """نموذج تسجيل وتعديل الأصل الثابت"""
    PAYMENT_CHOICES = [
        ('OPENING', 'رصيد افتتاحي (أصل قائم مسبقاً قبل النظام)'),
        ('TREASURY', 'شراء نقدي جديد (سداد من خزينة نقدية)'),
        ('BANK', 'شراء بنكي جديد (تحويل / شيك من حساب بنكي)'),
        ('SUPPLIER', 'شراء آجل على الحساب (ذمم دائنة - مورد)'),
    ]

    payment_method_type = forms.ChoiceField(
        choices=PAYMENT_CHOICES,
        initial='OPENING',
        label='طريقة تمويل / شراء الأصل',
        widget=forms.Select(attrs={'class': 'form-select bg-dark-glass text-white', 'id': 'id_payment_method_type'})
    )
    treasury = forms.ModelChoiceField(
        queryset=Treasury.objects.all(),
        required=False,
        label='الخزينة المسدد منها',
        widget=forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'})
    )
    bank_account = forms.ModelChoiceField(
        queryset=BankAccount.objects.all(),
        required=False,
        label='الحساب البنكي المسدد منه',
        widget=forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'})
    )
    supplier = forms.ModelChoiceField(
        queryset=Partner.objects.filter(partner_type__in=['SUPPLIER', 'BOTH']),
        required=False,
        label='المورد الدائن',
        widget=forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'})
    )

    class Meta:
        model = FixedAsset
        fields = [
            'name', 'category', 'branch', 'location',
            'purchase_date', 'purchase_cost', 'salvage_value',
            'is_opening_balance', 'opening_accumulated_depreciation',
            'useful_life_years', 'depreciation_rate',
            'asset_account', 'accumulated_depreciation_account', 'depreciation_expense_account',
            'notes'
        ]
        widgets = {
            'purchase_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 2, 'placeholder': 'أي تفاصيل فنية، رقم سيريال، الضمان، إلخ...'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.tenant.services.journal_service import _get_system_account

        # Defaults for accounts
        if not self.instance.pk:
            try:
                acc_1210 = _get_system_account('1210')
                if acc_1210:
                    self.fields['asset_account'].initial = acc_1210
                acc_1220 = _get_system_account('1220')
                if acc_1220:
                    self.fields['accumulated_depreciation_account'].initial = acc_1220
                acc_5220 = _get_system_account('5220')
                if acc_5220:
                    self.fields['depreciation_expense_account'].initial = acc_5220
            except Exception:
                pass
        else:
            # On update: disable financial baseline fields to preserve ledger integrity
            self.fields['purchase_cost'].disabled = True
            self.fields['is_opening_balance'].disabled = True
            self.fields['opening_accumulated_depreciation'].disabled = True
            self.fields['payment_method_type'].widget = forms.HiddenInput()
            self.fields['treasury'].widget = forms.HiddenInput()
            self.fields['bank_account'].widget = forms.HiddenInput()
            self.fields['supplier'].widget = forms.HiddenInput()

        # Limit account choices to active leaves
        self.fields['asset_account'].queryset = Account.objects.filter(account_type=Account.ASSET, is_active=True)
        self.fields['accumulated_depreciation_account'].queryset = Account.objects.filter(account_type=Account.ASSET, is_active=True)
        self.fields['depreciation_expense_account'].queryset = Account.objects.filter(account_type=Account.EXPENSE, is_active=True)

    def clean(self):
        cleaned_data = super().clean()
        cost = cleaned_data.get('purchase_cost') or Decimal('0.00')
        salvage = cleaned_data.get('salvage_value') or Decimal('0.00')
        opening_dep = cleaned_data.get('opening_accumulated_depreciation') or Decimal('0.00')
        is_opening = cleaned_data.get('is_opening_balance')
        pay_type = cleaned_data.get('payment_method_type')

        if cost <= Decimal('0.00'):
            self.add_error('purchase_cost', 'تكلفة الشراء يجب أن تكون أكبر من الصفر.')

        if salvage >= cost:
            self.add_error('salvage_value', 'القيمة التخريدية (الخردة) لا يمكن أن تتجاوز أو تساوي تكلفة الأصل الأصلية.')

        if opening_dep > (cost - salvage):
            self.add_error('opening_accumulated_depreciation', 'مجمع الإهلاك السابق لا يمكن أن يتجاوز القيمة القابلة للإهلاك للأصل.')

        # Validate financing sources on new asset creation
        if not self.instance.pk:
            if pay_type == 'TREASURY' and not cleaned_data.get('treasury'):
                self.add_error('treasury', 'برجاء تحديد الخزينة المسدد منها قيمة الشراء.')
            elif pay_type == 'BANK' and not cleaned_data.get('bank_account'):
                self.add_error('bank_account', 'برجاء تحديد الحساب البنكي المسدد منه قيمة الشراء.')
            elif pay_type == 'SUPPLIER' and not cleaned_data.get('supplier'):
                self.add_error('supplier', 'برجاء تحديد المورد الدائن لقيد الشراء الآجل.')

        return cleaned_data


class FixedAssetDisposalForm(forms.Form):
    """نموذج استبعاد أو بيع الأصل الثابت وتوليد قيد الإقفال المحاسبي"""
    DESTINATION_CHOICES = [
        ('NONE', 'تخريد / استبعاد بدون مقابل (تلف أو إهلاك كامل)'),
        ('TREASURY', 'بيع نقدي وإيداع في خزينة'),
        ('BANK', 'بيع بنكي وإيداع في حساب بنك'),
    ]

    disposal_date = forms.DateField(
        label='تاريخ الاستبعاد / البيع',
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control bg-dark text-white border-secondary'})
    )
    disposal_amount = forms.DecimalField(
        label='سعر البيع / المبلغ المحصل',
        initial=Decimal('0.00'),
        min_value=Decimal('0.00'),
        widget=forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'step': '0.01'})
    )
    destination_type = forms.ChoiceField(
        label='طريقة تحصيل القيمة (إن وجدت)',
        choices=DESTINATION_CHOICES,
        initial='NONE',
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary', 'id': 'id_disp_dest_type'})
    )
    treasury = forms.ModelChoiceField(
        label='الخزينة المستلمة',
        queryset=Treasury.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'})
    )
    bank_account = forms.ModelChoiceField(
        label='الحساب البنكي المستلم',
        queryset=BankAccount.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'})
    )
    disposal_notes = forms.CharField(
        label='سبب الاستبعاد / ملاحظات',
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control bg-dark text-white border-secondary', 'rows': 2, 'placeholder': 'تفاصيل عملية البيع أو سبب الاستبعاد...'})
    )

    def clean(self):
        cleaned_data = super().clean()
        dest = cleaned_data.get('destination_type')
        amt = cleaned_data.get('disposal_amount') or Decimal('0.00')

        if amt > Decimal('0.00') and dest == 'NONE':
            raise forms.ValidationError('لقد حددت مبلغ بيع، برجاء اختيار الخزينة أو البنك المستلم لقيمة البيع.')

        if dest == 'TREASURY' and not cleaned_data.get('treasury'):
            raise forms.ValidationError({'treasury': 'برجاء اختيار الخزينة المستلمة لمبلغ البيع.'})

        if dest == 'BANK' and not cleaned_data.get('bank_account'):
            raise forms.ValidationError({'bank_account': 'برجاء اختيار الحساب البنكي المستلم لمبلغ البيع.'})

        return cleaned_data


class FixedAssetDepreciationRunForm(forms.Form):
    """نموذج تشغيل الإهلاك الشهري بضغطة زر لجميع الأصول المستحقة"""
    year = forms.IntegerField(
        label='السنة المالية',
        required=False,
        initial=datetime.date.today().year,
        widget=forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary'})
    )
    month = forms.ChoiceField(
        label='الشهر',
        required=False,
        choices=[(str(i), f"شهر {i:02d}") for i in range(1, 13)],
        initial=datetime.date.today().month,
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'})
    )
    run_date = forms.DateField(
        label='تاريخ ترحيل القيد',
        required=False,
        initial=datetime.date.today,
        input_formats=['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d', '%m/%d/%Y', '%d.%m.%Y'],
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control bg-dark text-white border-secondary'})
    )
    notes = forms.CharField(
        label='بيان القيد',
        required=False,
        initial='إهلاك الأصول الثابتة الدوري',
        widget=forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary'})
    )

    def clean(self):
        cleaned_data = super().clean()
        today = datetime.date.today()
        if not cleaned_data.get('year'):
            cleaned_data['year'] = today.year
        if not cleaned_data.get('month'):
            cleaned_data['month'] = str(today.month)
        if not cleaned_data.get('run_date'):
            cleaned_data['run_date'] = today
        return cleaned_data


# ---------------------------------------------------------------------------
# Loans Forms (نماذج القروض والتسهيلات وسداد الأقساط)
# ---------------------------------------------------------------------------

class LoanForm(BaseGlassForm):
    """نموذج تسجيل القرض وتوليد جدول الأقساط وقيد الاستلام"""
    DISBURSEMENT_CHOICES = [
        ('BANK', 'إيداع بنكي مباشر (في حساب بنك الشركة)'),
        ('TREASURY', 'استلام نقدي (في خزينة الشركة)'),
        ('OPENING', 'قرض قائم مسبقاً (رصيد افتتاحي سابق دون سحب نقدي الآن)'),
    ]

    disbursement_destination = forms.ChoiceField(
        choices=DISBURSEMENT_CHOICES,
        initial='BANK',
        label='طريقة استلام أصل مبلغ القرض',
        widget=forms.Select(attrs={'class': 'form-select bg-dark-glass text-white', 'id': 'id_disb_destination'})
    )
    bank_account = forms.ModelChoiceField(
        queryset=BankAccount.objects.all(),
        required=False,
        label='الحساب البنكي المودع به القرض',
        widget=forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'})
    )
    treasury = forms.ModelChoiceField(
        queryset=Treasury.objects.all(),
        required=False,
        label='الخزينة المودع بها القرض',
        widget=forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'})
    )
    bank_fee = forms.DecimalField(
        label='مصاريف إدارية بنكية مخصومة عند الصرف',
        initial=Decimal('0.00'),
        min_value=Decimal('0.00'),
        required=False,
        widget=forms.NumberInput(attrs={'class': 'form-control bg-dark-glass text-white', 'step': '0.01'})
    )

    class Meta:
        model = Loan
        fields = [
            'name', 'loan_type', 'lender_name', 'contract_number', 'branch',
            'principal_amount', 'total_interest_amount',
            'start_date', 'installments_count', 'first_due_date',
            'loan_account', 'interest_expense_account',
            'notes'
        ]
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'first_due_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 2, 'placeholder': 'تفاصيل إضافية، رقم العقد، شروط السداد، الضمانات...'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.tenant.services.journal_service import _get_system_account

        # Defaults for accounts
        if not self.instance.pk:
            try:
                acc_2210 = _get_system_account('2210')
                if acc_2210:
                    self.fields['loan_account'].initial = acc_2210
                acc_5500 = _get_system_account('5500')
                if acc_5500:
                    self.fields['interest_expense_account'].initial = acc_5500
            except Exception:
                pass
            
            # Default dates
            today = datetime.date.today()
            self.fields['start_date'].initial = today
            # First installment due next month
            y = today.year
            m = today.month + 1
            if m > 12:
                y += 1
                m = 1
            d = min(today.day, 28)
            self.fields['first_due_date'].initial = datetime.date(y, m, d)

        self.fields['loan_account'].queryset = Account.objects.filter(account_type=Account.LIABILITY, is_active=True)
        self.fields['interest_expense_account'].queryset = Account.objects.filter(account_type=Account.EXPENSE, is_active=True)

    def clean(self):
        cleaned_data = super().clean()
        principal = cleaned_data.get('principal_amount') or Decimal('0.00')
        interest = cleaned_data.get('total_interest_amount') or Decimal('0.00')
        count = cleaned_data.get('installments_count') or 0
        dest = cleaned_data.get('disbursement_destination')

        if principal <= Decimal('0.00'):
            self.add_error('principal_amount', 'أصل مبلغ القرض يجب أن يكون أكبر من الصفر.')

        if count <= 0:
            self.add_error('installments_count', 'عدد الأقساط يجب أن يكون 1 قسط على الأقل.')

        if not self.instance.pk:
            if dest == 'BANK' and not cleaned_data.get('bank_account'):
                self.add_error('bank_account', 'برجاء اختيار الحساب البنكي المودع به مبلغ القرض.')
            elif dest == 'TREASURY' and not cleaned_data.get('treasury'):
                self.add_error('treasury', 'برجاء اختيار الخزينة المودع بها مبلغ القرض.')

        return cleaned_data


class LoanInstallmentPayForm(forms.Form):
    """نموذج سداد قسط القرض الفردي وتوليد قيد السداد المركب"""
    PAY_DESTINATION_CHOICES = [
        ('BANK', 'خصم من حساب بنكي'),
        ('TREASURY', 'سداد نقدي من خزينة'),
    ]

    payment_date = forms.DateField(
        label='تاريخ السداد الفعلي',
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control bg-dark text-white border-secondary'})
    )
    payment_destination = forms.ChoiceField(
        label='جهة السداد والخصم',
        choices=PAY_DESTINATION_CHOICES,
        initial='BANK',
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary', 'id': 'id_installment_pay_dest'})
    )
    bank_account = forms.ModelChoiceField(
        label='الحساب البنكي المسدد منه',
        queryset=BankAccount.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'})
    )
    treasury = forms.ModelChoiceField(
        label='الخزينة المسدد منها',
        queryset=Treasury.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select bg-dark text-white border-secondary'})
    )
    payment_notes = forms.CharField(
        label='ملاحظات السداد',
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'placeholder': 'رقم التحويل أو الشيك أو الإيصال...'})
    )

    def clean(self):
        cleaned_data = super().clean()
        dest = cleaned_data.get('payment_destination')
        if dest == 'BANK' and not cleaned_data.get('bank_account'):
            raise forms.ValidationError({'bank_account': 'برجاء اختيار الحساب البنكي المسدد منه القسط.'})
        if dest == 'TREASURY' and not cleaned_data.get('treasury'):
            raise forms.ValidationError({'treasury': 'برجاء اختيار الخزينة المسدد منها القسط.'})
        return cleaned_data






class EServiceCommissionRuleForm(BaseGlassForm):
    """Commission rule for payment services (fixed + percent, clamped to min / max)."""

    class Meta:
        model = EServiceCommissionRule
        fields = ['name', 'transaction_type', 'pos_machine', 'machine_type', 'fixed_amount', 'percent',
                  'min_amount', 'max_amount', 'is_active']

    def __init__(self, *args, machines=None, **kwargs):
        super().__init__(*args, **kwargs)
        if machines is not None:
            self.fields['pos_machine'].queryset = machines
        self.fields['pos_machine'].empty_label = '— كل الماكينات —'
        self.fields['machine_type'].choices = [('', '— كل الأنواع —')] + [
            c for c in self.fields['machine_type'].choices if c[0]]

    def clean(self):
        cleaned = super().clean()
        for f in ('fixed_amount', 'percent', 'min_amount', 'max_amount'):
            v = cleaned.get(f)
            if v is not None and v < 0:
                self.add_error(f, 'لا يمكن أن تكون القيمة سالبة.')
        pct = cleaned.get('percent')
        if pct is not None and pct > 100:
            self.add_error('percent', 'النسبة لا يمكن أن تتجاوز 100%.')
        mn, mx = cleaned.get('min_amount'), cleaned.get('max_amount')
        if mn and mx and mx < mn:
            self.add_error('max_amount', 'الحد الأقصى يجب أن يكون أكبر من أو يساوي الحد الأدنى.')
        machine, mtype = cleaned.get('pos_machine'), cleaned.get('machine_type')
        if machine and mtype and machine.machine_type != mtype:
            self.add_error('machine_type', 'نوع الماكينة لا يطابق الماكينة المختارة. اتركه فارغاً عند اختيار ماكينة محددة.')
        elif machine:
            cleaned['machine_type'] = ''
        return cleaned
