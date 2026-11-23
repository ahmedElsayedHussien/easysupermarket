"""
Single place to READ system settings that several apps share.

    from apps.tenant.core.settings_helpers import get_settings, get_pos_defaults, ...

Never read Branch.default_pos_* / SystemSetting.default_pos_* directly: use
get_pos_defaults(branch), which applies the per-branch override, falls back to
the global default, and never returns a treasury that belongs to another branch.
"""
import datetime
from decimal import Decimal, ROUND_HALF_UP


def get_settings():
    from django.db import connection, transaction
    from apps.tenant.core.models import SystemSetting
    # Public schema (django-tenants) has no SystemSetting table: don't even query,
    # a failed query would abort the surrounding PostgreSQL transaction.
    if getattr(connection, 'schema_name', None) == 'public':
        return None
    try:
        with transaction.atomic():  # savepoint: a failure can't poison the caller's transaction
            return SystemSetting.get_settings()
    except Exception:  # table missing while bootstrapping a fresh DB
        return None


def setting(name, default=None):
    s = get_settings()
    value = getattr(s, name, None) if s is not None else None
    return default if value is None else value


# ---------------------------------------------------------------------------
# POS payment defaults (per branch, then global)
# ---------------------------------------------------------------------------

def get_pos_defaults(branch):
    """
    {'treasury', 'bank_account', 'instapay_account', 'ewallet'} for ``branch``:
    the branch's own default if set, else the global SystemSetting default.
    A treasury is only returned if it belongs to ``branch`` (treasuries are
    branch-bound; banks / e-wallets are company-wide).
    """
    s = get_settings()
    fields = {
        'treasury': 'default_pos_treasury',
        'bank_account': 'default_pos_bank_account',
        'instapay_account': 'default_pos_instapay_account',
        'ewallet': 'default_pos_ewallet',
    }
    out = {}
    for key, field in fields.items():
        value = getattr(branch, field, None) if branch is not None else None
        if value is None and s is not None:
            value = getattr(s, field, None)
        out[key] = value
    t = out['treasury']
    if t is not None and branch is not None and t.branch_id and t.branch_id != branch.id:
        out['treasury'] = None
    return out


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------

def accounting_lock_date():
    return setting('accounting_lock_date')


def is_date_locked(date):
    """True if ``date`` falls in the closed period (on or before the lock date)."""
    lock = accounting_lock_date()
    if not lock or date is None:
        return False
    if isinstance(date, datetime.datetime):
        date = date.date()
    return date <= lock


def lock_error_message(date=None):
    lock = accounting_lock_date()
    return (f'الفترة المحاسبية مقفلة حتى {lock:%Y-%m-%d}. لا يمكن إضافة أو تعديل أو إلغاء قيود بتاريخ '
            f'{date:%Y-%m-%d} أو قبل تاريخ القفل.' if date else
            f'الفترة المحاسبية مقفلة حتى {lock:%Y-%m-%d}.')


def round_cash(amount):
    """Round a cash amount to SystemSetting.pos_cash_rounding (0 = no rounding)."""
    step = Decimal(str(setting('pos_cash_rounding', Decimal('0')) or 0))
    amount = Decimal(str(amount))
    if step <= 0:
        return amount
    return ((amount / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step).quantize(Decimal('0.01'))


# ---------------------------------------------------------------------------
# HR
# ---------------------------------------------------------------------------

def weekly_off_weekdays():
    """Python weekday numbers (Mon=0 .. Sun=6) that are weekly days off."""
    raw = setting('weekly_off_days', '4') or ''
    days = set()
    for part in str(raw).replace('،', ',').split(','):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            days.add(int(part))
    return days


# ---------------------------------------------------------------------------
# Security (used by settings: AXES_FAILURE_LIMIT / AXES_COOLOFF_TIME callables)
# ---------------------------------------------------------------------------

def axes_failure_limit(request=None, credentials=None):
    try:
        return int(setting('login_max_attempts', 5) or 5)
    except Exception:
        return 5


def axes_cooloff_time(request=None):
    try:
        return datetime.timedelta(minutes=int(setting('login_lockout_minutes', 15) or 15))
    except Exception:
        return datetime.timedelta(minutes=15)
