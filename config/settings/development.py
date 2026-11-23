"""
EasySupermarket - Development Settings
Reads sensitive config from .env file using python-decouple
"""
from .base import *
from decouple import config

# =============================================================================
# CORE
# =============================================================================

DEBUG = config('DEBUG', default=True, cast=bool)

SECRET_KEY = config(
    'SECRET_KEY',
    default='dev-secret-key-change-in-production-please-do-not-use-in-production'
)

ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1'
).split(',')

# =============================================================================
# DATABASE CONFIGURATION (Controlled via .env DB_ENGINE: 'sqlite' or 'postgresql')
# =============================================================================

DB_ENGINE = config('DB_ENGINE', default='sqlite').strip().lower()

if DB_ENGINE in ['postgresql', 'postgres', 'pgsql', 'pg']:
    # PostgreSQL with django-tenants (Multi-Tenant)
    DATABASES = {
        'default': {
            'ENGINE': 'django_tenants.postgresql_backend',
            'NAME': config('DB_NAME', default='easysupermarket').strip(),
            'USER': config('DB_USER', default='postgres').strip(),
            'PASSWORD': config('DB_PASSWORD').strip(),
            'HOST': config('DB_HOST', default='localhost').strip(),
            'PORT': config('DB_PORT', default='5432').strip(),
            'OPTIONS': {
                'connect_timeout': 10,
            },
        }
    }
else:
    # SQLite3 (Standalone / Single Instance)
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {
                'timeout': 30,
            }
        }
    }
    # -------------------------------------------------------------------------
    # SQLite concurrency note
    # select_for_update() is a silent no-op on SQLite, so row locks used by the
    # stock / sequence / journal code do not protect against lost updates.
    # With the default DEFERRED transactions two writers can both read the
    # same row and the second commit overwrites the first. From Django 5.1,
    # transaction_mode='IMMEDIATE' makes every atomic() block take SQLite's
    # write lock at BEGIN, serialising writers (readers are unaffected in WAL
    # mode). The 30s timeout / busy_timeout below make writers wait instead of
    # failing with "database is locked". On Django < 5.1 the option does not
    # exist and is skipped.
    # -------------------------------------------------------------------------
    import django as _django
    if _django.VERSION >= (5, 1):
        DATABASES['default']['OPTIONS']['transaction_mode'] = 'IMMEDIATE'
    # Auto-adjust for SQLite: remove django_tenants and its schema dependencies
    INSTALLED_APPS = [app for app in INSTALLED_APPS if app != 'django_tenants']
    MIDDLEWARE = [m for m in MIDDLEWARE if 'django_tenants' not in m]
    DATABASE_ROUTERS = []
    ROOT_URLCONF = 'config.urls'

# =============================================================================
# CACHING (simple local-memory for dev)
# =============================================================================

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'easysupermarket-dev',
    }
}

# =============================================================================
# EMAIL (console output in dev)
# =============================================================================

EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# =============================================================================
# DEBUG TOOLBAR (optional, uncomment if installed)
# =============================================================================
# if DEBUG:
#     INSTALLED_APPS += ['debug_toolbar']
#     MIDDLEWARE.insert(1, 'debug_toolbar.middleware.DebugToolbarMiddleware')
#     INTERNAL_IPS = ['127.0.0.1']

# =============================================================================
# CORS - Allow all origins in development
# =============================================================================

CORS_ALLOW_ALL_ORIGINS = True

# =============================================================================
# SECURITY - Relaxed for local dev
# =============================================================================

SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Auto-tune SQLite on connection (WAL mode + 30s busy timeout + auto indexes)
if DB_ENGINE != 'postgresql':
    from django.db.backends.signals import connection_created
    _sqlite_tuned = False
    def _tune_sqlite(sender, connection, **kwargs):
        global _sqlite_tuned
        if connection.vendor == 'sqlite' and not _sqlite_tuned:
            _sqlite_tuned = True
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA journal_mode=WAL;')
                cursor.execute('PRAGMA synchronous=NORMAL;')
                cursor.execute('PRAGMA busy_timeout=30000;')
                try:
                    cursor.execute('CREATE INDEX IF NOT EXISTS idx_prod_name_fast ON inventory_product(name);')
                    cursor.execute('CREATE INDEX IF NOT EXISTS idx_prod_active_fast ON inventory_product(is_active);')
                    cursor.execute('CREATE INDEX IF NOT EXISTS idx_batch_fast ON inventory_inventorybatch(quantity_remaining, created_at);')
                    cursor.execute('CREATE INDEX IF NOT EXISTS idx_batch_prod_qty_fast ON inventory_inventorybatch(product_id, quantity_remaining);')
                except Exception:
                    pass
    connection_created.connect(_tune_sqlite)
