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
        fields = ['name', 'eta_code']

from .models import Product, ProductUoM
from django.forms import inlineformset_factory

class ProductForm(BaseGlassForm):
    class Meta:
        model = Product
        fields = [
            'name', 'name_en', 'product_type', 'has_imei', 'has_serial', 'barcode', 'sku', 'egs_code', 'gpc_code',
            'category', 'sale_price', 'min_sale_price', 'tax_rate', 'withholding_tax_rate',
            'min_stock_level', 'max_stock_level', 'is_active',
            'allow_negative_stock', 'has_expiry', 'eta_tax_subtype', 'description', 'image'
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'barcode' in self.fields:
            self.fields['barcode'].required = True
        if 'egs_code' in self.fields:
            self.fields['egs_code'].help_text = ('اختياري: اتركه فارغاً — يُرسل الصنف بكود EGS لفئته. '
                                                 'أكواد الفئات من: منظومة الضرائب ← تسجيل أكواد الأصناف.')
        if 'image' in self.fields:
            self.fields['image'].widget.attrs['accept'] = 'image/*'
            self.fields['image'].help_text = 'تُحوَّل الصورة تلقائياً إلى WebP خفيف (أقصى بُعد 800px).'

    IMAGE_MAX_BYTES = 15 * 1024 * 1024

    def clean_barcode(self):
        """The main barcode must not be another product's / unit's / extra barcode."""
        code = (self.cleaned_data.get('barcode') or '').strip() or None
        if code:
            from .barcodes import validate_main_barcode
            from django.core.exceptions import ValidationError as DjangoValidationError
            try:
                validate_main_barcode(code, product_id=self.instance.pk)
            except DjangoValidationError as e:
                raise forms.ValidationError(e.messages)
        return code

    def clean_image(self):
        from django.core.files.uploadedfile import UploadedFile
        img = self.cleaned_data.get('image')
        if img and isinstance(img, UploadedFile) and img.size > self.IMAGE_MAX_BYTES:
            raise forms.ValidationError('حجم الصورة يجب ألا يتجاوز 15 ميجابايت.')
        return img

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
