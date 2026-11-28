"""pos_shift_stats_api (polled by pos.js): logged out -> JSON 401 with the login URL, never a redirect."""
from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.tests.factories import build_world, login


class ShiftStatsAuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_logged_out_gets_json_401(self):
        r = self.client.get(reverse('invoicing:pos_shift_stats_api'))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r['Content-Type'], 'application/json')
        self.assertEqual(r.json()['login_url'], '/login/')

    def test_logged_in_gets_the_stats(self):
        login(self.client, self.w.admin)
        r = self.client.get(reverse('invoicing:pos_shift_stats_api'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('has_open_shift', r.json())
