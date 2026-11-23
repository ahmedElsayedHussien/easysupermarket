from django.core.management.base import BaseCommand
from django.contrib.auth.models import User, Permission
from apps.tenant.core.models import Employee, Role
from apps.tenant.core.decorators import is_admin_user

class Command(BaseCommand):
    help = 'Syncs all admin users to have full permissions, superuser/staff flags, and active employee profiles.'

    def handle(self, *args, **options):
        # Find all users that might be admins
        all_users = User.objects.all()
        admin_count = 0

        for user in all_users:
            emp = getattr(user, 'employee_profile', None)
            is_admin = (
                user.is_superuser
                or user.is_staff
                or (emp and emp.role == Role.ADMIN)
                or (emp and emp.is_admin())
            )
            
            if is_admin:
                admin_count += 1
                user.is_superuser = True
                user.is_staff = True
                user.save()

                if not emp:
                    emp, _ = Employee.objects.get_or_create(
                        user=user,
                        defaults={'role': Role.ADMIN}
                    )
                else:
                    if emp.role != Role.ADMIN:
                        emp.role = Role.ADMIN
                        emp.save()

                # Assign all permissions
                all_perms = Permission.objects.all()
                user.user_permissions.set(all_perms)
                self.stdout.write(self.style.SUCCESS(f'Successfully synced admin permissions for user: {user.username}'))

        if admin_count == 0:
            self.stdout.write(self.style.WARNING('No admin users found to sync.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Done. Synced {admin_count} admin user(s).'))
