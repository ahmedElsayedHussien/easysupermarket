"""Whoever can move stock between warehouses (inventory.change_inventorybatch = the storekeeper)
gets the warehouse side of van loading/unloading: review & approve, print, issue, receive."""
from django.db import migrations

NEW = [
    ('loadingorder', 'view_loadingorder', 'Can view طلب تحميل'),
    ('loadingorder', 'approve_loadingorder', 'اعتماد طلب التحميل'),
    ('loadingorder', 'issue_loadingorder', 'صرف طلب التحميل من المخزن وترحيله'),
    ('unloadingorder', 'view_unloadingorder', 'Can view طلب تفريغ'),
    ('unloadingorder', 'receive_unloadingorder', 'استلام طلب التفريغ في المخزن وترحيله'),
]


def forwards(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    User = apps.get_model('auth', 'User')
    Group = apps.get_model('auth', 'Group')
    old = Permission.objects.filter(content_type__app_label='inventory', content_type__model='inventorybatch',
                                    codename='change_inventorybatch').first()
    for model, codename, name in NEW:
        ct, _ = ContentType.objects.get_or_create(app_label='invoicing', model=model)
        perm, _ = Permission.objects.get_or_create(content_type=ct, codename=codename, defaults={'name': name})
        if old is None:
            continue
        for user in User.objects.filter(user_permissions=old).exclude(user_permissions=perm):
            user.user_permissions.add(perm)
        for group in Group.objects.filter(permissions=old).exclude(permissions=perm):
            group.permissions.add(perm)


class Migration(migrations.Migration):
    dependencies = [
        ('invoicing', '0025_van_sales'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
