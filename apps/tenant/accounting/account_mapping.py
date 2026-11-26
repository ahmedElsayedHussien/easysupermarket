"""
ربط الحسابات: every system operation (sales, output VAT, input VAT, employees' receivable, collection fees…)
is identified by its DEFAULT account code; the admin can point it to another posting (leaf) account of the
same type. Every posting resolves its account through ``journal_service._get_system_account(code)``, which
asks ``mapped_account(code)`` first — so one switch here moves every future entry of that operation.

The mapping is cached per tenant schema (cleared when it changes) so posting doesn't pay an extra query.
"""
import threading

from django.db import connection

_cache = {}
_lock = threading.Lock()

# The operations worth mapping (posting accounts). Group accounts and parents of auto-created
# sub-accounts (treasuries 1110, banks 1120, customers 1130, suppliers 2110, wallets 1160, POS machines
# 1170, intermediaries 1157) are structural and not listed.
ROLE_CODES = [
    # assets
    '1135', '1140', '1145', '1150', '1155', '1180', '1185', '1190', '1195', '1215', '1220',
    # liabilities
    '2120', '2130', '2135', '2160', '2165', '2166', '2167', '2170', '2210',
    # equity
    '3100', '3200', '3999',
    # revenue
    '4110', '4120', '4230', '4240', '4300', '4999',
    # expenses
    '5100', '5105', '5110', '5115', '5210', '5212', '5215', '5220', '5230', '5250', '5305', '5306', '5360',
    '5370', '5500',
]
GROUPS = [('1', 'الأصول'), ('2', 'الخصوم'), ('3', 'حقوق الملكية'), ('4', 'الإيرادات'), ('5', 'المصروفات')]


def _schema():
    return getattr(connection, 'schema_name', 'default') or 'default'


def _load():
    from .models import AccountMapping
    return {code: (acc_id, acc_code) for code, acc_id, acc_code in
            AccountMapping.objects.filter(account__isnull=False).values_list('code', 'account_id', 'account__code')}


def mapped_account(code):
    """The account the admin mapped this operation to, or None (then the default code is used)."""
    from .models import Account
    schema = _schema()
    with _lock:
        table = _cache.get(schema)
    if table is None:
        try:
            table = _load()
        except Exception:          # table missing while migrating
            return None
        with _lock:
            _cache[schema] = table
    hit = table.get(str(code))
    if not hit:
        return None
    acc_id, acc_code = hit
    # id AND code must still match (a rolled-back change can't point to another account)
    return Account.objects.filter(pk=acc_id, code=acc_code, is_active=True).first()


def clear_cache():
    with _lock:
        _cache.pop(_schema(), None)


def roles():
    """[{code, default (Account|None), mapped (Account|None), group}] for the settings screen."""
    from .models import Account, AccountMapping
    defaults = {a.code: a for a in Account.objects.filter(code__in=ROLE_CODES)}
    current = {m.code: m.account for m in AccountMapping.objects.select_related('account')}
    out = []
    for code in ROLE_CODES:
        default = defaults.get(code)
        out.append({'code': code, 'default': default, 'mapped': current.get(code),
                    'group': code[0], 'type': default.account_type if default else None})
    return out


def posting_accounts(account_type):
    """Leaf, active accounts of a type (the only valid targets of a mapping)."""
    from .models import Account
    return Account.objects.filter(account_type=account_type, is_active=True, children__isnull=True).order_by('code')


def set_mapping(code, account, user=None):
    from django.core.exceptions import ValidationError
    from .models import Account, AccountMapping
    if code not in ROLE_CODES:
        raise ValidationError('عملية غير معروفة.')
    if account is None:
        AccountMapping.objects.filter(code=code).delete()
        clear_cache()
        return None
    default = Account.objects.filter(code=code).first()
    if default is not None and account.account_type != default.account_type:
        raise ValidationError(f'الحساب {account.code} نوعه مختلف عن الحساب الافتراضي {default.code}.')
    if not account.is_active or account.children.exists():
        raise ValidationError(f'الحساب {account.code} رئيسي أو موقوف — اختار حساب فرعي نشط.')
    m, _ = AccountMapping.objects.update_or_create(code=code, defaults={'account': account, 'updated_by': user})
    clear_cache()
    return m
