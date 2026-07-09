import re

def update_inventory_models(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    if 'from django.utils.translation import gettext_lazy as _' not in content:
        content = content.replace('from django.db import models\n', 'from django.db import models\nfrom django.utils.translation import gettext_lazy as _\n')

    # Replace verbose_name='something' with verbose_name=_('something')
    content = re.sub(r"verbose_name='(.*?)'", r"verbose_name=_('\1')", content)
    # Replace verbose_name_plural = 'something' with verbose_name_plural = _('something')
    content = re.sub(r"verbose_name_plural = '(.*?)'", r"verbose_name_plural = _('\1')", content)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

update_inventory_models('e:/easysupermarket/apps/tenant/inventory/models.py')
print("Inventory models updated.")
