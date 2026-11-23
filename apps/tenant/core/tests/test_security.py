"""Authorization / authentication regression tests."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.tenant.core.decorators import is_admin_user
from apps.tenant.core.tests.factories import (
    ADMIN_PASSWORD, build_world, login, make_user,
)


class IsAdminUserTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_username_containing_admin_is_not_admin(self):
        u = make_user('admin_branch2', role='CASHIER', branch=self.w.branch)
        self.assertFalse(is_admin_user(u))
        u2 = make_user('administrator', employee=False)
        self.assertFalse(is_admin_user(u2))

    def test_staff_flag_is_not_admin(self):
        u = make_user('staffer', role='CASHIER', branch=self.w.branch, is_staff=True)
        self.assertFalse(is_admin_user(u))

    def test_admin_role_is_admin(self):
        self.assertTrue(is_admin_user(self.w.admin))
        self.assertFalse(is_admin_user(self.w.cashier))


class UserManagementVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()
        cls.root = User.objects.create_superuser('platform_root', 'r@example.com', 'x')
        from apps.tenant.core.models import Employee
        Employee.objects.create(user=cls.root, role='ADMIN', branch=cls.w.branch)

    def setUp(self):
        login(self.client, self.w.admin)

    def test_user_list_hides_superusers_from_tenant_admin(self):
        resp = self.client.get(reverse('core:user_list'))
        self.assertEqual(resp.status_code, 200)
        ids = [u.pk for u in resp.context['users']]
        self.assertIn(self.w.cashier.pk, ids)
        self.assertNotIn(self.root.pk, ids)

    def test_user_update_of_superuser_is_404(self):
        resp = self.client.get(reverse('core:user_update', args=[self.root.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_cashier_cannot_open_user_list(self):
        self.client.logout()
        login(self.client, self.w.cashier)
        resp = self.client.get(reverse('core:user_list'))
        self.assertNotEqual(resp.status_code, 200)


class EndpointProtectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_rentals_excel_export_requires_login(self):
        resp = self.client.get(reverse('rentals:report_detailed_rentals_excel'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_get_on_post_only_delete_part_is_405(self):
        login(self.client, self.w.admin)
        resp = self.client.get(reverse('maintenance:delete_part', args=[1]))
        self.assertEqual(resp.status_code, 405)

    def test_get_on_post_only_commission_rule_delete_is_405(self):
        login(self.client, self.w.admin)
        resp = self.client.get(reverse('maintenance:commission_rule_delete', args=[1]))
        self.assertEqual(resp.status_code, 405)

    def test_get_on_post_only_payroll_process_is_405(self):
        login(self.client, self.w.admin)
        resp = self.client.get(reverse('hr:payroll_process'))
        self.assertEqual(resp.status_code, 405)


class EInvoicingApiAuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.tenant.einvoicing.models import TaxIntegrationSettings
        cls.settings_obj = TaxIntegrationSettings.objects.create(client_id='c', client_secret='s')

    def test_token_generated(self):
        self.assertTrue(len(self.settings_obj.api_token) >= 20)

    def test_pending_without_token_is_401(self):
        resp = self.client.get(reverse('einvoicing:api_pending'))
        self.assertEqual(resp.status_code, 401)

    def test_pending_with_wrong_token_is_401(self):
        resp = self.client.get(reverse('einvoicing:api_pending'), HTTP_AUTHORIZATION='Bearer wrong-token')
        self.assertEqual(resp.status_code, 401)

    def test_pending_with_wrong_scheme_is_401(self):
        resp = self.client.get(reverse('einvoicing:api_pending'),
                               HTTP_AUTHORIZATION=f'Basic {self.settings_obj.api_token}')
        self.assertEqual(resp.status_code, 401)

    def test_pending_with_valid_token_is_200(self):
        resp = self.client.get(reverse('einvoicing:api_pending'),
                               HTTP_AUTHORIZATION=f'Bearer {self.settings_obj.api_token}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_signed_without_token_is_401(self):
        resp = self.client.post(reverse('einvoicing:api_signed', args=[1]), data='{}',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 401)

    def test_signed_with_wrong_token_is_401(self):
        resp = self.client.post(reverse('einvoicing:api_signed', args=[1]), data='{}',
                                content_type='application/json', HTTP_AUTHORIZATION='Bearer nope')
        self.assertEqual(resp.status_code, 401)


@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=5)
class AxesLockoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def _login(self, password):
        return self.client.post(reverse('login'), {'username': 'tenant_admin', 'password': password},
                                REMOTE_ADDR='10.1.2.3')

    def test_correct_password_logs_in_before_lockout(self):
        resp = self._login(ADMIN_PASSWORD)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_lockout_after_five_failures(self):
        for _ in range(5):
            resp = self._login('wrong-password')
            self.assertNotIn('_auth_user_id', self.client.session)
        resp = self._login(ADMIN_PASSWORD)
        self.assertIn(resp.status_code, (403, 429))
        self.assertNotIn('_auth_user_id', self.client.session)
