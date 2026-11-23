"""
Tenant-namespaced usernames.

``auth_user`` lives in the shared (public) schema, so usernames are globally
unique: if tenant A could create "ahmed" and get "username already exists",
it would learn that "ahmed" is used by some other tenant. Users created by a
tenant admin are therefore stored internally as ``<schema>__<entered name>``
(schema lowercased). The entered name is what the UI shows and what people
type at login.

In single-instance SQLite mode (django_tenants stripped) and in the public
schema there is no prefix, so behaviour is unchanged there.

Existing (legacy) users are never renamed: login falls back to the raw name.
"""
from django.db import connection

SEPARATOR = '__'


def current_schema_name():
    """Current tenant schema, or '' when not multi-tenant / public schema."""
    schema = getattr(connection, 'schema_name', None) or ''
    if not schema:
        return ''
    try:
        from django_tenants.utils import get_public_schema_name
        public = get_public_schema_name()
    except Exception:
        public = 'public'
    if schema == public:
        return ''
    return schema.lower()


def tenant_username_prefix():
    schema = current_schema_name()
    return f'{schema}{SEPARATOR}' if schema else ''


def namespace_username(entered):
    """Internal username for a name typed in the current tenant."""
    entered = (entered or '').strip()
    prefix = tenant_username_prefix()
    if prefix and entered.lower().startswith(prefix):
        # Already namespaced (e.g. re-submitted value): don't double-prefix.
        return prefix + entered[len(prefix):]
    return prefix + entered


def display_username(value):
    """
    Name to show in the UI for a User or a raw username string: strips the
    current tenant's prefix. Legacy (un-prefixed) names are shown unchanged.
    """
    if value is None:
        return ''
    username = value if isinstance(value, str) else getattr(value, 'username', '') or ''
    prefix = tenant_username_prefix()
    if prefix and username.lower().startswith(prefix):
        return username[len(prefix):]
    return username
