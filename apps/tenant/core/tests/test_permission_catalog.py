"""The user screen's permission list stays in step with the code."""
import glob
import json
import os
import re

from django.conf import settings
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.permission_catalog import ALL_PERMS, ROLE_DEFAULTS, catalog_permissions
from apps.tenant.core.tests.factories import build_world, login, make_user

APPS = 'core|accounting|inventory|invoicing|partners|maintenance|einvoicing|hr|rentals|pricing'
PERM_RE = re.compile(r"['\"]((?:%s)\.[a-z_]+)['\"]" % APPS)


def all_model_permissions():
    from django.apps import apps
    out = set()
    for model in apps.get_models():
        meta = model._meta
        out.update(f'{meta.app_label}.{a}_{meta.model_name}' for a in meta.default_permissions)
        out.update(f'{meta.app_label}.{c}' for c, _ in meta.permissions)
    return out
TEMPLATE_RE = re.compile(r"perms\.((?:%s))\.([a-z_]+)" % APPS)


def perms_used_in_code():
    root = settings.BASE_DIR
    used = set()
    for path in glob.glob(os.path.join(root, 'apps', 'tenant', '**', '*.py'), recursive=True):
        norm = path.replace('\\', '/')
        if '/migrations/' in norm or '/tests' in norm or norm.endswith('permission_catalog.py'):
            continue
        used.update(PERM_RE.findall(open(path, encoding='utf-8').read()))
    for path in glob.glob(os.path.join(root, 'templates', '**', '*.html'), recursive=True):
        used.update(f'{a}.{c}' for a, c in TEMPLATE_RE.findall(open(path, encoding='utf-8').read()))
    return used & all_model_permissions()


class CatalogTests(TestCase):
    def test_every_permission_checked_in_code_is_on_the_user_screen(self):
        missing = perms_used_in_code() - set(ALL_PERMS)
        self.assertFalse(missing, f'add these to core/permission_catalog.py: {sorted(missing)}')

    def test_catalog_has_no_dead_or_duplicate_entries(self):
        self.assertEqual(len(ALL_PERMS), len(set(ALL_PERMS)))
        self.assertEqual(set(catalog_permissions()), set(ALL_PERMS), 'catalog permission missing in DB')
        unused = set(ALL_PERMS) - perms_used_in_code()
        self.assertFalse(unused, f'nothing checks these, remove them from the catalog: {sorted(unused)}')

    def test_role_defaults_are_catalog_permissions(self):
        for role, codes in ROLE_DEFAULTS.items():
            self.assertFalse(set(codes) - set(ALL_PERMS), role)


class UserScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)

    def test_screen_lists_sections_and_role_presets(self):
        r = self.client.get(reverse('core:user_create'))
        self.assertContains(r, 'نقطة البيع والمبيعات')
        self.assertContains(r, 'إنشاء فواتير الشراء ومرتجعات الشراء')
        self.assertNotContains(r, 'Can add')
        presets = json.loads(r.context['role_defaults_json'])
        self.assertIn('CASHIER', presets)
        shown = {i['code'] for s in r.context['permission_sections'] for i in s['items']}
        self.assertEqual(shown, set(ALL_PERMS))

    def test_saving_keeps_permissions_outside_the_catalog(self):
        user = make_user('perm_edit_target', role='CASHIER', branch=self.w.branch,
                         perms=('invoicing.view_invoice', 'auth.view_user'))
        pos_sell = Permission.objects.get(content_type__app_label='invoicing', codename='add_invoice')
        r = self.client.post(reverse('core:user_update', args=[user.pk]), {
            'username': 'perm_edit_target', 'first_name': 'أ', 'last_name': 'ب', 'email': '',
            'branch': self.w.branch.id, 'role': 'CASHIER', 'is_active': 'on',
            'user_permissions': [pos_sell.id],
        })
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        codes = set(user.user_permissions.values_list('codename', flat=True))
        self.assertEqual(codes, {'add_invoice', 'view_user'})   # view_invoice unticked, auth perm kept

    def test_edit_screen_shows_current_permissions_ticked(self):
        user = make_user('perm_show_target', role='CASHIER', branch=self.w.branch, perms=('invoicing.view_invoice',))
        r = self.client.get(reverse('core:user_update', args=[user.pk]))
        ticked = {i['code'] for s in r.context['permission_sections'] for i in s['items'] if i['checked']}
        self.assertEqual(ticked, {'invoicing.view_invoice'})


class SplitPermissionTests(TestCase):
    """Actions that got their own permission."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_seller_cannot_open_purchase_invoice_or_cancel(self):
        cashier = make_user('pos_only_user', role='CASHIER', branch=self.w.branch,
                            perms=('invoicing.view_invoice', 'invoicing.add_invoice', 'invoicing.change_invoice'))
        login(self.client, cashier)
        r = self.client.get(reverse('invoicing:purchase_invoice'))
        self.assertRedirects(r, reverse('core:main_screen'), fetch_redirect_response=False)

    def test_purchase_permission_opens_purchase_invoice(self):
        clerk = make_user('purchase_clerk', role='INVENTORY', branch=self.w.branch,
                          perms=('invoicing.view_invoice', 'invoicing.add_purchase_invoice'))
        login(self.client, clerk)
        self.assertEqual(self.client.get(reverse('invoicing:purchase_invoice')).status_code, 200)

    def test_egs_screen_needs_its_own_permission(self):
        user = make_user('einv_sender', role='ACCOUNTANT', branch=self.w.branch,
                         perms=('einvoicing.view_einvoicelog', 'einvoicing.change_einvoicelog'))
        login(self.client, user)
        r = self.client.get(reverse('einvoicing:egs_codes'))
        self.assertEqual(r.status_code, 302)


class MigrationGrantTests(TestCase):
    """0021/0012 give the new specific permissions to whoever had the old broad one."""

    def test_holders_of_the_old_permission_keep_access(self):
        import importlib
        from django.apps import apps as global_apps
        mig = importlib.import_module('apps.tenant.invoicing.migrations.0021_split_permissions')
        w = build_world()
        user = make_user('legacy_seller', role='CASHIER', branch=w.branch, perms=('invoicing.add_invoice',))
        Permission.objects.filter(codename__in=['add_purchase_invoice', 'add_sales_return']).delete()
        mig.forwards(global_apps, None)
        user = type(user).objects.get(pk=user.pk)   # fresh permission cache
        self.assertTrue(user.has_perm('invoicing.add_purchase_invoice'))
        self.assertTrue(user.has_perm('invoicing.add_sales_return'))
        self.assertFalse(user.has_perm('invoicing.cancel_invoice'))   # needs change_invoice


class AdminHoldsEveryPermissionTests(TestCase):
    def test_admin_role_has_permissions_added_after_it_was_created(self):
        w = build_world()
        admin = type(w.admin).objects.get(pk=w.admin.pk)
        admin.user_permissions.clear()
        self.assertTrue(admin.has_perm('hr.change_attendance'))
        self.assertTrue(admin.has_perm('einvoicing.manage_egs_codes'))
        cashier = make_user('plain_cashier', role='CASHIER', branch=w.branch)
        self.assertFalse(cashier.has_perm('hr.change_attendance'))
