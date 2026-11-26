"""إعدادات النظام صف واحد، وسجل التعديلات للإضافة بس."""
from django.test import TestCase

from apps.tenant.core.models import AuditLog, SystemSetting


class CoreIntegrityTests(TestCase):
    def test_settings_singleton(self):
        s = SystemSetting.get_settings()
        self.assertEqual(s.pk, 1)
        with self.assertRaises(ValueError):
            SystemSetting(store_name='تاني').save()
        self.assertEqual(SystemSetting.objects.count(), 1)
        s.store_name = 'المحل'
        s.save()
        self.assertEqual(SystemSetting.get_settings().store_name, 'المحل')

    def test_audit_log_is_append_only(self):
        log = AuditLog.objects.create(action=AuditLog.LOGIN, object_repr='x')
        log.object_repr = 'y'
        with self.assertRaises(ValueError):
            log.save()
        with self.assertRaises(ValueError):
            log.delete()
        self.assertEqual(AuditLog.objects.get(pk=log.pk).object_repr, 'x')
