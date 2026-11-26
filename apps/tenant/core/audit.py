"""
سجل التعديلات (AuditLog): who changed what, when, and the values before → after.

- AuditMiddleware keeps the current request (user / IP) in a context variable.
- pre_save / post_save / post_delete signals on the TRACKED models write the log; only the
  listed fields count (noise such as updated_at, cached totals or stock is ignored).
- Normal high-volume flows are NOT logged: creating a sale (POS), an invoice while it is a
  draft, GPS check-ins. What is logged is what a manager wants to review: cancellations,
  payment changes after posting, price / product edits, partners, settings, users and
  their permissions, manual attendance, payroll approval, loans, stock adjustments...
- Logins / logouts are logged too.
Failures here never break the action being logged.
"""
import contextvars
import datetime
import decimal
import logging

from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save

logger = logging.getLogger(__name__)
_request = contextvars.ContextVar('audit_request', default=None)

ALL = '__all__'
NOISE = {'id', 'created_at', 'updated_at', 'last_login', 'password', 'date_joined'}

# model label -> {fields, create, delete, skip(old, new) -> True to ignore an update}
TRACKED = {
    'inventory.Product': {'fields': ['name', 'barcode', 'sku', 'category', 'product_type', 'sale_price',
                                     'min_sale_price', 'tax_rate', 'withholding_tax_rate', 'min_stock_level',
                                     'is_active', 'egs_code'], 'create': True, 'delete': True},
    'inventory.ProductUoM': {'fields': ['uom', 'conversion_factor', 'barcode', 'is_base'], 'create': True, 'delete': True},
    'inventory.ProductBarcode': {'fields': ['barcode', 'uom', 'note'], 'create': True, 'delete': True},
    'inventory.ProductBranch': {'fields': ALL, 'create': True, 'delete': True},
    'inventory.Warehouse': {'fields': ALL, 'create': True, 'delete': True},
    'inventory.StockAdjustment': {'fields': ['status', 'warehouse', 'reason'], 'create': True, 'delete': True},
    'invoicing.Invoice': {'fields': ['status', 'payment_type', 'partner', 'treasury', 'bank_account', 'ewallet',
                                     'paid_cash', 'paid_card', 'paid_ewallet', 'paid_bank', 'paid_credit'],
                          'create': False, 'delete': True,
                          # a draft being filled / posted is the normal flow, not an edit
                          'skip': lambda old, new: getattr(old, 'status', None) == 'DRAFT'},
    'invoicing.POSSession': {'fields': ['status', 'closing_balance_actual', 'difference', 'variance_charged_to'],
                             'create': True, 'delete': True},
    'invoicing.Quotation': {'fields': ['status', 'partner'], 'create': False, 'delete': True},
    'accounting.Voucher': {'fields': ['status', 'amount', 'partner', 'payment_method', 'treasury'], 'create': True, 'delete': True},
    'accounting.Expense': {'fields': ['status', 'expense_account', 'branch'], 'create': True, 'delete': True},
    'accounting.InternalTransfer': {'fields': ['status', 'amount', 'received_amount'], 'create': True, 'delete': True},
    'accounting.Treasury': {'fields': ['name', 'branch', 'account'], 'create': True, 'delete': True},
    'accounting.BankAccount': {'fields': ['name', 'account'], 'create': True, 'delete': True},
    'accounting.EWallet': {'fields': ['name', 'account'], 'create': True, 'delete': True},
    'accounting.Account': {'fields': ['code', 'name', 'parent', 'is_active'], 'create': True, 'delete': True},
    'accounting.Cheque': {'fields': ['status', 'amount', 'due_date'], 'create': True, 'delete': True},
    'partners.Partner': {'fields': ['name', 'partner_type', 'phone', 'mobile', 'credit_limit', 'opening_balance',
                                    'opening_balance_type', 'sector', 'is_active'], 'create': True, 'delete': True},
    'pricing.PriceList': {'fields': ALL, 'create': True, 'delete': True},
    'pricing.PriceListItem': {'fields': ALL, 'create': True, 'delete': True},
    'core.SystemSetting': {'fields': ALL, 'create': False, 'delete': False},
    'core.Branch': {'fields': ALL, 'create': True, 'delete': True},
    'core.Employee': {'fields': ['role', 'branch', 'base_salary', 'hourly_rate', 'deduction_per_hour',
                                 'overtime_per_hour', 'shift_start_time', 'shift_end_time'], 'create': True, 'delete': True},
    'auth.User': {'fields': ['username', 'first_name', 'last_name', 'email', 'is_active'], 'create': True, 'delete': True},
    'hr.Attendance': {'fields': ['status', 'check_in', 'check_out', 'notes'], 'create': True, 'delete': True,
                      # the employee's own location check-in is not an edit
                      'only': lambda obj: getattr(obj, 'source', '') == 'MANUAL'},
    'hr.Payroll': {'fields': ['is_paid', 'net_salary'], 'create': False, 'delete': True,
                   'skip': lambda old, new: not (old.is_paid != new.is_paid)},
    'hr.EmployeeLoan': {'fields': ['status', 'amount', 'installments_count'], 'create': True, 'delete': True},
}


