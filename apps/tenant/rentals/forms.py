from django import forms
from .models import Equipment, Rental
from apps.tenant.accounting.models import Treasury, EWallet

class EquipmentForm(forms.ModelForm):
    class Meta:
        model = Equipment
        fields = ['name', 'code', 'purchase_cost', 'daily_rate', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control bg-dark-glass text-white'}),
            'code': forms.TextInput(attrs={'class': 'form-control bg-dark-glass text-white'}),
            'purchase_cost': forms.NumberInput(attrs={'class': 'form-control bg-dark-glass text-white', 'step': '0.01'}),
            'daily_rate': forms.NumberInput(attrs={'class': 'form-control bg-dark-glass text-white', 'step': '0.01'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input bg-dark-glass'}),
        }

class RentalForm(forms.ModelForm):
    class Meta:
        model = Rental
        fields = ['equipment', 'customer', 'start_date', 'days_rented', 'payment_method', 'treasury', 'ewallet']
        widgets = {
            'equipment': forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'}),
            'customer': forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'}),
            'start_date': forms.DateInput(attrs={'class': 'form-control bg-dark-glass text-white', 'type': 'date'}),
            'days_rented': forms.NumberInput(attrs={'class': 'form-control bg-dark-glass text-white', 'min': '1'}),
            'payment_method': forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'}),
            'treasury': forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'}),
            'ewallet': forms.Select(attrs={'class': 'form-select bg-dark-glass text-white'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only show available equipment
        if not self.instance.pk:
            self.fields['equipment'].queryset = Equipment.objects.filter(status=Equipment.STATUS_AVAILABLE, is_active=True)
