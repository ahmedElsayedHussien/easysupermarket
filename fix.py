import re

def fix(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    content = re.sub(r"verbose_name = '(.*?)'", r"verbose_name = _('\1')", content)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

fix('e:/easysupermarket/apps/tenant/inventory/models.py')
print("Fixed.")
