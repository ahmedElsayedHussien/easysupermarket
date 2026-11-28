"""
P3-1: Tests for PostgreSQL + django-tenants test config, production deploy check,
and concurrency / row-locking behavior across PostgreSQL workers and SQLite unit tests.
"""
from concurrent.futures import ThreadPoolExecutor
import datetime
from decimal import Decimal as D
from unittest.mock import patch
import uuid

from django.db import connection
from django.test import TestCase, TransactionTestCase

from apps.tenant.core.idempotency import run_idempotent
from apps.tenant.core.models import DocumentSequence
from apps.tenant.core.sequences import next_sequence
from apps.tenant.core.tests.factories import build_world, make_invoice, stock_in
from apps.tenant.invoicing.models import Invoice
from apps.tenant.services.fifo_engine import InsufficientStockError, consume_fifo_batches


class TestEnvironmentAndDeployCheckTests(TestCase):
    """Verify PostgreSQL test config, production settings security flags, and locking edge cases."""

    @classmethod
    def setUpTestData(cls):
        cls.w = build_world()

    def test_test_pg_settings_retains_django_tenants_and_tenant_runner(self):
        import config.settings.test_pg as test_pg

        self.assertIn('django_tenants', test_pg.INSTALLED_APPS)
        self.assertTrue(any('TenantMainMiddleware' in m for m in test_pg.MIDDLEWARE))
        self.assertEqual(test_pg.DATABASES['default']['ENGINE'], 'django_tenants.postgresql_backend')
        self.assertEqual(test_pg.DATABASE_ROUTERS, ['django_tenants.routers.TenantSyncRouter'])
        self.assertEqual(test_pg.TEST_RUNNER, 'apps.public.tenants.test_runner.TenantAwareTestRunner')

    def test_production_settings_security_flags_and_deploy_check(self):
        import config.settings.production as prod

        self.assertFalse(prod.DEBUG)
        self.assertEqual(prod.DATABASES['default']['ENGINE'], 'django_tenants.postgresql_backend')
        self.assertGreaterEqual(prod.SECURE_HSTS_SECONDS, 31536000)
        self.assertTrue(prod.SECURE_HSTS_INCLUDE_SUBDOMAINS)
        self.assertTrue(prod.SECURE_HSTS_PRELOAD)
        self.assertTrue(prod.SECURE_SSL_REDIRECT)
        self.assertTrue(prod.SESSION_COOKIE_SECURE)
        self.assertTrue(prod.CSRF_COOKIE_SECURE)
        self.assertTrue(prod.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertEqual(prod.X_FRAME_OPTIONS, 'DENY')

    def test_sequence_concurrent_first_insert_integrity_error_recovery(self):
        from django.db.models.query import QuerySet

        key = 'RACE-SEQ-UNIT-'
        DocumentSequence.objects.create(key=key, last_value=9)
        orig_first = QuerySet.first
        skipped = {'done': False}

        def racy_first(qs_self):
            if qs_self.model is DocumentSequence and not skipped['done']:
                skipped['done'] = True
                return None
            return orig_first(qs_self)

        with patch.object(QuerySet, 'first', autospec=True, side_effect=racy_first):
            val = next_sequence(key, seed=lambda: 9)

        self.assertTrue(skipped['done'])
        self.assertEqual(val, 10)
        self.assertEqual(DocumentSequence.objects.get(key=key).last_value, 10)

    def test_fifo_consume_batches_locks_rows_and_rejects_over_consumption(self):
        batch = stock_in(self.w.product, self.w.warehouse, 5, D('10.0000'))
        consumed = consume_fifo_batches(
            self.w.product, self.w.warehouse, D('3'), reference='FIFO-UNIT-1', allow_negative=False
        )
        self.assertEqual(len(consumed), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D('2.0000'))

        with self.assertRaises(InsufficientStockError):
            consume_fifo_batches(
                self.w.product, self.w.warehouse, D('4'), reference='FIFO-UNIT-2', allow_negative=False
            )
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D('2.0000'))


class PostgreSQLConcurrencyTests(TransactionTestCase):
    """Multi-threaded concurrency tests designed for PostgreSQL + django-tenants CI runs."""

    def _run_in_threads(self, fn, count):
        tenant = getattr(connection, 'tenant', None)

        def worker(idx):
            try:
                if tenant is not None and hasattr(connection, 'set_tenant'):
                    connection.set_tenant(tenant)
                return fn(idx)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(worker, range(count)))

    def test_concurrent_next_sequence_produces_unique_gapless_numbers(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Multi-threaded locking concurrency test requires PostgreSQL.')
        values = self._run_in_threads(lambda _i: next_sequence('PG-CONC-SEQ-'), 8)
        self.assertEqual(sorted(values), list(range(1, 9)))

    def test_concurrent_consume_fifo_batches_prevents_double_spend(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Multi-threaded FIFO SELECT FOR UPDATE test requires PostgreSQL.')
        w = build_world()
        batch = stock_in(w.product, w.warehouse, 5, D('10.0000'))

        def attempt(i):
            try:
                consume_fifo_batches(
                    w.product, w.warehouse, D('3'), reference=f'PG-CONC-{i}', allow_negative=False
                )
                return 'OK'
            except InsufficientStockError:
                return 'INSUFFICIENT'

        outcomes = self._run_in_threads(attempt, 4)
        self.assertEqual(outcomes.count('OK'), 1)
        self.assertEqual(outcomes.count('INSUFFICIENT'), 3)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_remaining, D('2.0000'))

    def test_concurrent_run_idempotent_creates_single_invoice(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Multi-threaded idempotency test requires PostgreSQL.')
        w = build_world()
        idem_uuid = str(uuid.uuid4())

        def attempt(_i):
            return run_idempotent(
                Invoice,
                idem_uuid,
                lambda k: make_invoice(
                    w, invoice_type='SALE', lines=[], confirm=False, payment_type='CREDIT',
                    idempotency_key=k, date=datetime.date.today()
                ),
            )[0].pk

        pks = self._run_in_threads(attempt, 4)
        self.assertEqual(len(set(pks)), 1)
        self.assertEqual(Invoice.objects.filter(idempotency_key=idem_uuid).count(), 1)

    def test_pg_triggers_active_in_postgres_schema(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL trigger presence verification requires PostgreSQL.')
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT tgname FROM pg_trigger
                WHERE NOT tgisinternal
                  AND tgname IN ('trg_prevent_posted_journalitem_mutation', 'trg_prevent_auditlog_mutation')
                ORDER BY tgname
                """
            )
            triggers = [row[0] for row in cur.fetchall()]
        self.assertEqual(
            triggers,
            ['trg_prevent_auditlog_mutation', 'trg_prevent_posted_journalitem_mutation'],
        )
