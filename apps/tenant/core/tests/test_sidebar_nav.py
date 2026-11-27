"""Tests for sidebar show/hide settings and centralized nav visibility calculation."""
from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.nav import build_sidebar_nav
from apps.tenant.core.tests.factories import build_world, login, make_user, system_settings


class SidebarNavTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.url = reverse('core:main_screen')

    def setUp(self):
        # Reset settings to all-True defaults before each test
        system_settings(**{
            field.name: True
            for field in self.w.settings._meta.fields
            if field.name.startswith('show_nav_')
        })
        login(self.client, self.w.admin)

    def test_defaults_show_newly_toggleable_links_for_admin(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(reverse('invoicing:purchase_order_list'), html)
        self.assertIn(reverse('invoicing:rep_list'), html)
        self.assertIn(reverse('core:audit_log'), html)
        self.assertIn(reverse('accounting:custody_list'), html)

    def test_toggling_single_new_item_hides_only_that_item(self):
        system_settings(show_nav_purchase_orders=False)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn(reverse('invoicing:purchase_order_list'), html)
        self.assertIn(reverse('invoicing:purchase_invoice_list'), html)

    def test_hiding_all_sales_items_hides_sales_section_header(self):
        system_settings(
            show_nav_quotations=False,
            show_nav_sales_invoices=False,
            show_nav_pricelists=False,
            show_nav_sales_reps=False,
            show_nav_loading_orders=False,
            show_nav_unloading_orders=False,
            show_nav_rep_handovers=False,
            show_nav_rep_app=False,
        )
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn('nav-section-title">المبيعات<', html)

    def test_hiding_only_old_sales_items_keeps_sales_header_for_reps_links(self):
        system_settings(
            show_nav_quotations=False,
            show_nav_sales_invoices=False,
            show_nav_pricelists=False,
        )
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('nav-section-title">المبيعات<', html)
        self.assertIn(reverse('invoicing:rep_list'), html)

    def test_build_sidebar_nav_hr_section_respects_user_perms(self):
        plain_user = make_user(username='no_hr_perms', branch=self.w.branch, employee=False)
        setting = system_settings()

        # Both attendance & payroll on -> sec_hr is True even without HR model permissions
        nav = build_sidebar_nav(plain_user, setting, is_admin=False)
        self.assertFalse(nav['item_hr_employees'])
        self.assertFalse(nav['item_hr_attendance_manage'])
        self.assertFalse(nav['item_hr_my_portal'])
        self.assertTrue(nav['item_hr_attendance'])
        self.assertTrue(nav['sec_hr'])

        # Disable only attendance -> payroll keeps sec_hr True
        setting = system_settings(show_nav_hr_attendance=False, show_nav_hr_payroll=True)
        nav = build_sidebar_nav(plain_user, setting, is_admin=False)
        self.assertFalse(nav['item_hr_attendance'])
        self.assertTrue(nav['item_hr_payroll'])
        self.assertTrue(nav['sec_hr'])

        # Disable both attendance & payroll -> user has no HR perms and no employee profile -> sec_hr False
        setting = system_settings(show_nav_hr_attendance=False, show_nav_hr_payroll=False)
        nav = build_sidebar_nav(plain_user, setting, is_admin=False)
        self.assertFalse(nav['sec_hr'])
