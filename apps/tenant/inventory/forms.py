from django import forms
from .models import UnitOfMeasure

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

class UnitOfMeasureForm(BaseGlassForm):
    class Meta:
        model = UnitOfMeasure
        fields = ['name']

from .models import Product, ProductUoM
from django.forms import inlineformset_factory

class ProductForm(BaseGlassForm):
    class Meta:
        model = Product
        fields = [
            'name', 'name_en', 'product_type', 'has_imei', 'has_serial', 'barcode', 'sku', 'gs1_code', 'egs_code',
            'category', 'sale_price', 'min_sale_price', 'tax_rate', 'withholding_tax_rate',
            'min_stock_level', 'max_stock_level', 'is_active',
            'allow_negative_stock', 'description', 'image'
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'barcode' in self.fields:
            self.fields['barcode'].required = True

class ProductUoMForm(BaseGlassForm):
    class Meta:
        model = ProductUoM
        fields = ['uom', 'conversion_factor', 'barcode', 'is_base']

ProductUoMFormSet = inlineformset_factory(
    Product,
    ProductUoM,
    form=ProductUoMForm,
    fields=['uom', 'conversion_factor', 'barcode', 'is_base'],
    extra=1,
    can_delete=True
)
