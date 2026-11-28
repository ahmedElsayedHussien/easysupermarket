"""Database-level immutability triggers for posted accounting_journalitem rows (P2-4)."""
from apps.tenant.core.db_triggers import run_pg_statements, run_sqlite_statements

PG_JOURNALITEM_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION prevent_posted_journalitem_mutation()
RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM accounting_journalentry
        WHERE id = OLD.entry_id AND status = 'POSTED'
    ) THEN
        RAISE EXCEPTION 'لا يمكن تعديل أو حذف سطور قيد محاسبي مرحّل (POSTED).';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.entry_id IS DISTINCT FROM OLD.entry_id THEN
        IF EXISTS (
            SELECT 1 FROM accounting_journalentry
            WHERE id = NEW.entry_id AND status = 'POSTED'
        ) THEN
            RAISE EXCEPTION 'لا يمكن نقل سطر إلى قيد محاسبي مرحّل (POSTED).';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""".strip()

PG_DROP_JOURNALITEM_TRIGGER_SQL = (
    "DROP TRIGGER IF EXISTS trg_prevent_posted_journalitem_mutation ON accounting_journalitem;",
    "DROP FUNCTION IF EXISTS prevent_posted_journalitem_mutation();",
)

PG_CREATE_JOURNALITEM_TRIGGER_STATEMENTS = (
    PG_JOURNALITEM_FUNCTION_SQL,
    PG_DROP_JOURNALITEM_TRIGGER_SQL[0],
    (
        "CREATE TRIGGER trg_prevent_posted_journalitem_mutation "
        "BEFORE UPDATE OR DELETE ON accounting_journalitem "
        "FOR EACH ROW EXECUTE FUNCTION prevent_posted_journalitem_mutation();"
    ),
)

SQLITE_JOURNALITEM_TRIGGERS = (
    (
        "CREATE TRIGGER IF NOT EXISTS trg_sqlite_prevent_posted_journalitem_update "
        "BEFORE UPDATE ON accounting_journalitem "
        "FOR EACH ROW "
        "WHEN EXISTS (SELECT 1 FROM accounting_journalentry WHERE id = OLD.entry_id AND status = 'POSTED') "
        "OR EXISTS (SELECT 1 FROM accounting_journalentry WHERE id = NEW.entry_id AND status = 'POSTED') "
        "BEGIN "
        "SELECT RAISE(ABORT, 'Cannot modify items of a POSTED JournalEntry'); "
        "END;"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS trg_sqlite_prevent_posted_journalitem_delete "
        "BEFORE DELETE ON accounting_journalitem "
        "FOR EACH ROW "
        "WHEN EXISTS (SELECT 1 FROM accounting_journalentry WHERE id = OLD.entry_id AND status = 'POSTED') "
        "BEGIN "
        "SELECT RAISE(ABORT, 'Cannot delete items of a POSTED JournalEntry'); "
        "END;"
    ),
)

SQLITE_DROP_JOURNALITEM_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_sqlite_prevent_posted_journalitem_update;",
    "DROP TRIGGER IF EXISTS trg_sqlite_prevent_posted_journalitem_delete;",
)


def install_posted_journalitem_trigger(apps, schema_editor):
    """Install PostgreSQL trigger blocking UPDATE/DELETE on POSTED JournalItem rows."""
    run_pg_statements(schema_editor, PG_CREATE_JOURNALITEM_TRIGGER_STATEMENTS)


def drop_posted_journalitem_trigger(apps, schema_editor):
    """Remove PostgreSQL immutability trigger from accounting_journalitem."""
    run_pg_statements(schema_editor, PG_DROP_JOURNALITEM_TRIGGER_SQL)


def install_sqlite_posted_journalitem_trigger(conn):
    """Install equivalent SQLite triggers for raw-SQL enforcement tests."""
    run_sqlite_statements(conn, SQLITE_JOURNALITEM_TRIGGERS)


def drop_sqlite_posted_journalitem_trigger(conn):
    """Drop SQLite test triggers from accounting_journalitem."""
    run_sqlite_statements(conn, SQLITE_DROP_JOURNALITEM_TRIGGERS)
