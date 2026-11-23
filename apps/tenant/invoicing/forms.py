from django import forms
from django.forms import inlineformset_factory
from .models import Quotation, QuotationLine

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


from decimal import Decimal

class QuotationForm(BaseGlassForm):
    class Meta:
        model = Quotation
        fields = [
            'partner', 'branch', 'warehouse', 'date', 'valid_until',
            'discount_percentage', 'vat_percentage', 'wht_percentage', 'notes'
        ]
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'valid_until': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['discount_percentage'].required = False
        self.fields['vat_percentage'].required = False
        self.fields['wht_percentage'].required = False
        self.fields['notes'].required = False
        self.fields['valid_until'].required = False

    def clean_discount_percentage(self):
        val = self.cleaned_data.get('discount_percentage')
        return val if val is not None else Decimal('0')

    def clean_vat_percentage(self):
        val = self.cleaned_data.get('vat_percentage')
        return val if val is not None else Decimal('0')

    def clean_wht_percentage(self):
        val = self.cleaned_data.get('wht_percentage')
        return val if val is not None else Decimal('0')


class QuotationLineForm(BaseGlassForm):
    class Meta:
        model = QuotationLine
        fields = [
            'product', 'uom', 'quantity', 'unit_price', 'discount_pct', 'tax_rate', 'wht_rate'
        ]
        widgets = {
            'quantity': forms.NumberInput(attrs={'step': '0.01', 'min': '0.01'}),
            'unit_price': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'discount_pct': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'max': '100'}),
            'tax_rate': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'wht_rate': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['uom'].required = False
        self.fields['uom'].empty_label = 'الوحدة الأساسية (افتراضية)'
        self.fields['discount_pct'].required = False
        self.fields['tax_rate'].required = False
        self.fields['wht_rate'].required = False
        self.fields['product'].widget.attrs.update({'class': 'form-select bg-dark-glass text-white product-select'})
        self.fields['uom'].widget.attrs.update({'class': 'form-select bg-dark-glass text-white uom-select'})
        self.fields['quantity'].widget.attrs.update({'class': 'form-control bg-dark-glass text-white qty-input'})
        self.fields['unit_price'].widget.attrs.update({'class': 'form-control bg-dark-glass text-white price-input'})
        self.fields['discount_pct'].widget.attrs.update({'class': 'form-control bg-dark-glass text-white disc-input'})
        self.fields['tax_rate'].widget.attrs.update({'class': 'form-control bg-dark-glass text-white tax-input'})
        self.fields['wht_rate'].widget.attrs.update({'class': 'form-control bg-dark-glass text-white wht-input'})

    def clean_discount_pct(self):
        val = self.cleaned_data.get('discount_pct')
        return val if val is not None else Decimal('0')

    def clean_tax_rate(self):
        val = self.cleaned_data.get('tax_rate')
        return val if val is not None else Decimal('0')

    def clean_wht_rate(self):
        val = self.cleaned_data.get('wht_rate')
        return val if val is not None else Decimal('0')

    def clean(self):
        cleaned_data = super().clean()
        product = cleaned_data.get('product')
        uom = cleaned_data.get('uom')
        if uom and product and uom.product_id != product.id:
            cleaned_data['uom'] = None
        return cleaned_data


QuotationLineFormSet = inlineformset_factory(
    Quotation,
    QuotationLine,
    form=QuotationLineForm,
    extra=1,
    can_delete=True
)