# ---------------------------------------------------------------------------- request
class AuditMiddleware:
    """Makes the current user / IP available to the signal handlers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request.set(request)
        try:
            return self.get_response(request)
        finally:
            _request.reset(token)


def _current_user():
    request = _request.get()
    user = getattr(request, 'user', None)
    return user if getattr(user, 'is_authenticated', False) else None


def _current_ip(request=None):
    request = request or _request.get()
    if request is None:
        return None
    ip = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')
    return ip or None


# ---------------------------------------------------------------------------- values
def _fields(model, config):
    if config['fields'] == ALL:
        return [f for f in model._meta.concrete_fields if f.name not in NOISE]
    names = set(config['fields'])
    return [f for f in model._meta.concrete_fields if f.name in names]


def _plain(value):
    if value is None:
        return None
    if isinstance(value, decimal.Decimal):
        return format(value.normalize(), 'f') if value == value.to_integral() else str(value)
    if isinstance(value, (datetime.datetime,)):
        from django.utils import timezone
        return timezone.localtime(value).strftime('%Y-%m-%d %H:%M') if timezone.is_aware(value) else value.strftime('%Y-%m-%d %H:%M')
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()[:16]
    if isinstance(value, bool):
        return 'نعم' if value else 'لا'
    return str(value)


def _display(obj, field):
    """Readable value of ``field``: choice label, related object's name, formatted number."""
    if field.is_relation:
        fk_id = getattr(obj, field.attname)
        if fk_id is None:
            return None
        try:
            return str(getattr(obj, field.name))
        except Exception:
            return str(fk_id)
    value = getattr(obj, field.attname)
    if field.choices and value is not None:
        return str(dict(field.flatchoices).get(value, value))
    return _plain(value)


def _raw(obj, field):
    return getattr(obj, field.attname)


def _write(action, instance, changes=None, user=None, request=None, repr_text=None):
    from django.contrib.contenttypes.models import ContentType
    from django.db import connection, transaction
    from .models import AuditLog
    if getattr(connection, 'schema_name', None) == 'public':
        return   # platform (public) schema: no tenant audit table
    try:
        with transaction.atomic():   # own savepoint: a failure here cannot break the caller's transaction
            AuditLog.objects.create(
                user=user if user is not None else _current_user(),
                action=action,
                content_type=ContentType.objects.get_for_model(instance) if instance is not None else None,
                object_id=str(getattr(instance, 'pk', '') or ''),
                object_repr=(repr_text if repr_text is not None else str(instance))[:255],
                changes=changes or {},
                ip=_current_ip(request),
            )
    except Exception:   # the audit must never break the action itself
        logger.exception('audit log write failed')


# ---------------------------------------------------------------------------- handlers
def _config(sender):
    return TRACKED.get(sender._meta.label)


def _on_pre_save(sender, instance, raw=False, **kwargs):
    config = _config(sender)
    if config is None or raw or not instance.pk:
        return
    try:
        instance._audit_old = sender._default_manager.filter(pk=instance.pk).first()
    except Exception:
        instance._audit_old = None


def _on_post_save(sender, instance, created=False, raw=False, **kwargs):
    config = _config(sender)
    if config is None or raw:
        return
    if 'only' in config and not config['only'](instance):
        return
    fields = _fields(sender, config)
    if created:
        if config.get('create'):
            changes = {f.verbose_name and str(f.verbose_name) or f.name: [None, _display(instance, f)]
                       for f in fields if _raw(instance, f) not in (None, '')}
            _write('CREATE', instance, changes)
        return
    old = getattr(instance, '_audit_old', None)
    instance._audit_old = None
    if old is None or (config.get('skip') and config['skip'](old, instance)):
        return
    changes = {}
    for f in fields:
        if _raw(old, f) != _raw(instance, f):
            changes[str(f.verbose_name or f.name)] = [_display(old, f), _display(instance, f)]
    if changes:
        _write('UPDATE', instance, changes)


def _on_post_delete(sender, instance, **kwargs):
    config = _config(sender)
    if config is None or not config.get('delete'):
        return
    _write('DELETE', instance)


def _on_permissions_changed(sender, instance, action, pk_set=None, **kwargs):
    """A user's permissions changed (user screen)."""
    if action not in ('post_add', 'post_remove', 'post_clear') or not hasattr(instance, 'username'):
        return
    from django.contrib.auth.models import Permission
    names = []
    if pk_set:
        names = [f'{p.content_type.app_label}.{p.codename}' for p in
                 Permission.objects.filter(pk__in=pk_set).select_related('content_type')]
    label = {'post_add': 'صلاحيات اتضافت', 'post_remove': 'صلاحيات اتشالت', 'post_clear': 'كل الصلاحيات اتشالت'}[action]
    shown = names[:40] + ([f'… و{len(names) - 40} أخرى'] if len(names) > 40 else [])
    _write('UPDATE', instance, {label: [None, '، '.join(shown) or '—']})


def _on_login(sender, request, user, **kwargs):
    _write('LOGIN', user, user=user, request=request)


def _on_logout(sender, request, user, **kwargs):
    if user is not None:
        _write('LOGOUT', user, user=user, request=request)


def connect():
    """Called from CoreConfig.ready()."""
    from django.apps import apps
    from django.contrib.auth.models import User
    from django.contrib.auth.signals import user_logged_in, user_logged_out
    for label in TRACKED:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        pre_save.connect(_on_pre_save, sender=model, dispatch_uid=f'audit-pre-{label}')
        post_save.connect(_on_post_save, sender=model, dispatch_uid=f'audit-post-{label}')
        post_delete.connect(_on_post_delete, sender=model, dispatch_uid=f'audit-del-{label}')
    m2m_changed.connect(_on_permissions_changed, sender=User.user_permissions.through, dispatch_uid='audit-perms')
    user_logged_in.connect(_on_login, dispatch_uid='audit-login')
    user_logged_out.connect(_on_logout, dispatch_uid='audit-logout')
