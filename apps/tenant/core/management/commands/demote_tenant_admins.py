"""
Remove the global is_superuser / is_staff flags from users that were elevated
because of a tenant role (old behaviour of Employee.save / user forms).

auth_user is shared by all tenants, so those flags gave a tenant admin full
access to every tenant and to the public /admin/. After this command tenant
admins keep their rights through Employee.role == ADMIN.

Dry run by default; pass --apply to write changes.
Users listed with --keep (platform operators) are never touched.
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = 'Demote users with an Employee profile in any tenant schema from is_superuser/is_staff (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually write the changes (default: dry run).')
        parser.add_argument('--keep', nargs='*', default=[], metavar='USERNAME',
                            help='Usernames of genuine platform operators to leave untouched.')

    def _employee_user_ids(self):
        from apps.tenant.core.models import Employee
        from apps.tenant.core.access import employee_table_exists
        if not employee_table_exists():
            return set()
        return set(Employee.objects.values_list('user_id', flat=True))

    def handle(self, *args, **options):
        from django.contrib.auth.models import User

        apply = options['apply']
        keep = set(options['keep'] or [])
        user_ids = {}  # user_id -> set of schemas

        is_tenants = 'django_tenants' in connection.settings_dict.get('ENGINE', '') or hasattr(connection, 'set_schema')
        if is_tenants:
            from django_tenants.utils import get_tenant_model, schema_context, get_public_schema_name
            public = get_public_schema_name()
            for tenant in get_tenant_model().objects.exclude(schema_name=public).order_by('schema_name'):
                with schema_context(tenant.schema_name):
                    for uid in self._employee_user_ids():
                        user_ids.setdefault(uid, set()).add(tenant.schema_name)
        else:
            for uid in self._employee_user_ids():
                user_ids.setdefault(uid, set()).add('(default)')

        users = User.objects.filter(id__in=user_ids.keys()).filter(
            is_superuser=True) | User.objects.filter(id__in=user_ids.keys()).filter(is_staff=True)
        changed = 0
        for user in users.distinct().order_by('username'):
            schemas = ', '.join(sorted(user_ids.get(user.id, [])))
            if user.username in keep:
                self.stdout.write(f'KEEP    {user.username} (superuser={user.is_superuser}, staff={user.is_staff}) schemas: {schemas}')
                continue
            self.stdout.write(
                f'{"DEMOTE " if apply else "WOULD DEMOTE"} {user.username}: '
                f'is_superuser {user.is_superuser}->False, is_staff {user.is_staff}->False; employee in: {schemas}'
            )
            if apply:
                user.is_superuser = False
                user.is_staff = False
                user.save(update_fields=['is_superuser', 'is_staff'])
            changed += 1

        if apply:
            self.stdout.write(self.style.SUCCESS(f'Done. Demoted {changed} user(s).'))
        else:
            self.stdout.write(self.style.WARNING(
                f'Dry run: {changed} user(s) would be demoted. Re-run with --apply '
                f'(use --keep <username> for platform operators who also have an Employee profile).'
            ))
