from django.core.management.base import BaseCommand
from django.contrib.auth.models import Permission
from apps.tenant.core.models import Employee, Role


class Command(BaseCommand):
    help = (
        'Gives every Employee with role ADMIN in the CURRENT schema all model permissions. '
        'It never sets the global is_superuser/is_staff flags (auth_user is shared by all tenants). '
        'With django-tenants run it via: manage.py tenant_command sync_admin_permissions --schema=<schema>'
    )

    def handle(self, *args, **options):
        admins = Employee.objects.filter(role=Role.ADMIN).select_related('user')
        all_perms = list(Permission.objects.all())
        count = 0
        for emp in admins:
            emp.user.user_permissions.set(all_perms)
            count += 1
            self.stdout.write(self.style.SUCCESS(f'Synced admin permissions for user: {emp.user.username}'))
        if count == 0:
            self.stdout.write(self.style.WARNING('No ADMIN employees found in this schema.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Done. Synced {count} admin user(s).'))
