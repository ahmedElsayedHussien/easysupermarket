"""Shared idempotency-key helpers for financial transaction endpoints."""
import uuid

from django.db import IntegrityError


def parse_idempotency_key(raw):
    """Parse a raw UUID string (or UUID) into a uuid.UUID, returning None when blank/invalid."""
    if not raw:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def find_by_idempotency_key(model, idem_key, select_related=()):
    """Look up an existing record by parsed UUID key, or return None."""
    if idem_key is None:
        return None
    qs = model.objects.select_related(*select_related) if select_related else model.objects.all()
    return qs.filter(idempotency_key=idem_key).first()


def run_idempotent(model, raw_key, create_fn, select_related=()):
    """Execute ``create_fn(idem_key)`` once per UUID key, returning ``(obj, deduplicated)``."""
    idem_key = parse_idempotency_key(raw_key)
    existing = find_by_idempotency_key(model, idem_key, select_related=select_related)
    if existing is not None:
        return existing, True
    try:
        return create_fn(idem_key), False
    except IntegrityError:
        if idem_key is not None:
            existing = find_by_idempotency_key(model, idem_key, select_related=select_related)
            if existing is not None:
                return existing, True
        raise
