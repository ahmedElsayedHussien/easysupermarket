from decimal import Decimal

from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import MinValueValidator, MaxValueValidator
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

# Weekday numbers follow python's date.weekday() (Mon=0 .. Sun=6); listed Saturday-first (Egypt).
WEEKDAY_CHOICES = [
    ('5', 'السبت'), ('6', 'الأحد'), ('0', 'الاثنين'), ('1', 'الثلاثاء'),
    ('2', 'الأربعاء'), ('3', 'الخميس'), ('4', 'الجمعة'),
]

LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_ALLOWED_TYPES = {'image/png', 'image/jpeg', 'image/webp'}
LOGO_ALLOWED_EXTS = ('.png', '.jpg', '.jpeg', '.webp')

_CHECK = {'class': 'form-check-input'}
_SELECT = {'class': 'form-select'}


def _num(**attrs):
    return forms.NumberInput(attrs={'class': 'form-control', **attrs})


def parse_weekday_csv(raw):
    """'4,5' -> ['4', '5'] (ignores junk / out-of-range values)."""
    out = []
    for part in str(raw or '').replace('،', ',').split(','):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6 and part not in out:
            out.append(part)
    return out


class SystemSettingForm(forms.ModelForm):
    # (min, max) enforced server side and mirrored on the <input min/max>.
    RANGES = {
        'low_stock_threshold': (0, 1_000_000),
        'stagnant_stock_days': (1, 3650),
        'expiry_warning_days': (1, 3650),
        'dormant_debt_days': (1, 3650),
        'credit_limit_warning_percent': (1, 100),
        'late_grace_minutes': (0, 240),
        'session_timeout_hours': (1, 720),
        'login_max_attempts': (3, 50),
        'login_lockout_minutes': (1, 1440),
        'auto_backup_retention': (1, 365),
        'pos_price_margin_percent': (0, 100),
        'rep_price_margin_percent': (0, 100),
        'rep_return_without_invoice_max': (0, None),
        'max_pos_discount_percentage': (0, 100),
        'large_amount_requires_customer': (0, None),
    }

    weekly_off_days = forms.MultipleChoiceField(
        choices=WEEKDAY_CHOICES, required=False,
        widget=forms.CheckboxSelectMultiple(attrs=_CHECK),
        label='أيام الإجازة الأسبوعية',
        help_text='تُستخدم في حساب الحضور والغياب والمرتبات. يمكن اختيار أكثر من يوم أو عدم اختيار أي يوم.',
    )

    class Meta:
        model = SystemSetting
        # the lock date moves only through accounting.periods (checks + history), never as a plain field
        exclude = ['accounting_lock_date']
        widgets = {
            'store_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'اسم المؤسسة / المتجر'}),
            'store_phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'مثال: 0225000000', 'dir': 'ltr'}),
            'store_address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'عنوان المحل'}),
            'tax_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'الرقم الضريبي (إن وجد)'}),
            'commercial_register': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'السجل التجاري (إن وجد)'}),
            'receipt_footer': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'مثال: البضاعة المباعة لا ترد ولا تستبدل إلا خلال 14 يوم...'}),
            'company_logo': forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'image/png,image/jpeg,image/webp'}),
            'receipt_paper_width': forms.Select(attrs=_SELECT),
            'show_branch_info_on_receipt': forms.CheckboxInput(attrs=_CHECK),
            'large_amount_requires_customer': _num(step='any'),
            'low_stock_threshold': _num(),
            'allow_negative_stock': forms.CheckboxInput(attrs=_CHECK),
            'auto_print_receipt': forms.CheckboxInput(attrs=_CHECK),
            'allow_pos_price_modification': forms.CheckboxInput(attrs=_CHECK),
            'show_pos_cost': forms.CheckboxInput(attrs=_CHECK),
            'pos_price_margin_percent': _num(step='0.01'),
            'rep_allow_price_edit': forms.CheckboxInput(attrs=_CHECK),
            'rep_price_margin_percent': _num(step='0.01'),
            'rep_allow_return_without_invoice': forms.CheckboxInput(attrs=_CHECK),
            'rep_allow_cancel_invoice': forms.CheckboxInput(attrs=_CHECK),
            'rep_return_without_invoice_max': _num(step='0.01'),
            'max_pos_discount_percentage': _num(step='0.01'),
            'apply_vat_by_default': forms.CheckboxInput(attrs=_CHECK),
            'pos_cash_rounding': forms.Select(attrs=_SELECT),
            'default_pos_treasury': forms.Select(attrs=_SELECT),
            'default_pos_bank_account': forms.Select(attrs=_SELECT),
            'default_pos_instapay_account': forms.Select(attrs=_SELECT),
            'default_pos_ewallet': forms.Select(attrs=_SELECT),
            'lock_pos_payment_accounts': forms.CheckboxInput(attrs=_CHECK),
            'enable_expiry_tracking': forms.CheckboxInput(attrs=_CHECK),
            'stagnant_stock_days': _num(),
            'expiry_warning_days': _num(),
            'auto_post_journals': forms.CheckboxInput(attrs=_CHECK),
            'accounting_lock_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}, format='%Y-%m-%d'),
            'dormant_debt_days': _num(),
            'credit_limit_warning_percent': _num(),
            'payroll_day_basis': forms.Select(attrs=_SELECT),
            'late_grace_minutes': _num(),
            'session_timeout_hours': _num(),
            'login_max_attempts': _num(),
            'login_lockout_minutes': _num(),
            'auto_backup_enabled': forms.CheckboxInput(attrs=_CHECK),
            'auto_backup_retention': _num(),
        }
        help_texts = {
            'low_stock_threshold': 'حد طلب افتراضي للأصناف التي ليس لها حد خاص',
            'store_name': 'يظهر على الفواتير وعروض الأسعار وكشوف الحساب والتقارير المطبوعة وملفات Excel.',
            'company_logo': 'صورة PNG أو JPG أو WEBP بحجم أقصى 2 ميجابايت.',
            'session_timeout_hours': 'يتم تسجيل خروج المستخدم تلقائياً بعد هذه المدة من آخر دخول.',
            'auto_backup_retention': 'عند تجاوز هذا العدد تُحذف أقدم النسخ التلقائية.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        radio_choices = [(True, ''), (False, '')]
        if 'store_name' in self.fields:
            self.fields['store_name'].label = 'اسم المؤسسة / المتجر'
        for field_name, field in self.fields.items():
            if field_name.startswith('show_nav_'):
                field.widget = forms.RadioSelect(choices=radio_choices)

        for name, (lo, hi) in self.RANGES.items():
            field = self.fields.get(name)
            if field is None:
                continue
            if lo is not None:
                field.validators.append(MinValueValidator(lo))
                field.widget.attrs['min'] = lo
            if hi is not None:
                field.validators.append(MaxValueValidator(hi))
                field.widget.attrs['max'] = hi

        if not self.is_bound:
            # CSV in the model <-> list of checkbox values in the form
            self.initial['weekly_off_days'] = parse_weekday_csv(getattr(self.instance, 'weekly_off_days', ''))
            # Decimal('0') must render as the '0.00' option to be pre-selected.
            rounding = self.initial.get('pos_cash_rounding')
            if rounding is not None:
                self.initial['pos_cash_rounding'] = Decimal(str(rounding)).quantize(Decimal('0.01'))

        treasury = self.fields.get('default_pos_treasury')
        if treasury is not None:
            treasury.queryset = treasury.queryset.select_related('branch').order_by('branch__name', 'name')
            treasury.label_from_instance = lambda t: f'{t.name} — {t.branch.name}' if t.branch_id else t.name

    def clean_weekly_off_days(self):
        days = sorted({int(d) for d in self.cleaned_data.get('weekly_off_days') or []})
        if len(days) == 7:
            raise forms.ValidationError('لا يمكن جعل كل أيام الأسبوع إجازة.')
        return ','.join(str(d) for d in days)

    def clean_company_logo(self):
        logo = self.cleaned_data.get('company_logo')
        if logo and isinstance(logo, UploadedFile):
            if logo.size > LOGO_MAX_BYTES:
                raise forms.ValidationError('حجم الشعار يجب ألا يتجاوز 2 ميجابايت.')
            content_type = getattr(logo, 'content_type', '') or ''
            if content_type not in LOGO_ALLOWED_TYPES or not logo.name.lower().endswith(LOGO_ALLOWED_EXTS):
                raise forms.ValidationError('صيغة الشعار غير مدعومة. المسموح: PNG أو JPG أو WEBP.')
        return logo


class BranchForm(forms.ModelForm):
    """Branch create / update, incl. the per-branch POS payment defaults."""
    POS_FIELDS = ('default_pos_treasury', 'default_pos_bank_account',
                  'default_pos_instapay_account', 'default_pos_ewallet')
    ETA_FIELDS = ('eta_branch_code', 'eta_governate', 'eta_region_city', 'eta_street', 'eta_building_number')

    class Meta:
        model = Branch
        fields = ['name', 'code', 'address', 'phone', 'is_active',
                  'default_pos_treasury', 'default_pos_bank_account',
                  'default_pos_instapay_account', 'default_pos_ewallet',
                  'eta_branch_code', 'eta_governate', 'eta_region_city', 'eta_street', 'eta_building_number']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'code': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'is_active': forms.CheckboxInput(attrs=_CHECK),
            'default_pos_treasury': forms.Select(attrs=_SELECT),
            'default_pos_bank_account': forms.Select(attrs=_SELECT),
            'default_pos_instapay_account': forms.Select(attrs=_SELECT),
            'default_pos_ewallet': forms.Select(attrs=_SELECT),
            'eta_branch_code': forms.TextInput(attrs={'class': 'form-control'}),
            'eta_governate': forms.TextInput(attrs={'class': 'form-control'}),
            'eta_region_city': forms.TextInput(attrs={'class': 'form-control'}),
            'eta_street': forms.TextInput(attrs={'class': 'form-control'}),
            'eta_building_number': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.tenant.accounting.models import Treasury
        treasury = self.fields['default_pos_treasury']
        if self.instance.pk:
            treasury.queryset = Treasury.objects.business().filter(branch=self.instance).order_by('name')
            treasury.help_text = 'خزائن هذا الفرع فقط. اتركه فارغاً لاستخدام الافتراضي العام.'
        else:
            treasury.queryset = Treasury.objects.none()
            treasury.disabled = True
            treasury.help_text = 'احفظ الفرع أولاً ثم أضف خزينته'
        for name in self.POS_FIELDS[1:]:
            self.fields[name].help_text = 'اتركه فارغاً لاستخدام الافتراضي العام من إعدادات النظام.'

    @property
    def general_fields(self):
        return [self[n] for n in self.Meta.fields if n not in self.POS_FIELDS and n not in self.ETA_FIELDS]

    @property
    def eta_fields(self):
        return [self[n] for n in self.ETA_FIELDS]

    def clean(self):
        data = super().clean()
        if (data.get('eta_branch_code') or '').strip():
            missing = [self.fields[f].label for f in self.ETA_FIELDS[1:] if not (data.get(f) or '').strip()]
            if missing:
                raise forms.ValidationError('الفرع ليه كود عند الضرائب — لازم عنوانه المسجل كامل: ' + '، '.join(missing))
        return data

    @property
    def pos_fields(self):
        return [self[n] for n in self.POS_FIELDS]

    def clean_default_pos_treasury(self):
        treasury = self.cleaned_data.get('default_pos_treasury')
        if treasury is not None and (not self.instance.pk or treasury.branch_id != self.instance.pk):
            raise forms.ValidationError('الخزينة المختارة لا تتبع هذا الفرع.')
        return treasury



def catalog_permission_queryset():
    """Only the permissions of core.permission_catalog can be given from the user screen."""
    from .permission_catalog import catalog_permissions
    return Permission.objects.filter(id__in=[p.id for p in catalog_permissions().values()])


def save_user_permissions(user, role, selected):
    """ADMIN: every permission. Others: the ticked catalog permissions; permissions
    outside the catalog (not shown on the screen) are left as they were."""
    if role == Role.ADMIN:
        user.user_permissions.set(Permission.objects.all())
        return
    catalog_ids = set(catalog_permission_queryset().values_list('id', flat=True))
    keep = [pid for pid in user.user_permissions.values_list('id', flat=True) if pid not in catalog_ids]
    user.user_permissions.set(keep + [p.id for p in selected])


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
        queryset=Permission.objects.none(),   # the permission catalog, set in __init__
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='الصلاحيات المخصصة'
    )
    
    # HR Fields
    base_salary = forms.DecimalField(max_digits=10, decimal_places=2, initial=Decimal('0.00'), required=False, label='الراتب الأساسي')
    hourly_rate = forms.DecimalField(max_digits=8, decimal_places=2, initial=Decimal('0.00'), required=False, label='قيمة ساعة العمل')
    shift_start_time = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), required=False, label='ميعاد الحضور')
    shift_end_time = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), required=False, label='ميعاد الانصراف')
    deduction_per_hour = forms.DecimalField(max_digits=8, decimal_places=2, initial=Decimal('0.00'), required=False, label='قيمة الخصم للساعة')
    overtime_per_hour = forms.DecimalField(max_digits=8, decimal_places=2, initial=Decimal('0.00'), required=False, label='قيمة الإضافي للساعة')
    is_insured = forms.BooleanField(required=False, initial=True, label='مؤمَّن عليه (تأمينات اجتماعية)')
    insurance_wage = forms.DecimalField(max_digits=10, decimal_places=2, required=False, label='الأجر التأميني (فاضي = الأساسي)')
    tax_exempt = forms.BooleanField(required=False, label='معفى من ضريبة كسب العمل')

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'last_name', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'username' in self.fields:
            self.fields['username'].label = 'اسم المستخدم (Username)'
            self.fields['username'].help_text = 'اسم تسجيل الدخول للنظام (حروف إنجليزية أو أرقام).'
        self.fields['branch'].help_text = NO_BRANCH_HELP
        self.fields['user_permissions'].queryset = catalog_permission_queryset()

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
                    'is_insured': bool(self.cleaned_data.get('is_insured')),
                    'insurance_wage': self.cleaned_data.get('insurance_wage') or None,
                    'tax_exempt': bool(self.cleaned_data.get('tax_exempt')),
                }
            )
            save_user_permissions(user, role, self.cleaned_data.get('user_permissions', []))
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
        queryset=Permission.objects.none(),   # the permission catalog, set in __init__
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='الصلاحيات المخصصة'
    )

    # HR Fields
    base_salary = forms.DecimalField(max_digits=10, decimal_places=2, initial=Decimal('0.00'), required=False, label='الراتب الأساسي')
    hourly_rate = forms.DecimalField(max_digits=8, decimal_places=2, initial=Decimal('0.00'), required=False, label='قيمة ساعة العمل')
    shift_start_time = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), required=False, label='ميعاد الحضور')
    shift_end_time = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), required=False, label='ميعاد الانصراف')
    deduction_per_hour = forms.DecimalField(max_digits=8, decimal_places=2, initial=Decimal('0.00'), required=False, label='قيمة الخصم للساعة')
    overtime_per_hour = forms.DecimalField(max_digits=8, decimal_places=2, initial=Decimal('0.00'), required=False, label='قيمة الإضافي للساعة')
    is_insured = forms.BooleanField(required=False, initial=True, label='مؤمَّن عليه (تأمينات اجتماعية)')
    insurance_wage = forms.DecimalField(max_digits=10, decimal_places=2, required=False, label='الأجر التأميني (فاضي = الأساسي)')
    tax_exempt = forms.BooleanField(required=False, label='معفى من ضريبة كسب العمل')

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'is_active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['branch'].help_text = NO_BRANCH_HELP
        self.fields['user_permissions'].queryset = catalog_permission_queryset()
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
                self.fields['is_insured'].initial = employee.is_insured
                self.fields['insurance_wage'].initial = employee.insurance_wage
                self.fields['tax_exempt'].initial = employee.tax_exempt
            except Employee.DoesNotExist:
                pass
            self.fields['user_permissions'].initial = list(self.instance.user_permissions.values_list('id', flat=True))

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
                    'is_insured': bool(self.cleaned_data.get('is_insured')),
                    'insurance_wage': self.cleaned_data.get('insurance_wage') or None,
                    'tax_exempt': bool(self.cleaned_data.get('tax_exempt')),
                }
            )
            save_user_permissions(user, role, self.cleaned_data.get('user_permissions', []))
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
