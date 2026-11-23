"""SystemSetting wiring: session timeout (BranchMiddleware) and `manage.py auto_backup`."""
import datetime
import io
import os
import shutil
import tempfile
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from apps.tenant.core import backup_service
from apps.tenant.core.tests.factories import build_world, login, system_settings


class SessionTimeoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_session_expiry_follows_setting(self):
        system_settings(session_timeout_hours=3)
        c = login(self.client, self.w.admin)
        c.get('/')
        self.assertEqual(c.session.get_expiry_age(), 3 * 3600)
        system_settings(session_timeout_hours=24)
        c.get('/')
        self.assertEqual(c.session.get_expiry_age(), 24 * 3600)

    def test_no_session_write_when_unchanged(self):
        system_settings(session_timeout_hours=5)
        c = login(self.client, self.w.admin)
        c.get('/')
        with mock.patch('django.contrib.sessions.backends.base.SessionBase.set_expiry') as set_expiry:
            c.get('/')
        set_expiry.assert_not_called()


class AutoBackupCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='auto_backup_test_')
        patcher = mock.patch.object(backup_service, 'BACKUP_DIR', self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = os.path.join(self.tmp, 'default')

    def _run(self, *args):
        out = io.StringIO()
        call_command('auto_backup', *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def _files(self):
        return sorted(os.listdir(self.folder)) if os.path.isdir(self.folder) else []

    def _fake_auto(self, name, age_hours):
        os.makedirs(self.folder, exist_ok=True)
        path = os.path.join(self.folder, name)
        for p in (path, path + '.sig'):
            with open(p, 'w') as fh:
                fh.write('x')
            ts = (datetime.datetime.now() - datetime.timedelta(hours=age_hours)).timestamp()
            os.utime(p, (ts, ts))

    def test_disabled_does_nothing(self):
        system_settings(auto_backup_enabled=False)
        out = self._run()
        self.assertIn('disabled', out)
        self.assertEqual(self._files(), [])

    def test_creates_signed_auto_backup_then_skips_when_recent(self):
        system_settings(auto_backup_enabled=True, auto_backup_retention=3)
        self.assertIn('created', self._run())
        files = self._files()
        dumps = [f for f in files if f.endswith('.db')]
        self.assertEqual(len(dumps), 1)
        self.assertTrue(dumps[0].startswith('auto_backup_sqlite_'))
        self.assertIn(dumps[0] + '.sig', files)
        self.assertTrue(backup_service.verify_signature(os.path.join(self.folder, dumps[0])))
        # a second run within 23h does not create another one
        self.assertIn('skipped', self._run())
        self.assertEqual(len([f for f in self._files() if f.endswith('.db')]), 1)

    def test_prunes_old_auto_backups_but_keeps_manual(self):
        system_settings(auto_backup_enabled=True, auto_backup_retention=2)
        for i, age in enumerate((30, 50, 70, 90)):
            self._fake_auto(f'auto_backup_sqlite_old{i}.db', age)
        self._fake_auto('backup_sqlite_manual.db', 200)       # manual: never pruned
        out = self._run()
        self.assertIn('created', out)
        self.assertIn('pruned 3 old', out)
        files = self._files()
        autos = [f for f in files if f.startswith('auto_') and f.endswith('.db')]
        self.assertEqual(len(autos), 2)
        self.assertIn('auto_backup_sqlite_old0.db', autos)       # newest old one kept
        self.assertNotIn('auto_backup_sqlite_old3.db.sig', files)
        self.assertIn('backup_sqlite_manual.db', files)
        self.assertIn('backup_sqlite_manual.db.sig', files)
