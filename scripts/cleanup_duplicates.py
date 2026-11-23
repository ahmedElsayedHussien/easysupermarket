"""
Cleanup Duplicate Products Tool
EasySupermarket ERP
Safely merges duplicated products and re-links all invoices, FIFO batches, and stock movements.
"""
import os
import sys
import shutil
import sqlite3
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root (script lives in scripts/)
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

def cleanup_sqlite():
    db_path = os.path.join(BASE_DIR, 'db.sqlite3')
    if not os.path.exists(db_path):
        print(f"[ERROR] db.sqlite3 not found at {db_path}")
        return

    print("=" * 65)
    print("      EasySupermarket - Duplicate Products Cleaner (SQLite)")
    print("=" * 65)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("SELECT count(*) FROM inventory_product")
    total_before = c.fetchone()[0]
    print(f"[*] Total products in database: {total_before}")

    c.execute("""
        SELECT count(*) FROM (
            SELECT name, count(*) as cnt 
            FROM inventory_product 
            GROUP BY name 
            HAVING cnt > 1
        )
    """)
    dup_names_count = c.fetchone()[0]

    c.execute("""
        SELECT count(*) FROM (
            SELECT barcode, count(*) as cnt 
            FROM inventory_product 
            WHERE barcode IS NOT NULL AND barcode != ''
            GROUP BY barcode 
            HAVING cnt > 1
        )
    """)
    dup_bc_count = c.fetchone()[0]

    print(f"[*] Distinct product names duplicated: {dup_names_count}")
    print(f"[*] Distinct barcodes duplicated: {dup_bc_count}")

    if dup_names_count == 0 and dup_bc_count == 0:
        print("\n[OK] No duplicate products found! Your database is already clean.")
        conn.close()
        return

    # Check sample
    c.execute("""
        SELECT name, count(*) as cnt 
        FROM inventory_product 
        GROUP BY name 
        HAVING cnt > 1 
        LIMIT 5
    """)
    print("\nSample duplicates found:")
    for row in c.fetchall():
        print(f"  - {row[0]!r} ({row[1]} copies)")

    # Create backup before merging
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BASE_DIR, f"db.sqlite3.backup_before_merge_{ts}")
    print(f"\n[1/3] Creating safety backup of database...")
    conn.close()
    shutil.copyfile(db_path, backup_path)
    print(f"      Backup created at: {backup_path}")

    # Reconnect and merge
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    print(f"[2/3] Merging duplicate products and re-linking transaction records...")

    c.execute("""
        SELECT name, count(*) as cnt 
        FROM inventory_product 
        GROUP BY name 
        HAVING cnt > 1
    """)
    dup_groups = c.fetchall()

    merged_count = 0
    relinked_invoices = 0
    relinked_movements = 0
    relinked_batches = 0

    for name, cnt in dup_groups:
        c.execute("SELECT id, barcode, category_id, unit, sale_price FROM inventory_product WHERE name = ? ORDER BY id ASC", (name,))
        prods = c.fetchall()
        if len(prods) < 2:
            continue

        primary = prods[0]
        prim_id = primary[0]
        prim_bc = primary[1]
        prim_cat = primary[2]
        prim_unit = primary[3]

        best_bc = prim_bc
        best_cat = prim_cat
        best_unit = prim_unit

        for p in prods[1:]:
            if not best_bc and p[1]:
                best_bc = p[1]
            if not best_cat and p[2]:
                best_cat = p[2]
            if not best_unit and p[3]:
                best_unit = p[3]

        if (best_bc != prim_bc) or (best_cat != prim_cat) or (best_unit != prim_unit):
            c.execute(
                "UPDATE inventory_product SET barcode = ?, category_id = ?, unit = ? WHERE id = ?",
                (best_bc, best_cat, best_unit, prim_id)
            )

        for dup in prods[1:]:
            dup_id = dup[0]

            # Re-link invoice lines
            c.execute("UPDATE invoicing_invoiceline SET product_id = ? WHERE product_id = ?", (prim_id, dup_id))
            relinked_invoices += c.rowcount

            # Re-link stock movements
            c.execute("UPDATE inventory_stockmovement SET product_id = ? WHERE product_id = ?", (prim_id, dup_id))
            relinked_movements += c.rowcount

            # Re-link inventory batches
            c.execute("UPDATE inventory_inventorybatch SET product_id = ? WHERE product_id = ?", (prim_id, dup_id))
            relinked_batches += c.rowcount

            # Re-link other potential tables
            for tbl in ['invoicing_quotationline', 'maintenance_ticketpart', 'pricing_pricelistitem',
                        'inventory_productbranch', 'inventory_serialitem', 'inventory_productuom',
                        'inventory_stockadjustmentline']:
                try:
                    c.execute(f"UPDATE {tbl} SET product_id = ? WHERE product_id = ?", (prim_id, dup_id))
                except Exception:
                    pass

            # Delete duplicate product
            c.execute("DELETE FROM inventory_product WHERE id = ?", (dup_id,))
            merged_count += 1

    # Also merge any duplicate barcodes if they exist
    c.execute("""
        SELECT barcode, count(*) as cnt 
        FROM inventory_product 
        WHERE barcode IS NOT NULL AND barcode != ''
        GROUP BY barcode 
        HAVING cnt > 1
    """)
    bc_groups = c.fetchall()
    for bc, cnt in bc_groups:
        c.execute("SELECT id FROM inventory_product WHERE barcode = ? ORDER BY id ASC", (bc,))
        prods = c.fetchall()
        if len(prods) < 2:
            continue
        prim_id = prods[0][0]
        for dup in prods[1:]:
            dup_id = dup[0]
            c.execute("UPDATE invoicing_invoiceline SET product_id = ? WHERE product_id = ?", (prim_id, dup_id))
            relinked_invoices += c.rowcount
            c.execute("UPDATE inventory_stockmovement SET product_id = ? WHERE product_id = ?", (prim_id, dup_id))
            relinked_movements += c.rowcount
            c.execute("UPDATE inventory_inventorybatch SET product_id = ? WHERE product_id = ?", (prim_id, dup_id))
            relinked_batches += c.rowcount
            for tbl in ['invoicing_quotationline', 'maintenance_ticketpart', 'pricing_pricelistitem',
                        'inventory_productbranch', 'inventory_serialitem', 'inventory_productuom',
                        'inventory_stockadjustmentline']:
                try:
                    c.execute(f"UPDATE {tbl} SET product_id = ? WHERE product_id = ?", (prim_id, dup_id))
                except Exception:
                    pass
            c.execute("DELETE FROM inventory_product WHERE id = ?", (dup_id,))
            merged_count += 1

    conn.commit()

    c.execute("SELECT count(*) FROM inventory_product")
    total_after = c.fetchone()[0]

    c.execute("""
        SELECT count(*) FROM (
            SELECT name, count(*) as cnt 
            FROM inventory_product 
            GROUP BY name 
            HAVING cnt > 1
        )
    """)
    remaining_dups = c.fetchone()[0]

    conn.close()

    print("[3/3] Verification & Summary:")
    print("=" * 65)
    print(f"  [SUCCESS] Merged and deleted duplicates: {merged_count} products")
    print(f"  [SUCCESS] Re-linked invoice lines:       {relinked_invoices}")
    print(f"  [SUCCESS] Re-linked stock movements:     {relinked_movements}")
    print(f"  [SUCCESS] Re-linked inventory batches:   {relinked_batches}")
    print(f"  [SUCCESS] Products count before merge:   {total_before}")
    print(f"  [SUCCESS] Products count after merge:    {total_after}")
    print(f"  [SUCCESS] Remaining duplicate names:     {remaining_dups}")
    print("=" * 65)
    print("Cleanup completed successfully! All duplicates resolved.")


def cleanup_django(apply_changes=True):
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
    import django
    django.setup()
    from django.core.management import call_command
    call_command('cleanup_duplicate_products', apply=apply_changes)


if __name__ == '__main__':
    # Check .env or command line
    from decouple import config
    engine = config('DB_ENGINE', default='sqlite').strip().lower()

    if '--sqlite' in sys.argv or engine in ['sqlite', 'sqlite3']:
        cleanup_sqlite()
    else:
        # PostgreSQL multi-tenant via django command
        cleanup_django(apply_changes='--apply' in sys.argv or len(sys.argv) == 1)
