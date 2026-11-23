from django.core.management.base import BaseCommand
from django.conf import settings
from apps.tenant.accounting.models import Account


def setup_chart_of_accounts_data():
    """
    Core function to populate standard Chart of Accounts.
    Safe to execute within any active database / tenant schema or standalone SQLite.
    """
    # 1000 ASSETS
    assets, _ = Account.objects.get_or_create(code='1000', defaults={'name': 'أصول', 'account_type': Account.ASSET})
    curr_assets, _ = Account.objects.get_or_create(code='1100', defaults={'name': 'أصول متداولة', 'account_type': Account.ASSET, 'parent': assets})
    Account.objects.get_or_create(code='1110', defaults={'name': 'النقدية والخزينة', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1120', defaults={'name': 'البنوك', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1130', defaults={'name': 'ذمم مدينة - عملاء', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1135', defaults={'name': 'ذمم الموظفين والعهد', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1140', defaults={'name': 'المخزون', 'account_type': Account.ASSET, 'parent': curr_assets})
    Account.objects.get_or_create(code='1145', defaults={'name': 'دفعات مقدمة للموردين', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1150', defaults={'name': 'عهد نقدية', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1152', defaults={'name': 'مصروفات مدفوعة مقدماً', 'account_type': Account.ASSET, 'parent': curr_assets})
    Account.objects.get_or_create(code='1155', defaults={'name': 'ضريبة خصم من المنبع لنا (مدينة)', 'account_type': Account.ASSET, 'parent': curr_assets})
    Account.objects.get_or_create(code='1160', defaults={'name': 'المحافظ الإلكترونية', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1170', defaults={'name': 'ماكينات الدفع الإلكتروني (POS)', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1180', defaults={'name': 'أوراق قبض - حافظة الشيكات', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1185', defaults={'name': 'شيكات برسم التحصيل', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    Account.objects.get_or_create(code='1190', defaults={'name': 'ضريبة القيمة المضافة - مدخلات (مشتريات)', 'account_type': Account.ASSET, 'parent': curr_assets})
    # Inter-branch transfers: cash sent by one branch until the other confirms receipt
    Account.objects.get_or_create(code='1195', defaults={'name': 'نقدية بالطريق بين الفروع', 'account_type': Account.ASSET, 'parent': curr_assets, 'allow_reconcile': True})
    
    fix_assets, _ = Account.objects.get_or_create(code='1200', defaults={'name': 'أصول ثابتة', 'account_type': Account.ASSET, 'parent': assets})
    Account.objects.get_or_create(code='1210', defaults={'name': 'الأصول الثابتة والآلات', 'account_type': Account.ASSET, 'parent': fix_assets})
    # Rental equipment. Legacy tenants have it as '1110-EQ' wrongly placed under cash (1110): move it.
    if not Account.objects.filter(code='1110-EQ').exists():
        Account.objects.get_or_create(code='1215', defaults={'name': 'معدات التأجير', 'account_type': Account.ASSET, 'parent': fix_assets})
    Account.objects.filter(code='1110-EQ').exclude(parent=fix_assets).update(parent=fix_assets)
    Account.objects.get_or_create(code='1220', defaults={'name': 'مجمع الإهلاك', 'account_type': Account.ASSET, 'parent': fix_assets, 'is_contra': True})

    # 2000 LIABILITIES
    liab, _ = Account.objects.get_or_create(code='2000', defaults={'name': 'خصوم', 'account_type': Account.LIABILITY})
    curr_liab, _ = Account.objects.get_or_create(code='2100', defaults={'name': 'خصوم متداولة', 'account_type': Account.LIABILITY, 'parent': liab})
    Account.objects.get_or_create(code='2110', defaults={'name': 'ذمم دائنة - موردون', 'account_type': Account.LIABILITY, 'parent': curr_liab, 'allow_reconcile': True})
    Account.objects.get_or_create(code='2120', defaults={'name': 'ضريبة الخصم والإضافة', 'account_type': Account.LIABILITY, 'parent': curr_liab})
    Account.objects.get_or_create(code='2130', defaults={'name': 'ضريبة القيمة المضافة - مخرجات (مبيعات)', 'account_type': Account.LIABILITY, 'parent': curr_liab})
    Account.objects.get_or_create(code='2140', defaults={'name': 'مصروفات مستحقة الدفع', 'account_type': Account.LIABILITY, 'parent': curr_liab})
    Account.objects.get_or_create(code='2150', defaults={'name': 'أرصدة دائنة أخرى وتأمينات', 'account_type': Account.LIABILITY, 'parent': curr_liab})
    Account.objects.get_or_create(code='2155', defaults={'name': 'دفعات مقدمة من العملاء', 'account_type': Account.LIABILITY, 'parent': curr_liab, 'allow_reconcile': True})
    Account.objects.get_or_create(code='2160', defaults={'name': 'الرواتب والأجور المستحقة', 'account_type': Account.LIABILITY, 'parent': curr_liab})
    Account.objects.get_or_create(code='2165', defaults={'name': 'التأمينات الاجتماعية المستحقة', 'account_type': Account.LIABILITY, 'parent': curr_liab})
    Account.objects.get_or_create(code='2166', defaults={'name': 'ضريبة كسب العمل المستحقة', 'account_type': Account.LIABILITY, 'parent': curr_liab})
    Account.objects.get_or_create(code='2167', defaults={'name': 'عمولات موظفين مستحقة', 'account_type': Account.LIABILITY, 'parent': curr_liab})
    Account.objects.get_or_create(code='2170', defaults={'name': 'أوراق دفع (شيكات برسم السداد)', 'account_type': Account.LIABILITY, 'parent': curr_liab, 'allow_reconcile': True})
    Account.objects.get_or_create(code='2180', defaults={'name': 'أقساط قروض قصيرة الأجل ومستحقة', 'account_type': Account.LIABILITY, 'parent': curr_liab, 'allow_reconcile': True})

    long_liab, _ = Account.objects.get_or_create(code='2200', defaults={'name': 'خصوم طويلة الأجل', 'account_type': Account.LIABILITY, 'parent': liab})
    Account.objects.get_or_create(code='2210', defaults={'name': 'قروض وتسهيلات بنكية طويلة الأجل', 'account_type': Account.LIABILITY, 'parent': long_liab})

    # 3000 EQUITY
    equity, _ = Account.objects.get_or_create(code='3000', defaults={'name': 'حقوق الملكية', 'account_type': Account.EQUITY})
    Account.objects.get_or_create(code='3100', defaults={'name': 'رأس المال', 'account_type': Account.EQUITY, 'parent': equity})
    Account.objects.get_or_create(code='3200', defaults={'name': 'أرباح محتجزة', 'account_type': Account.EQUITY, 'parent': equity})
    Account.objects.get_or_create(code='3300', defaults={'name': 'ملخص الدخل', 'account_type': Account.EQUITY, 'parent': equity})
    Account.objects.get_or_create(code='3400', defaults={'name': 'جاري الشركاء', 'account_type': Account.EQUITY, 'parent': equity, 'allow_reconcile': True, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='3410', defaults={'name': 'مسحوبات الشريك', 'account_type': Account.EQUITY, 'parent': equity, 'allow_reconcile': True, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='3999', defaults={'name': 'أرصدة افتتاحية وتسويات رأس المال', 'account_type': Account.EQUITY, 'parent': equity})

    # 4000 REVENUE
    rev, _ = Account.objects.get_or_create(code='4000', defaults={'name': 'إيرادات', 'account_type': Account.REVENUE})
    Account.objects.get_or_create(code='4100', defaults={'name': 'المبيعات', 'account_type': Account.REVENUE, 'parent': rev})
    Account.objects.get_or_create(code='4110', defaults={'name': 'إيرادات المصنعيات والخدمات', 'account_type': Account.REVENUE, 'parent': rev})
    Account.objects.get_or_create(code='4120', defaults={'name': 'مردودات ومسموحات المبيعات', 'account_type': Account.REVENUE, 'parent': rev, 'is_contra_revenue': True})
    Account.objects.get_or_create(code='4130', defaults={'name': 'خصم مسموح به', 'account_type': Account.REVENUE, 'parent': rev, 'is_contra_revenue': True})
    Account.objects.get_or_create(code='4140', defaults={'name': 'إيرادات تأجير المعدات', 'account_type': Account.REVENUE, 'parent': rev})
    Account.objects.get_or_create(code='4200', defaults={'name': 'إيرادات أخرى', 'account_type': Account.REVENUE, 'parent': rev})
    Account.objects.get_or_create(code='4210', defaults={'name': 'خصم مكتسب (موردين)', 'account_type': Account.REVENUE, 'parent': rev})
    Account.objects.get_or_create(code='4220', defaults={'name': 'إيرادات غرامات وتأخيرات', 'account_type': Account.REVENUE, 'parent': rev})
    Account.objects.get_or_create(code='4230', defaults={'name': 'أرباح بيع واستبعاد أصول ثابتة', 'account_type': Account.REVENUE, 'parent': rev})
    Account.objects.get_or_create(code='4300', defaults={'name': 'إيرادات الصيانة والمصنعيات', 'account_type': Account.REVENUE, 'parent': rev})
    Account.objects.get_or_create(code='4999', defaults={'name': 'أرباح وإيرادات فروق جرد وتسويات بالزيادة', 'account_type': Account.REVENUE, 'parent': rev})

    # 5000 EXPENSE
    exp, _ = Account.objects.get_or_create(code='5000', defaults={'name': 'مصروفات', 'account_type': Account.EXPENSE, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5100', defaults={'name': 'تكلفة البضاعة المباعة', 'account_type': Account.EXPENSE, 'parent': exp, 'is_cogs': True, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5105', defaults={'name': 'تكلفة قطع الغيار المستهلكة في الصيانة', 'account_type': Account.EXPENSE, 'parent': exp, 'is_cogs': True, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5110', defaults={'name': 'مردودات ومسموحات المشتريات', 'account_type': Account.EXPENSE, 'parent': exp, 'allow_direct_payment': False, 'is_contra': True})
    Account.objects.get_or_create(code='5115', defaults={'name': 'فروق تقريب نقطة البيع', 'account_type': Account.EXPENSE, 'parent': exp, 'allow_direct_payment': False})
    
    op_exp, _ = Account.objects.get_or_create(code='5200', defaults={'name': 'مصاريف تشغيلية', 'account_type': Account.EXPENSE, 'parent': exp, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5210', defaults={'name': 'رواتب وأجور', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5215', defaults={'name': 'مصروف عمولات الموظفين', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5220', defaults={'name': 'مصروف الإهلاك', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5230', defaults={'name': 'مصاريف إيجار مقرات ومستودعات', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5240', defaults={'name': 'مصاريف مرافق (كهرباء ومياه وإنترنت)', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5241', defaults={'name': 'مصروف كهرباء', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5242', defaults={'name': 'مصروف مياه', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5250', defaults={'name': 'مصاريف تالف وهالك وعجز مخزون', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': False})
    
    admin_exp, _ = Account.objects.get_or_create(code='5300', defaults={'name': 'مصاريف إدارية وعمولات بنكية', 'account_type': Account.EXPENSE, 'parent': exp, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5305', defaults={'name': 'عمولات ماكينات الدفع والمحافظ والبنوك', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5310', defaults={'name': 'مصاريف صيانة ونظافة المقرات', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5320', defaults={'name': 'مصاريف دعاية وإعلان وتسويق', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5330', defaults={'name': 'مصاريف ضيافة ومستلزمات', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5331', defaults={'name': 'مصروف بوفية', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5332', defaults={'name': 'مصروف تغذية', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5340', defaults={'name': 'تبرعات وإعانات', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5350', defaults={'name': 'مصاريف انتقالات ومواصلات', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5360', defaults={'name': 'مصاريف رسوم واشتراكات حكومية وتراخيص', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})
    Account.objects.get_or_create(code='5370', defaults={'name': 'خسائر بيع واستبعاد أصول ثابتة', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5400', defaults={'name': 'عجز وزيادة الخزينة (الورديات)', 'account_type': Account.EXPENSE, 'parent': exp, 'allow_direct_payment': False})
    Account.objects.get_or_create(code='5500', defaults={'name': 'مصاريف فوائد وتمويل بنكي', 'account_type': Account.EXPENSE, 'parent': exp, 'allow_direct_payment': False})

    # التأكد من وجود الحسابات المطلوبة بالاسم في حال وجود شجرة قديمة بأكواد مختلفة
    if not Account.objects.filter(name__icontains='بوفية').exists() and not Account.objects.filter(name__icontains='بوفيه').exists():
        Account.objects.get_or_create(code='5331', defaults={'name': 'مصروف بوفية', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})

    if not Account.objects.filter(name__icontains='تغذية').exists():
        Account.objects.get_or_create(code='5332', defaults={'name': 'مصروف تغذية', 'account_type': Account.EXPENSE, 'parent': admin_exp, 'allow_direct_payment': True})

    if not Account.objects.filter(name='مصروف كهرباء').exists() and not Account.objects.filter(name='كهرباء').exists():
        Account.objects.get_or_create(code='5241', defaults={'name': 'مصروف كهرباء', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': True})

    if not Account.objects.filter(name='مصروف مياه').exists() and not Account.objects.filter(name='مياه').exists():
        Account.objects.get_or_create(code='5242', defaults={'name': 'مصروف مياه', 'account_type': Account.EXPENSE, 'parent': op_exp, 'allow_direct_payment': True})

    if not Account.objects.filter(name__icontains='مسحوبات الشريك').exists():
        Account.objects.get_or_create(code='3410', defaults={'name': 'مسحوبات الشريك', 'account_type': Account.EQUITY, 'parent': equity, 'allow_reconcile': True, 'allow_direct_payment': True})

    Account.objects.filter(code='5360', name__contains='وااشتراكات').update(name='مصاريف رسوم واشتراكات حكومية وتراخيص')
    Account.objects.filter(code='2130', name='ضريبة القيمة المضافة').update(name='ضريبة القيمة المضافة - مخرجات (مبيعات)')

    Account.objects.rebuild()

    # ── Auto-link / heal all existing Treasuries, BankAccounts, and EWallets
    try:
        from apps.tenant.accounting.models import Treasury, BankAccount, EWallet
        from apps.tenant.core.models import Branch, SystemSetting

        for t in Treasury.objects.all():
            t.ensure_account()
        for b in BankAccount.objects.all():
            b.ensure_account()
        for w in EWallet.objects.all():
            w.ensure_account()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error linking treasuries in setup_chart_of_accounts: {e}")



class Command(BaseCommand):
    help = 'Sets up the default chart of accounts for a tenant or standalone database'

    def add_arguments(self, parser):
        parser.add_argument('--schema', type=str, help='The schema name to run this for')

    def handle(self, *args, **options):
        schema_name = options.get('schema')
        has_tenants = 'django_tenants' in getattr(settings, 'INSTALLED_APPS', [])

        if has_tenants:
            try:
                from django_tenants.utils import schema_context
                from apps.public.tenants.models import Tenant

                if schema_name:
                    tenants = Tenant.objects.filter(schema_name=schema_name)
                else:
                    tenants = Tenant.objects.exclude(schema_name='public')

                if tenants.exists():
                    for tenant in tenants:
                        with schema_context(tenant.schema_name):
                            self.stdout.write(self.style.SUCCESS(f'Setting up Chart of Accounts for {tenant.schema_name}'))
                            setup_chart_of_accounts_data()
                    return
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'Multi-tenant resolution bypassed: {e}'))

        # Fallback for standalone / SQLite or when no tenant schemas found
        self.stdout.write(self.style.SUCCESS('Setting up Chart of Accounts in current database/schema...'))
        setup_chart_of_accounts_data()
        self.stdout.write(self.style.SUCCESS('Chart of Accounts setup completed successfully!'))
