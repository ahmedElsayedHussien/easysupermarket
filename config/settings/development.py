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
# DATABASE (PostgreSQL with django-tenants backend)
# =============================================================================

DATABASES = {
    'default': {
        'ENGINE': 'django_tenants.postgresql_backend',
        'NAME': config('DB_NAME', default='easysupermarket'),
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
        'OPTIONS': {
            'connect_timeout': 10,
        },
    }
}

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
