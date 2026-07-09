import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from django.contrib.auth.models import User
from apps.public.tenants.models import Tenant
from django_tenants.utils import schema_context

t = Tenant.objects.get(schema_name='test')
with schema_context(t.schema_name):
    # Get all users and their roles/branches
    for u in User.objects.all():
        try:
            e = u.employee_profile
            print(f"User: {u.username}, Role: {e.role}, Branch: {e.branch.name if e.branch else 'None'}")
        except Exception as ex:
            print(f"User: {u.username}, No profile or error: {ex}")
