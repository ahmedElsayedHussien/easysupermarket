"""AuditLog append-only + deleting a user: works (also on PostgreSQL with the immutability trigger), and the
line stays, still readable by the name kept on it."""
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase

from apps.tenant.core.models import AuditLog
from apps.tenant.core.tests.factories import make_user


class AuditLogUserDeleteTests(TestCase):
    def test_deleting_a_user_with_log_lines(self):
        u = make_user('gone_user', employee=False)
        log = AuditLog.objects.create(user=u, action=AuditLog.LOGIN, object_repr='دخول')
        self.assertEqual(log.username, 'gone_user')
        uid = u.pk
        u.delete()                                     # no UPDATE on the log (the trigger would refuse it)
        self.assertFalse(User.objects.filter(pk=uid).exists())
        kept = AuditLog.objects.get(pk=log.pk)
        self.assertEqual((kept.user_id, kept.username), (uid, 'gone_user'))
        self.assertIsNone(kept.user_label)

    def test_trigger_active_on_postgresql(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL only')
        from django.db import DatabaseError, transaction
        log = AuditLog.objects.create(action=AuditLog.LOGIN, object_repr='x')
        with self.assertRaises(DatabaseError), transaction.atomic():       # raw SQL: the trigger itself refuses
            with connection.cursor() as c:
                c.execute('UPDATE core_auditlog SET object_repr = %s WHERE id = %s', ['y', log.pk])
