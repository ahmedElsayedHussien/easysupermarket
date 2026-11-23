from django.apps import AppConfig


class PricingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.pricing'
    label = 'pricing'
    verbose_name = 'التسعير وقوائم الأسعار'
