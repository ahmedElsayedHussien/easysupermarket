"""
Race-safe sequential numbering for documents (invoices, journal entries, vouchers...).

Usage:
    from apps.tenant.core.sequences import next_sequence
    num = next_sequence('SALE-2026-', seed=lambda: <current max int already used>)
    invoice_number = f'SALE-2026-{num:05d}'

The counter row is locked with SELECT ... FOR UPDATE, so two concurrent
transactions can never receive the same number. Call it inside the same
transaction.atomic() block that saves the document so the lock is held until commit.
`seed` is called only the first time a key is used, to continue from numbers
that already exist in the database.
"""
import re

from django.db import IntegrityError, transaction


def max_numeric_suffix(queryset, field, prefix):
    """Return the highest integer suffix of `field` values starting with `prefix` (0 if none)."""
    pattern = re.compile(r'^' + re.escape(prefix) + r'(\d+)$')
    best = 0
    for value in queryset.filter(**{f'{field}__startswith': prefix}).values_list(field, flat=True).iterator():
        m = pattern.match(value or '')
        if m:
            best = max(best, int(m.group(1)))
    return best


def next_sequence(key, seed=None):
    from apps.tenant.core.models import DocumentSequence

    with transaction.atomic():
        seq = DocumentSequence.objects.select_for_update().filter(key=key).first()
        if seq is None:
            start = int(seed()) if seed else 0
            try:
                with transaction.atomic():
                    seq = DocumentSequence.objects.create(key=key, last_value=start)
            except IntegrityError:
                pass  # created concurrently by another transaction
            seq = DocumentSequence.objects.select_for_update().get(key=key)
        seq.last_value += 1
        seq.save(update_fields=['last_value'])
        return seq.last_value
