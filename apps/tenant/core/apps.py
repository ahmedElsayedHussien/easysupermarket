from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.core'
    label = 'core'
    verbose_name = 'الإعدادات الأساسية'

    def ready(self):
        # Uploaded images -> resized WebP files (apps.tenant.core.images)
        from apps.tenant.core.images import connect_image_optimizers
        connect_image_optimizers()
        # سجل التعديلات (apps.tenant.core.audit)
        from apps.tenant.core.audit import connect as connect_audit
        connect_audit()
