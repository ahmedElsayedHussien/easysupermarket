"""سجل التعديلات: what is logged, what is not, and the screen."""
from decimal import Decimal as D

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from apps.tenant.core.audit import TRACKED
from apps.tenant.core.models import AuditLog
from apps.tenant.core.tests.factories import build_world, login, make_invoice, make_user, stock_in


class AuditLogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        login(self.client, self.w.admin)
        self._since_id = AuditLog.objects.order_by('-id').values_list('id', flat=True).first() or 0

    def _logs(self, **filters):
        return AuditLog.objects.filter(id__gt=self._since_id, **filters)

    def test_configured_models_and_fields_exist(self):
        for label, cfg in TRACKED.items():
            model = apps.get_model(label)
            if cfg['fields'] != '__all__':
                names = {f.name for f in model._meta.concrete_fields}
                self.assertFalse(set(cfg['fields']) - names, f'{label}: {set(cfg["fields"]) - names}')

    def test_price_change_logged_with_before_after(self):
        p = self.w.product
        old = p.sale_price
        p.sale_price = old + D('5')
        p.save()
        log = self._logs(action='UPDATE', object_id=str(p.pk), content_type__model='product').get()
        (field, (before, after)), = log.changes.items()
        self.assertEqual(D(before), old)
        self.assertEqual(D(after), old + D('5'))
        self.assertEqual(log.user, None)   # no request in a unit test

    def test_pos_sale_is_not_logged_but_cancellation_is(self):
        stock_in(self.w.product, self.w.warehouse, 10, D('5'))
        inv = make_invoice(self.w, lines=[(self.w.product, 1, D('10'))])
        self.assertFalse(self._logs(object_id=str(inv.pk), content_type__model='invoice').exists())
        from apps.tenant.services.invoice_service import cancel_invoice
        cancel_invoice(inv.id, user=self.w.admin)
        log = self._logs(content_type__model='invoice', object_id=str(inv.pk)).get()
        self.assertIn('الحالة', ''.join(log.changes))

    def test_permissions_and_login_logged(self):
        u = make_user('audited_user', role='CASHIER', branch=self.w.branch)
        from django.contrib.auth.models import Permission
        u.user_permissions.add(Permission.objects.get(codename='view_invoice'))
        self.assertTrue(any('صلاحيات اتضافت' in l.changes
                            for l in self._logs(object_id=str(u.pk), content_type__model='user')))

    def test_edit_through_screen_records_user_and_ip(self):
        p = self.w.product
        from apps.tenant.core.audit import _request
        from django.test import RequestFactory
        req = RequestFactory().get('/', REMOTE_ADDR='10.0.0.7')
        req.user = self.w.admin
        token = _request.set(req)
        try:
            p.name = 'اسم معدل'
            p.save()
        finally:
            _request.reset(token)
        log = self._logs(content_type__model='product', object_id=str(p.pk)).latest('id')
        self.assertEqual((log.user, log.ip), (self.w.admin, '10.0.0.7'))

    def test_screen_and_permission(self):
        p = self.w.product
        p.name = 'اسم جديد للصنف'
        p.save()
        r = self.client.get(reverse('core:audit_log'))
        self.assertContains(r, 'اسم جديد للصنف')
        login(self.client, make_user('no_audit', role='CASHIER', branch=self.w.branch))
        self.assertEqual(self.client.get(reverse('core:audit_log')).status_code, 302)
