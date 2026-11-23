"""
Scheduled automatic backup (SystemSetting.auto_backup_enabled / auto_backup_retention).

    python manage.py auto_backup            # back up every tenant that enabled it
    python manage.py auto_backup --force    # ignore the "once a day" check

For each tenant (PostgreSQL: every tenant schema; SQLite: the single database):
  * skipped unless auto_backup_enabled is on,
  * a new signed backup (file name ``auto_backup_...``) is created when the newest
    automatic backup is older than 23 hours (or none exists),
  * then only the newest ``auto_backup_retention`` automatic backups are kept.
Manual backups (made from the backup page) are never deleted.

Run by start_server.bat after migrations and by the daily Windows task that
schedule_backup.bat registers. Exit code is 1 only when a backup failed.
"""
import datetime

from django.core.management.base import BaseCommand

MIN_AGE = datetime.timedelta(hours=23)


class Command(BaseCommand):
    help = 'Create the daily automatic backup for tenants that enabled it, then prune old ones.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Back up even if a recent automatic backup exists')

    def handle(self, *args, **options):
        from apps.tenant.core import backup_service

        force = options.get('force', False)
        results = []
        if backup_service.is_postgres() and self._tenants_available():
            from django_tenants.utils import schema_context
            for schema in self._tenant_schemas():
                with schema_context(schema):
                    results.append((schema, self._run_one(force)))
        else:
            results.append(('default', self._run_one(force)))

        failed = 0
        for schema, (status, detail) in results:
            line = f'[{schema}] {status}' + (f': {detail}' if detail else '')
            if status == 'failed':
                failed += 1
                self.stderr.write(self.style.ERROR(line))
            elif status == 'created':
                self.stdout.write(self.style.SUCCESS(line))
            else:
                self.stdout.write(line)
        self.stdout.write(f'auto_backup: {len(results)} tenant(s) checked, '
                          f'{sum(1 for _, (s, _d) in results if s == "created")} backup(s) created, {failed} failed.')
        if failed:
            raise SystemExit(1)

    # ------------------------------------------------------------------
    @staticmethod
    def _tenants_available():
        try:
            import django_tenants  # noqa: F401
            from django.apps import apps
            return apps.is_installed('django_tenants')
        except ImportError:
            return False

    @staticmethod
    def _tenant_schemas():
        from django_tenants.utils import get_tenant_model
        return list(get_tenant_model().objects.exclude(schema_name='public')
                    .order_by('schema_name').values_list('schema_name', flat=True))

    def _run_one(self, force):
        """Returns (status, detail); status in disabled / skipped / created / failed."""
        from apps.tenant.core import backup_service
        from apps.tenant.core.settings_helpers import get_settings

        s = get_settings()
        if s is None or not getattr(s, 'auto_backup_enabled', False):
            return 'disabled', ''
        keep = getattr(s, 'auto_backup_retention', 7) or 7

        try:
            autos = backup_service.list_auto_backups()
        except backup_service.BackupError as e:
            return 'failed', str(e)

        detail = ''
        if not force and autos and datetime.datetime.now() - autos[0]['created_at'] < MIN_AGE:
            status, detail = 'skipped', f"recent backup {autos[0]['filename']}"
        else:
            ok, msg, _path = backup_service.create_backup(auto=True)
            if not ok:
                return 'failed', msg
            status, detail = 'created', msg

        removed = backup_service.prune_auto_backups(keep)
        if removed:
            detail += f' (pruned {len(removed)} old)'
        return status, detail
