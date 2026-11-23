# One-off maintenance script. Run from the project root: python scripts/inspect_sqlite_barcodes.py
import sqlite3

conn = sqlite3.connect('db.sqlite3')
c = conn.cursor()

c.execute("SELECT id, name, barcode FROM inventory_product WHERE barcode IS NOT NULL AND barcode != '' LIMIT 30")
rows = c.fetchall()
print("Sample products with barcodes in SQLite:")
for r in rows:
    print(" ", r)

# Check if there are any duplicate barcodes in SQLite
c.execute("SELECT barcode, count(*) FROM inventory_product WHERE barcode IS NOT NULL AND barcode != '' GROUP BY barcode HAVING count(*) > 1")
dup_bc = c.fetchall()
print("\nDuplicate barcodes in SQLite:", len(dup_bc))

conn.close()
