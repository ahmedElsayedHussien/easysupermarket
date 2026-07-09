from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context
from apps.public.tenants.models import Tenant
from apps.tenant.core.models import Branch
from apps.tenant.inventory.models import Warehouse, Category, Product
from apps.tenant.partners.models import Partner
import random
from decimal import Decimal

class Command(BaseCommand):
    help = 'Creates demo data for a tenant'

    def add_arguments(self, parser):
        parser.add_argument('--schema', type=str, help='The schema name to run this for')

    def handle(self, *args, **options):
        schema_name = options.get('schema')
        
        if schema_name:
            tenants = Tenant.objects.filter(schema_name=schema_name)
        else:
            tenants = Tenant.objects.exclude(schema_name='public')
            
        for tenant in tenants:
            with schema_context(tenant.schema_name):
                self.stdout.write(self.style.SUCCESS(f'Creating demo data for {tenant.schema_name}'))
                self._create_demo_data()
                
    def _create_demo_data(self):
        # Create Branches
        for i in range(1, 4):
            branch, created = Branch.objects.get_or_create(
                code=f'BR{i:03d}', 
                defaults={'name': f'الفرع {i}', 'address': f'شارع {i} المعادي', 'phone': f'010000000{i}'}
            )
            # Create Warehouses per branch
            Warehouse.objects.get_or_create(
                code=f'WH{i}A', defaults={'name': f'مستودع رئيسي فرع {i}', 'branch': branch}
            )
            Warehouse.objects.get_or_create(
                code=f'WH{i}B', defaults={'name': f'ثلاجة فرع {i}', 'branch': branch}
            )

        # Create Categories
        cat_food, _ = Category.objects.get_or_create(name='مواد غذائية')
        cat_drink, _ = Category.objects.get_or_create(name='مشروبات')
        cat_clean, _ = Category.objects.get_or_create(name='منظفات')
        Category.objects.rebuild()

        # Create Products
        for i in range(1, 21):
            cat = random.choice([cat_food, cat_drink, cat_clean])
            Product.objects.get_or_create(
                barcode=f'1000000000{i:02d}',
                defaults={
                    'name': f'منتج تجريبي {i}',
                    'category': cat,
                    'unit': 'PIECE',
                    'sale_price': Decimal(str(random.randint(10, 100))),
                    'tax_rate': Decimal('14.00'),
                }
            )

        # Create Partners
        for i in range(1, 6):
            Partner.objects.get_or_create(
                name=f'مورد تجريبي {i}',
                defaults={'partner_type': 'SUPPLIER', 'phone': f'011000000{i}'}
            )
        for i in range(1, 11):
            Partner.objects.get_or_create(
                name=f'عميل تجريبي {i}',
                defaults={'partner_type': 'CUSTOMER', 'phone': f'012000000{i}'}
            )
