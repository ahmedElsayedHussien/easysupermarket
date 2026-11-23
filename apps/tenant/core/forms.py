from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.contrib.auth.models import Permission
from apps.tenant.core.models import Employee, Branch, Role
from django.db import transaction
from .models import SystemSetting
from .usernames import namespace_username, display_username

USERNAME_TAKEN_MSG = 'اسم المستخدم هذا مستخدم بالفعل.'
NO_BRANCH_HELP = 'الموظفون غير المديرين بدون فرع لن يروا أي بيانات خاصة بالفروع.'


def _clean_tenant_username(form, entered, exclude_pk=None):
    """
    Validate a username typed in a tenant and return the internal
    (namespaced) value. Uniqueness is checked ONLY against the namespaced
    form, so other tenants' usernames are never revealed.
    """
    entered = (entered or '').strip()
    if not entered:
        raise forms.ValidationError('اسم المستخدم مطلوب.')
    if '__' in entered:
        raise forms.ValidationError('اسم المستخدم لا يجب أن يحتوي على "__".')
    internal = namespace_username(entered)
    max_len = User._meta.get_field('username').max_length
    if len(internal) > max_len:
        raise forms.ValidationError('اسم المستخدم طويل جداً.')
    User._meta.get_field('username').run_validators(internal)
    qs = User.objects.filter(username__iexact=internal)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise forms.ValidationError(USERNAME_TAKEN_MSG)
    return internal

class SystemSettingForm(forms.ModelForm):
    class Meta:
        model = SystemSetting
        fields = '__all__'
        widgets = {
            'store_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'اسم المتجر للإيصال'}),
            'tax_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'الرقم الضريبي (إن وجد)'}),
            'commercial_register': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'السجل التجاري (إن وجد)'}),
            'receipt_footer': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'مثال: البضاعة المباعة لا ترد ولا تستبدل إلا خلال 14 يوم...'}),
            'large_amount_requires_customer': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': 'any'}),
            'low_stock_threshold': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'allow_negative_stock': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'auto_print_receipt': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'allow_pos_price_modification': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'show_pos_cost': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'pos_price_margin_percent': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': '0.01'}),
            'max_pos_discount_percentage': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'max': 100, 'step': '0.01'}),
            'apply_vat_by_default': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'default_pos_treasury': forms.Select(attrs={'class': 'form-select'}),
            'default_pos_bank_account': forms.Select(attrs={'class': 'form-select'}),
            'default_pos_instapay_account': forms.Select(attrs={'class': 'form-select'}),
            'default_pos_ewallet': forms.Select(attrs={'class': 'form-select'}),
            'lock_pos_payment_accounts': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'enable_expiry_tracking': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'auto_post_journals': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        radio_choices = [(True, ''), (False, '')]
        for field_name, field in self.fields.items():
            if field_name.startswith('show_nav_'):
                field.widget = forms.RadioSelect(choices=radio_choices)


