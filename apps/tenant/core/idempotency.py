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


def find_by_idempotency_key(model, idem_key, select_related=(), **owner):
    """Look up an existing record by parsed UUID key, or return None. ``owner`` (e.g. created_by=user)
    limits the match to the same user's records: a key never returns someone else's document."""
    if idem_key is None:
        return None
    qs = model.objects.select_related(*select_related) if select_related else model.objects.all()
    return qs.filter(idempotency_key=idem_key, **owner).first()


def new_form_key(request):
    """The key a form page carries in its hidden ``idempotency_key`` input: the one just posted when the
    page is shown again after an error (a re-submit stays the same operation), else a fresh one."""
    posted = parse_idempotency_key(request.POST.get('idempotency_key')) if request.method == 'POST' else None
    return posted or uuid.uuid4()


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
