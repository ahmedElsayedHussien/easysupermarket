from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from apps.tenant.inventory.models import Category, Product
from apps.tenant.partners.models import Partner
from apps.tenant.pricing.models import CustomerSector, PriceList, PriceListItem
from apps.tenant.pricing.services import PricingEngineService


class PricingEngineTestCase(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='البقالة')
        self.product1 = Product.objects.create(
            name='أرز فاخر 1 كجم',
            category=self.category,
            sale_price=Decimal('50.0000')
        )
        self.product2 = Product.objects.create(
            name='زيت عباد 1 لتر',
            category=self.category,
            sale_price=Decimal('80.0000')
        )
        self.sector_wholesale = CustomerSector.objects.create(name='قطاع الجملة')
        self.partner_vip = Partner.objects.create(
            name='شركة السلام للتجارة',
            partner_type='CUSTOMER',
            sector=self.sector_wholesale
        )
        self.partner_regular = Partner.objects.create(
            name='عميل نقدي عادي',
            partner_type='CUSTOMER'
        )

        # Price list for sector: 10% discount on product1 (50 -> 45), fixed 70 on product2
        self.sector_list = PriceList.objects.create(
            name='قائمة أسعار الجملة',
            list_type=PriceList.SECTOR,
            sector=self.sector_wholesale
        )
        PriceListItem.objects.create(
            price_list=self.sector_list,
            product=self.product1,
            pricing_method=PriceListItem.DISCOUNT,
            discount_percentage=Decimal('10.00')
        )
        PriceListItem.objects.create(
            price_list=self.sector_list,
            product=self.product2,
            pricing_method=PriceListItem.FIXED,
            custom_price=Decimal('70.0000')
        )

        # Customer specific list for partner_vip: product1 fixed at 40 (overrides 45 from sector!)
        self.customer_list = PriceList.objects.create(
            name='قائمة شركة السلام الخاصة',
            list_type=PriceList.CUSTOMER,
            partner=self.partner_vip
        )
        PriceListItem.objects.create(
            price_list=self.customer_list,
            product=self.product1,
            pricing_method=PriceListItem.FIXED,
            custom_price=Decimal('40.0000')
        )

    def test_tier_1_customer_priority(self):
        # partner_vip gets 40 on product1 because Tier 1 (Customer) beats Tier 2 (Sector)
        res = PricingEngineService.get_effective_price(self.product1, partner=self.partner_vip)
        self.assertEqual(res['tier'], 'CUSTOMER')
        self.assertEqual(res['price'], Decimal('40.0000'))

    def test_tier_2_sector_fallback(self):
        # partner_vip gets 70 on product2 from Sector list because product2 is not in customer list
        res = PricingEngineService.get_effective_price(self.product2, partner=self.partner_vip)
        self.assertEqual(res['tier'], 'SECTOR')
        self.assertEqual(res['price'], Decimal('70.0000'))

    def test_tier_3_default_fallback(self):
        # partner_regular has no sector and no customer list -> default price
        res1 = PricingEngineService.get_effective_price(self.product1, partner=self.partner_regular)
        self.assertEqual(res1['tier'], 'DEFAULT')
        self.assertEqual(res1['price'], Decimal('50.0000'))

        res2 = PricingEngineService.get_effective_price(self.product2, partner=self.partner_regular)
        self.assertEqual(res2['tier'], 'DEFAULT')
        self.assertEqual(res2['price'], Decimal('80.0000'))

    def test_anonymous_customer_default(self):
        # When no partner is given (e.g. walk-in POS customer)
        res = PricingEngineService.get_effective_price(self.product1, partner=None)
        self.assertEqual(res['tier'], 'DEFAULT')
        self.assertEqual(res['price'], Decimal('50.0000'))


class PricingViewsTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.backends.db import SessionStore

        self.user = User.objects.create_superuser(username='testadmin', password='password123', email='admin@test.com')
        self.rf = RequestFactory()

    def _setup_request(self, request):
        from django.contrib.messages.storage.fallback import FallbackStorage
        request.user = self.user
        setattr(request, 'session', 'session')
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)
        return request

    def test_sector_create_get(self):
        from apps.tenant.pricing.views import sector_create
        req = self._setup_request(self.rf.get('/pricing/sectors/create/'))
        resp = sector_create(req)
        self.assertEqual(resp.status_code, 200)

    def test_sector_create_post(self):
        from apps.tenant.pricing.views import sector_create
        req = self._setup_request(self.rf.post('/pricing/sectors/create/', {
            'name': 'قطاع الشركات',
            'code': 'CORP',
            'description': 'شركات قطاع خاص',
            'is_active': 'on'
        }))
        resp = sector_create(req)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(CustomerSector.objects.filter(name='قطاع الشركات').exists())

    def test_pricelist_create_get(self):
        from apps.tenant.pricing.views import pricelist_create
        req = self._setup_request(self.rf.get('/pricing/pricelists/create/'))
        resp = pricelist_create(req)
        self.assertEqual(resp.status_code, 200)

    def test_pricelist_list_get(self):
        from apps.tenant.pricing.views import pricelist_list
        req = self._setup_request(self.rf.get('/pricing/pricelists/'))
        resp = pricelist_list(req)
        self.assertEqual(resp.status_code, 200)

    def test_sector_list_get(self):
        from apps.tenant.pricing.views import sector_list
        req = self._setup_request(self.rf.get('/pricing/sectors/'))
        resp = sector_list(req)
        self.assertEqual(resp.status_code, 200)