class EmployeeUserCreationForm(UserCreationForm):
    first_name = forms.CharField(label='الاسم الأول', max_length=30, required=True)
    last_name = forms.CharField(label='الاسم الأخير', max_length=30, required=True)
    email = forms.EmailField(label='البريد الإلكتروني', required=False)
    password1 = forms.CharField(
        label='كلمة المرور',
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'أدخل كلمة مرور (6 أحرف على الأقل)'}),
        help_text='كلمة المرور يجب ألا تقل عن 6 أحرف.'
    )
    password2 = forms.CharField(
        label='تأكيد كلمة المرور',
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'أعد إدخال كلمة المرور للتأكيد'}),
        help_text='أدخل نفس كلمة المرور السابقة للتأكيد.'
    )
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.filter(is_active=True), 
        label='الفرع التابع له', 
        required=False,
        empty_label='الإدارة المركزية (بدون فرع)'
    )
    role = forms.ChoiceField(
        choices=Role.choices, 
        label='الدور الوظيفي', 
        initial=Role.CASHIER
    )
    user_permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='الصلاحيات المخصصة'
    )
    
    # HR Fields
    base_salary = forms.DecimalField(max_digits=10, decimal_places=2, initial=0.00, required=False, label='الراتب الأساسي')
    hourly_rate = forms.DecimalField(max_digits=8, decimal_places=2, initial=0.00, required=False, label='قيمة ساعة العمل')
    shift_start_time = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), required=False, label='ميعاد الحضور')
    shift_end_time = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), required=False, label='ميعاد الانصراف')
    deduction_per_hour = forms.DecimalField(max_digits=8, decimal_places=2, initial=0.00, required=False, label='قيمة الخصم للساعة')
    overtime_per_hour = forms.DecimalField(max_digits=8, decimal_places=2, initial=0.00, required=False, label='قيمة الإضافي للساعة')

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'last_name', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'username' in self.fields:
            self.fields['username'].label = 'اسم المستخدم (Username)'
            self.fields['username'].help_text = 'اسم تسجيل الدخول للنظام (حروف إنجليزية أو أرقام).'
        self.fields['branch'].help_text = NO_BRANCH_HELP

    def clean_username(self):
        # Stored as <schema>__<entered> (no prefix in SQLite mode).
        return _clean_tenant_username(self, self.cleaned_data.get('username'))

    def _post_clean(self):
        forms.ModelForm._post_clean(self)

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2:
            if password1 != password2:
                raise forms.ValidationError("كلمتا المرور غير متطابقتين.")
            if len(password1) < 6:
                raise forms.ValidationError("كلمة المرور يجب ألا تقل عن 6 أحرف.")
        return password2

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get('password1')
        p2 = cleaned_data.get('password2')
        if p1 and p2:
            if p1 != p2:
                self.add_error('password2', 'كلمتا المرور غير متطابقتين.')
            elif len(p1) < 6:
                self.add_error('password1', 'كلمة المرور يجب ألا تقل عن 6 أحرف.')
        return cleaned_data

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get('first_name')
        user.last_name = self.cleaned_data.get('last_name')
        user.email = self.cleaned_data.get('email')
        role = self.cleaned_data.get('role')
        # SECURITY: tenant roles never grant the global is_superuser/is_staff
        # flags (auth_user is shared by all tenants). Platform superusers keep
        # their flag untouched.

        if commit:
            user.save()
            # Create the employee profile
            Employee.objects.update_or_create(
                user=user,
                defaults={
                    'branch': self.cleaned_data.get('branch'),
                    'role': self.cleaned_data.get('role'),
                    'base_salary': self.cleaned_data.get('base_salary') or 0,
                    'hourly_rate': self.cleaned_data.get('hourly_rate') or 0,
                    'shift_start_time': self.cleaned_data.get('shift_start_time'),
                    'shift_end_time': self.cleaned_data.get('shift_end_time'),
                    'deduction_per_hour': self.cleaned_data.get('deduction_per_hour') or 0,
                    'overtime_per_hour': self.cleaned_data.get('overtime_per_hour') or 0,
                }
            )
            if role == Role.ADMIN:
                user.user_permissions.set(Permission.objects.all())
            else:
                user.user_permissions.set(self.cleaned_data.get('user_permissions', []))
        return user



