from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context
from apps.public.tenants.models import Tenant
from apps.tenant.accounting.models import Account

class Command(BaseCommand):
    help = 'Sets up the default chart of accounts for a tenant'

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
                self.stdout.write(self.style.SUCCESS(f'Setting up Chart of Accounts for {tenant.schema_name}'))
                self._setup_accounts()
                
    def _setup_accounts(self):
        # 1000 ASSETS
        assets, _ = Account.objects.get_or_create(code='1000', defaults={'name': 'أصول', 'account_type': Account.ASSET})
        curr_assets, _ = Account.objects.get_or_create(code='1100', defaults={'name': 'أصول متداولة', 'account_type': Account.ASSET, 'parent': assets})
        Account.objects.get_or_create(code='1110', defaults={'name': 'النقدية والخزينة', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
        Account.objects.get_or_create(code='1120', defaults={'name': 'البنوك', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
        Account.objects.get_or_create(code='1130', defaults={'name': 'ذمم مدينة - عملاء', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
        Account.objects.get_or_create(code='1140', defaults={'name': 'المخزون', 'account_type': Account.ASSET, 'parent': curr_assets})
        Account.objects.get_or_create(code='1150', defaults={'name': 'عهد نقدية ومصروفات مقدمة', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
        Account.objects.get_or_create(code='1160', defaults={'name': 'المحافظ الإلكترونية', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
        fix_assets, _ = Account.objects.get_or_create(code='1200', defaults={'name': 'أصول ثابتة', 'account_type': Account.ASSET, 'parent': assets})
        Account.objects.get_or_create(code='1210', defaults={'name': 'الأصول الثابتة', 'account_type': Account.ASSET, 'parent': fix_assets})
        Account.objects.get_or_create(code='1220', defaults={'name': 'مجمع الإهلاك', 'account_type': Account.ASSET, 'parent': fix_assets})

        # 2000 LIABILITIES
        liab, _ = Account.objects.get_or_create(code='2000', defaults={'name': 'خصوم', 'account_type': Account.LIABILITY})
        curr_liab, _ = Account.objects.get_or_create(code='2100', defaults={'name': 'خصوم متداولة', 'account_type': Account.LIABILITY, 'parent': liab})
        Account.objects.get_or_create(code='2110', defaults={'name': 'ذمم دائنة - موردون', 'account_type': Account.LIABILITY, 'parent': curr_liab, 'allow_reconcile': True})
        Account.objects.get_or_create(code='2120', defaults={'name': 'ضريبة الخصم والإضافة', 'account_type': Account.LIABILITY, 'parent': curr_liab})
        Account.objects.get_or_create(code='2130', defaults={'name': 'ضريبة القيمة المضافة', 'account_type': Account.LIABILITY, 'parent': curr_liab})
        Account.objects.get_or_create(code='2140', defaults={'name': 'مصروفات مستحقة الدفع', 'account_type': Account.LIABILITY, 'parent': curr_liab})
        Account.objects.get_or_create(code='2150', defaults={'name': 'أرصدة دائنة أخرى', 'account_type': Account.LIABILITY, 'parent': curr_liab})

        # 3000 EQUITY
        equity, _ = Account.objects.get_or_create(code='3000', defaults={'name': 'حقوق الملكية', 'account_type': Account.EQUITY})
        Account.objects.get_or_create(code='3100', defaults={'name': 'رأس المال', 'account_type': Account.EQUITY, 'parent': equity})
        Account.objects.get_or_create(code='3200', defaults={'name': 'أرباح محتجزة', 'account_type': Account.EQUITY, 'parent': equity})
        Account.objects.get_or_create(code='3300', defaults={'name': 'ملخص الدخل', 'account_type': Account.EQUITY, 'parent': equity})
        Account.objects.get_or_create(code='3400', defaults={'name': 'جاري الشركاء / مسحوبات', 'account_type': Account.EQUITY, 'parent': equity})

        # 4000 REVENUE
        rev, _ = Account.objects.get_or_create(code='4000', defaults={'name': 'إيرادات', 'account_type': Account.REVENUE})
        Account.objects.get_or_create(code='4100', defaults={'name': 'المبيعات', 'account_type': Account.REVENUE, 'parent': rev})
        Account.objects.get_or_create(code='4110', defaults={'name': 'إيرادات المصنعيات والخدمات', 'account_type': Account.REVENUE, 'parent': rev})
        Account.objects.get_or_create(code='4120', defaults={'name': 'مردودات ومسموحات المبيعات', 'account_type': Account.REVENUE, 'parent': rev})
        Account.objects.get_or_create(code='4130', defaults={'name': 'خصم مسموح به', 'account_type': Account.REVENUE, 'parent': rev})
        Account.objects.get_or_create(code='4200', defaults={'name': 'إيرادات أخرى', 'account_type': Account.REVENUE, 'parent': rev})
        Account.objects.get_or_create(code='4210', defaults={'name': 'خصم مكتسب (موردين)', 'account_type': Account.REVENUE, 'parent': rev})

        # 5000 EXPENSE
        exp, _ = Account.objects.get_or_create(code='5000', defaults={'name': 'مصروفات', 'account_type': Account.EXPENSE})
        Account.objects.get_or_create(code='5100', defaults={'name': 'تكلفة البضاعة المباعة', 'account_type': Account.EXPENSE, 'parent': exp})
        Account.objects.get_or_create(code='5110', defaults={'name': 'مردودات ومسموحات المشتريات', 'account_type': Account.EXPENSE, 'parent': exp})
        Account.objects.get_or_create(code='5200', defaults={'name': 'مصاريف تشغيلية', 'account_type': Account.EXPENSE, 'parent': exp})
        Account.objects.get_or_create(code='5210', defaults={'name': 'رواتب وأجور', 'account_type': Account.EXPENSE, 'parent': exp})
        Account.objects.get_or_create(code='5220', defaults={'name': 'مصروف الإهلاك', 'account_type': Account.EXPENSE, 'parent': exp})
        Account.objects.get_or_create(code='5300', defaults={'name': 'مصاريف إدارية', 'account_type': Account.EXPENSE, 'parent': exp})
        Account.objects.get_or_create(code='5400', defaults={'name': 'عجز وزيادة الخزينة (الورديات)', 'account_type': Account.EXPENSE, 'parent': exp})
        
        Account.objects.rebuild()
