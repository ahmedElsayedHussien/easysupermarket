"""Database-level immutability triggers for core_auditlog (P2-4)."""

PG_AUDITLOG_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION prevent_auditlog_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'سجل التدقيق غير قابل للتعديل أو الحذف (Append-Only).';
END;
$$ LANGUAGE plpgsql;
""".strip()

PG_DROP_AUDITLOG_TRIGGER_SQL = (
    "DROP TRIGGER IF EXISTS trg_prevent_auditlog_mutation ON core_auditlog;",
    "DROP FUNCTION IF EXISTS prevent_auditlog_mutation();",
)

PG_CREATE_AUDITLOG_TRIGGER_STATEMENTS = (
    PG_AUDITLOG_FUNCTION_SQL,
    PG_DROP_AUDITLOG_TRIGGER_SQL[0],
    (
        "CREATE TRIGGER trg_prevent_auditlog_mutation "
        "BEFORE UPDATE OR DELETE ON core_auditlog "
        "FOR EACH ROW EXECUTE FUNCTION prevent_auditlog_mutation();"
    ),
)

SQLITE_AUDITLOG_TRIGGERS = (
    (
        "CREATE TRIGGER IF NOT EXISTS trg_sqlite_prevent_auditlog_update "
        "BEFORE UPDATE ON core_auditlog "
        "FOR EACH ROW BEGIN "
        "SELECT RAISE(ABORT, 'AuditLog is append-only'); "
        "END;"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS trg_sqlite_prevent_auditlog_delete "
        "BEFORE DELETE ON core_auditlog "
        "FOR EACH ROW BEGIN "
        "SELECT RAISE(ABORT, 'AuditLog is append-only'); "
        "END;"
    ),
)

SQLITE_DROP_AUDITLOG_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_sqlite_prevent_auditlog_update;",
    "DROP TRIGGER IF EXISTS trg_sqlite_prevent_auditlog_delete;",
)


def run_pg_statements(schema_editor, statements):
    """Execute DDL statements only when running against PostgreSQL."""
    if schema_editor.connection.vendor != 'postgresql':
        return
    for sql in statements:
        schema_editor.execute(sql)


def run_sqlite_statements(conn, statements):
    """Execute SQL statements on SQLite (used by trigger unit tests)."""
    if conn.vendor != 'sqlite':
        return
    with conn.cursor() as cursor:
        for sql in statements:
            cursor.execute(sql)


def install_auditlog_immutability_trigger(apps, schema_editor):
    """Install PostgreSQL trigger blocking UPDATE/DELETE on core_auditlog."""
    run_pg_statements(schema_editor, PG_CREATE_AUDITLOG_TRIGGER_STATEMENTS)


def drop_auditlog_immutability_trigger(apps, schema_editor):
    """Remove PostgreSQL immutability trigger from core_auditlog."""
    run_pg_statements(schema_editor, PG_DROP_AUDITLOG_TRIGGER_SQL)


def install_sqlite_auditlog_trigger(conn):
    """Install equivalent SQLite triggers for raw-SQL enforcement tests."""
    run_sqlite_statements(conn, SQLITE_AUDITLOG_TRIGGERS)


def drop_sqlite_auditlog_trigger(conn):
    """Drop SQLite test triggers from core_auditlog."""
    run_sqlite_statements(conn, SQLITE_DROP_AUDITLOG_TRIGGERS)
