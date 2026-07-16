import os
import django
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from django.test import Client
from django.contrib.auth.models import User

client = Client()

# Create a test user with the required permissions or try without permissions
try:
    user = User.objects.get(username='admin')
except User.DoesNotExist:
    user = User.objects.create_superuser('admin', 'admin@example.com', 'admin')

client.force_login(user)

try:
    response = client.get('/reports/sales/debts/')
    print("STATUS:", response.status_code)
    if response.status_code == 200:
        print("SUCCESS")
    else:
        print(response.content.decode('utf-8'))
except Exception as e:
    import traceback
    traceback.print_exc()
