"""تثبيت كتطبيق (PWA) وجرس التنبيهات."""
import json

from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login


class PwaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_manifest_and_network_only_worker(self):
        r = self.client.get(reverse('core:pwa_manifest'))
        data = json.loads(r.content)
        self.assertEqual((data['display'], data['dir']), ('standalone', 'rtl'))
        r = self.client.get(reverse('core:pwa_sw'))
        self.assertEqual(r['Content-Type'], 'application/javascript')
        self.assertNotIn(b'caches.', r.content)                       # never caches business data

    def test_bell_api(self):
        login(self.client, self.w.admin)
        r = self.client.get(reverse('core:alerts_api'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('items', r.json())
        r = self.client.get(reverse('core:main_screen'))
        self.assertContains(r, 'id="bellBtn"')
        self.assertContains(r, 'rel="manifest"')
