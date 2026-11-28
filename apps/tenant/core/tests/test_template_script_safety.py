"""P3-5: data reaches JavaScript through |json_script, never |safe inside an inline <script>.

A value printed with |safe inside <script> is not escaped: a product name like ``</script><script>…`` would
run as code. |json_script escapes <, > and & and puts the data in its own <script type="application/json">."""
import json
import re
from decimal import Decimal as D
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

TEMPLATES = Path(settings.BASE_DIR) / 'templates'
SCRIPT = re.compile(r'<script\b[^>]*>(.*?)</script>', re.S | re.I)
SAFE_VAR = re.compile(r'\{\{[^}]*\|\s*safe\b[^}]*\}\}')


class TemplateScriptSafetyTests(SimpleTestCase):
    def test_no_safe_filter_inside_inline_scripts(self):
        found = []
        for path in TEMPLATES.rglob('*.html'):
            text = path.read_text(encoding='utf-8', errors='replace')
            for block in SCRIPT.finditer(text):
                for var in SAFE_VAR.finditer(block.group(1)):
                    line = text[:block.start(1) + var.start()].count('\n') + 1
                    found.append(f'{path.relative_to(TEMPLATES)}:{line} {var.group(0)}')
        self.assertEqual(found, [], 'use {{ value|json_script:"id" }} + JSON.parse(...textContent) instead')

    def test_json_script_escapes_a_hostile_name(self):
        from django.template import Context, Template
        html = Template('{{ data|json_script:"d" }}').render(Context({'data': {'name': '</script><script>alert(1)'}}))
        self.assertNotIn('</script><script>', html)
        self.assertIn('\\u003C/script\\u003E', html)


def json_element(response, element_id):
    """The parsed content of <script id="element_id" type="application/json"> in the page."""
    html = response.content.decode('utf-8')
    m = re.search(r'<script id="%s" type="application/json">(.*?)</script>' % re.escape(element_id), html, re.S)
    assert m, f'#{element_id} not in the page'
    return json.loads(m.group(1))


class PagesCarryTheirDataAsJsonScriptTests(TestCase):
    """Every page that used |safe now renders its data element, parseable, before the script reading it —
    with a hostile product name kept as data."""

    @classmethod
    def setUpTestData(cls):
        from apps.tenant.core.tests.factories import build_world, make_product
        cls.w = build_world()
        cls.hostile = make_product(cls.w.category, '</script><script>alert(1)</script>', sale_price=D('5'))

    def setUp(self):
        from apps.tenant.core.tests.factories import login
        login(self.client, self.w.admin)

    def assert_no_injection(self, r):
        self.assertNotIn('</script><script>alert(1)', r.content.decode('utf-8'))

    def test_quotation_form(self):
        r = self.client.get(reverse('invoicing:quotation_create'))
        products = json_element(r, 'quotation-products')
        self.assertEqual(products[str(self.hostile.id)]['name'], self.hostile.name)
        self.assert_no_injection(r)

    def test_purchase_invoice(self):
        r = self.client.get(reverse('invoicing:purchase_invoice'))
        for element in ('product-uoms', 'product-last-prices', 'product-stock-info'):
            self.assertIsInstance(json_element(r, element), dict)
        self.assert_no_injection(r)

    def test_account_form(self):
        r = self.client.get(reverse('accounting:account_create'))
        self.assertIsInstance(json_element(r, 'parent-type-map'), dict)

    def test_transfer_form(self):
        r = self.client.get(reverse('accounting:transfer_create'))
        data = json_element(r, 'accounts-data')
        self.assertIn(str(self.w.treasury.id), data['TREASURY'])

    def test_user_form(self):
        r = self.client.get(reverse('core:user_create'))
        self.assertIn('CASHIER', json_element(r, 'roleDefaults'))

    def test_admin_dashboard(self):
        r = self.client.get(reverse('core:admin_dashboard'))
        for element in ('sales-chart-data', 'sales-chart-labels', 'profit-chart-data'):
            self.assertIsInstance(json_element(r, element), list)
