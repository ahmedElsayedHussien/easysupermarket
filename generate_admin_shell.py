import os
from django.apps import apps
from django.conf import settings

app_paths = {
    'tenants': r'e:\easysupermarket\apps\public\tenants\admin.py',
    'subscriptions': r'e:\easysupermarket\apps\public\subscriptions\admin.py',
    'core': r'e:\easysupermarket\apps\tenant\core\admin.py',
    'accounting': r'e:\easysupermarket\apps\tenant\accounting\admin.py',
    'inventory': r'e:\easysupermarket\apps\tenant\inventory\admin.py',
    'invoicing': r'e:\easysupermarket\apps\tenant\invoicing\admin.py',
    'partners': r'e:\easysupermarket\apps\tenant\partners\admin.py',
}

for app_name, path in app_paths.items():
    try:
        app_config = apps.get_app_config(app_name)
        models = app_config.get_models()
        
        with open(path, 'w', encoding='utf-8') as f:
            f.write("from django.contrib import admin\n")
            f.write("from .models import *\n\n")
            
            for model in models:
                model_name = model.__name__
                f.write(f"@admin.register({model_name})\n")
                f.write(f"class {model_name}Admin(admin.ModelAdmin):\n")
                fields = [field.name for field in model._meta.fields if field.name not in ('password',)]
                fields_str = ', '.join([f"'{f}'" for f in fields[:5]])
                if fields_str:
                    f.write(f"    list_display = ({fields_str},)\n\n")
                else:
                    f.write(f"    pass\n\n")
        print(f"Created {path}")
    except Exception as e:
        print(f"Error for {app_name}: {e}")