class EmployeeUserUpdateForm(forms.ModelForm):
    first_name = forms.CharField(label='الاسم الأول', max_length=30, required=True)
    last_name = forms.CharField(label='الاسم الأخير', max_length=30, required=True)
    new_password = forms.CharField(
        label='كلمة المرور الجديدة (اختياري)',
        widget=forms.PasswordInput(attrs={'placeholder': 'أدخل كلمة مرور جديدة للتعيين'}),
        required=False,
        help_text='اتركه فارغاً إذا كنت لا ترغب بتغيير كلمة المرور الحالية.'
    )
    confirm_password = forms.CharField(
        label='تأكيد كلمة المرور الجديدة',
        widget=forms.PasswordInput(attrs={'placeholder': 'أعد كتابة كلمة المرور الجديدة'}),
        required=False
    )
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.filter(is_active=True), 
        label='الفرع التابع له', 
        required=False,
        empty_label='الإدارة المركزية (بدون فرع)'
    )
    role = forms.ChoiceField(
        choices=Role.choices, 
        label='الدور الوظيفي'
    )
    is_active = forms.BooleanField(label='حساب نشط', required=False)
    user_permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='الصلاحيات المخصصة'
    )

    # HR Fields
    base_salary = forms.DecimalField(max_digits=10, decimal_places=2, initial=0.00, required=False, label='الراتب الأساسي')
    hourly_rate = forms.DecimalField(max_digits=8, decimal_places=2, initial=0.00, required=False, label='قيمة ساعة العمل')
    shift_start_time = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), required=False, label='ميعاد الحضور')
    shift_end_time = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), required=False, label='ميعاد الانصراف')
    deduction_per_hour = forms.DecimalField(max_digits=8, decimal_places=2, initial=0.00, required=False, label='قيمة الخصم للساعة')
    overtime_per_hour = forms.DecimalField(max_digits=8, decimal_places=2, initial=0.00, required=False, label='قيمة الإضافي للساعة')

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'is_active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['branch'].help_text = NO_BRANCH_HELP
        if self.instance and self.instance.pk:
            # Show the entered name, never the internal tenant prefix.
            self.initial['username'] = display_username(self.instance.username)
            try:
                employee = self.instance.employee_profile
                self.fields['branch'].initial = employee.branch
                self.fields['role'].initial = employee.role
                self.fields['base_salary'].initial = employee.base_salary
                self.fields['hourly_rate'].initial = employee.hourly_rate
                self.fields['shift_start_time'].initial = employee.shift_start_time
                self.fields['shift_end_time'].initial = employee.shift_end_time
                self.fields['deduction_per_hour'].initial = employee.deduction_per_hour
                self.fields['overtime_per_hour'].initial = employee.overtime_per_hour
            except Employee.DoesNotExist:
                pass
            self.fields['user_permissions'].initial = self.instance.user_permissions.all()

    def clean_username(self):
        entered = (self.cleaned_data.get('username') or '').strip()
        current = self.instance.username if self.instance and self.instance.pk else ''
        if current and entered == display_username(current):
            return current  # unchanged (legacy users keep their raw name)
        return _clean_tenant_username(self, entered, exclude_pk=getattr(self.instance, 'pk', None))

    def clean(self):
        cleaned_data = super().clean()
        new_pass = cleaned_data.get('new_password')
        confirm_pass = cleaned_data.get('confirm_password')

        if new_pass or confirm_pass:
            if new_pass != confirm_pass:
                self.add_error('confirm_password', 'كلمتا المرور غير متطابقتين.')
            elif len(new_pass) < 6:
                self.add_error('new_password', 'كلمة المرور يجب ألا تقل عن 6 أحرف.')
        return cleaned_data

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        role = self.cleaned_data.get('role')
        # SECURITY: tenant roles never grant the global is_superuser/is_staff
        # flags (auth_user is shared by all tenants). Platform superusers keep
        # their flag untouched.

        new_pass = self.cleaned_data.get('new_password')
        if new_pass:
            user.set_password(new_pass)

        if commit:
            user.save()
            Employee.objects.update_or_create(
                user=user,
                defaults={
                    'branch': self.cleaned_data.get('branch'),
                    'role': self.cleaned_data.get('role'),
                    'base_salary': self.cleaned_data.get('base_salary') or 0,
                    'hourly_rate': self.cleaned_data.get('hourly_rate') or 0,
                    'shift_start_time': self.cleaned_data.get('shift_start_time'),
                    'shift_end_time': self.cleaned_data.get('shift_end_time'),
                    'deduction_per_hour': self.cleaned_data.get('deduction_per_hour') or 0,
                    'overtime_per_hour': self.cleaned_data.get('overtime_per_hour') or 0,
                }
            )
            if role == Role.ADMIN:
                user.user_permissions.set(Permission.objects.all())
            else:
                user.user_permissions.set(self.cleaned_data.get('user_permissions', []))
        return user



from django.contrib.auth.forms import AuthenticationForm


class TenantAuthenticationForm(AuthenticationForm):
    """
    Login form for tenant sites. auth_user is shared by all tenants, so valid
    credentials alone are not enough: the user must have an Employee profile in
    the current tenant schema (platform superusers excepted). The same generic
    "invalid login" error is shown to avoid account enumeration.

    Usernames created inside a tenant are stored as ``<schema>__<entered>``;
    the user types only ``<entered>``. If no such namespaced user exists, the
    raw name is tried (legacy users created before namespacing).

    ``request`` is always passed to authenticate() (required by django-axes).
    """

    def clean(self):
        from django.contrib.auth import authenticate
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if username is not None and password:
            username = username.strip()
            login_name = username
            namespaced = namespace_username(username)
            if namespaced != username and User.objects.filter(username=namespaced).exists():
                login_name = namespaced
            # Exactly one authenticate() call per attempt so django-axes counts
            # one failure per attempt.
            self.user_cache = authenticate(self.request, username=login_name, password=password)
            if self.user_cache is None:
                raise self.get_invalid_login_error()
            self.confirm_login_allowed(self.user_cache)

        return self.cleaned_data

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        from apps.tenant.core.access import is_tenant_member
        if not is_tenant_member(user):
            raise forms.ValidationError(
                self.error_messages['invalid_login'],
                code='invalid_login',
                params={'username': self.username_field.verbose_name},
            )
