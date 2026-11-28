"""
Tenant-aware Django test runner for PostgreSQL + django-tenants.

Creates and activates a default test tenant (`schema_name='test'`) with domains
`testserver`, `localhost`, and `127.0.0.1` so both ORM tests and Django test
`Client()` requests route inside the tenant schema.
"""
from django.db import connection
from django.db.models.signals import post_migrate
from django.test.runner import DiscoverRunner

_restoring_tenant = False


def ensure_test_tenant(schema_name='test', domains=('testserver', 'localhost', '127.0.0.1'), verbosity=0):
    """Ensure the PostgreSQL test tenant and its primary domain exist and are active."""
    global _restoring_tenant
    if not hasattr(connection, 'set_tenant') or _restoring_tenant:
        return None

    from apps.public.tenants.models import Domain, Tenant

    _restoring_tenant = True
    try:
        connection.set_schema_to_public()
        tenant = Tenant.objects.filter(schema_name=schema_name).first()
        if tenant is None:
            tenant = Tenant(
                schema_name=schema_name,
                name='Test Supermarket',
                owner_name='Test Owner',
                owner_email='owner@test.local',
            )
            tenant.auto_create_schema = True
            tenant.save(verbosity=verbosity)

        for idx, domain_name in enumerate(domains):
            Domain.objects.get_or_create(
                domain=domain_name,
                defaults={'tenant': tenant, 'is_primary': idx == 0},
            )

        connection.set_tenant(tenant)
        return tenant
    finally:
        _restoring_tenant = False


def _post_migrate_restore_test_tenant(sender, **kwargs):
    """Re-seed test tenant after TransactionTestCase flushes public schema tables."""
    from django_tenants.utils import schema_exists

    if schema_exists('test'):
        ensure_test_tenant(verbosity=0)


class TenantAwareTestRunner(DiscoverRunner):
    """DiscoverRunner that provisions and maintains a test tenant on PostgreSQL."""

    def setup_databases(self, **kwargs):
        old_config = super().setup_databases(**kwargs)
        ensure_test_tenant(verbosity=max(0, self.verbosity - 1))
        post_migrate.connect(
            _post_migrate_restore_test_tenant,
            dispatch_uid='easysupermarket_restore_test_tenant',
        )
        return old_config

    def teardown_databases(self, old_config, **kwargs):
        post_migrate.disconnect(dispatch_uid='easysupermarket_restore_test_tenant')
        connection.set_schema_to_public()
        return super().teardown_databases(old_config, **kwargs)
