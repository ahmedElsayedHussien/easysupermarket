# One-off maintenance script. Run from the project root: python scripts/inspect_dup_batches.py
import sqlite3

conn = sqlite3.connect('db.sqlite3')
c = conn.cursor()

sample_name = '.. 2كارتة كراون 220فولت CT33098'
c.execute("SELECT id, name, barcode, sale_price, last_purchase_price, min_stock_level, created_at, updated_at FROM inventory_product WHERE name = ?", (sample_name,))
rows = c.fetchall()
print(f"Details for {sample_name}:")
for r in rows:
    print(" ", r)

sample_name2 = '0قمة رباط قصيرة 32مم على 3/4" اسود تصادميه صلب هواء'
c.execute("SELECT id, name, barcode, sale_price, last_purchase_price, min_stock_level, created_at, updated_at FROM inventory_product WHERE name = ?", (sample_name2,))
rows = c.fetchall()
print(f"\nDetails for {sample_name2}:")
for r in rows:
    print(" ", r)

# Let's check range of IDs for the two batches
c.execute("""
    SELECT min(id), max(id), count(*), min(created_at), max(created_at) 
    FROM inventory_product
    WHERE id < 8000
""")
print("\nBatch 1:", c.fetchall())

c.execute("""
    SELECT min(id), max(id), count(*), min(created_at), max(created_at) 
    FROM inventory_product
    WHERE id >= 8000
""")
print("Batch 2:", c.fetchall())

conn.close()
