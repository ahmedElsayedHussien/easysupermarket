# EasySupermarket - Engineering & Architecture Standards (Fixed Order)

## 1. Design & Styling
- **Primary Button Color:** All primary action buttons (Save, Add, Submit, Primary Actions) must be **Orange** (`#ff7300` / `btn-orange`) with a soft glow effect by default across all screens.

---

## 2. Django & Python Engineering Rules
- **Zero FloatField Policy:** Never use `FloatField` or Python `float` for monetary values, taxes, discounts, or quantities. Always use `models.DecimalField(max_digits=18, decimal_places=4)` and Python's `decimal.Decimal` to prevent floating-point rounding errors.
- **Atomicity & Row-Level Locking:** Wrap every financial state change (posting invoices, cashbox transfers, inventory deductions) inside `with transaction.atomic():`. Always use `select_for_update()` when reading and updating balances or stock levels to prevent race conditions.
- **Ban on_delete=models.CASCADE:** Never use `CASCADE` on financial foreign keys (e.g., Customer → Invoice, Account → JournalLine). Always enforce `on_delete=models.PROTECT` to prevent accidental deletion of financial history.
- **Database-Level Constraints:** Do not rely solely on views, forms, or serializers for validation. Enforce rules at the DB level using `CheckConstraint` and `UniqueConstraint` inside `class Meta` (e.g., `debit >= 0`, `credit >= 0`, and preventing both `debit > 0` and `credit > 0` on the same line).
- **Isolated Service Layer:** Keep `views.py` and `serializers.py` thin. All accounting logic, calculations, and journal generation must live in dedicated `services.py` modules so they are reusable and independently unit-tested.
- **Database-Level Aggregation & Zero In-Memory Loops:** Never loop over querysets or model instances in Python/Views to calculate sums, balances, or breakdown totals. All financial and aggregate calculations must execute in SQL via Django ORM expressions (`aggregate`, `annotate`, `Sum`, `Case`, `When`, `F`, `Coalesce`).
- **Zero N+1 Queries & Lazy Loading:** Prohibit per-item database property lookups in view templates/loops. Always leverage batched/prefetched evaluations (e.g. `batch_get_account_balances`, `select_related`, `prefetch_related`) and enforce pagination and lazy loading on all heavy reports and tables.

---

## 3. Reliability, Security & Auditability
- **Idempotency:** Every mutation endpoint (invoice creation, payment posting) must accept and enforce a unique `idempotency_key` (UUID) to prevent duplicate entries from network retries or double-clicks.
- **Immutable Audit Trail:** Automatically log every sensitive action in an append-only audit table capturing: `user`, `timestamp` (`timezone.now()`), `action_type`, and before/after snapshots.
- **Gapless Sequential Numbering:** Assign official document numbers (invoice numbers, journal numbers) only upon posting, never while in `DRAFT` state, to avoid sequence gaps when drafts are discarded.

---

## 4. Data Integrity & Inventory Safeguards
- **Zero Negative Stock on Import:** When importing products or opening balances from Excel sheets, negative quantities or balances must strictly never be recorded; any negative value must be automatically sanitized/zeroed out to `0`.
- **Deduplication Safeguards:** Enforce pre-import duplicate checks on barcodes and names to prevent duplicate product records, updating existing items rather than re-creating them.
