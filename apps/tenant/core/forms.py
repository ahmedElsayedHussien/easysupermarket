from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.contrib.auth.models import Permission
from apps.tenant.core.models import Employee, Branch, Role
from django.db import transaction
from .models import SystemSetting

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
            'pos_price_margin_percent': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': '0.01'}),
            'apply_vat_by_default': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'enable_expiry_tracking': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'auto_post_journals': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class EmployeeUserCreationForm(UserCreationForm):
    first_name = forms.CharField(label='الاسم الأول', max_length=30, required=True)
    last_name = forms.CharField(label='الاسم الأخير', max_length=30, required=True)
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

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get('first_name')
        user.last_name = self.cleaned_data.get('last_name')
        user.email = self.cleaned_data.get('email')
        role = self.cleaned_data.get('role')
        if role == Role.ADMIN:
            user.is_superuser = True
            user.is_staff = True
        else:
            user.is_superuser = False
            user.is_staff = False
        
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
            user.user_permissions.set(self.cleaned_data.get('user_permissions', []))
        return user


class EmployeeUserUpdateForm(forms.ModelForm):
    first_name = forms.CharField(label='الاسم الأول', max_length=30, required=True)
    last_name = forms.CharField(label='الاسم الأخير', max_length=30, required=True)
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
        if self.instance and self.instance.pk:
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

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        role = self.cleaned_data.get('role')
        if role == Role.ADMIN:
            user.is_superuser = True
            user.is_staff = True
        else:
            user.is_superuser = False
            user.is_staff = False
            
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
            user.user_permissions.set(self.cleaned_data.get('user_permissions', []))
        return user
