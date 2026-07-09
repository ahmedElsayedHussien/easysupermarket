import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from django_tenants.utils import schema_context
from apps.public.tenants.models import Tenant
from apps.tenant.accounting.models import Treasury

for tenant in Tenant.objects.exclude(schema_name='public'):
    with schema_context(tenant.schema_name):
        for treasury in Treasury.objects.all():
            treasury.save()
