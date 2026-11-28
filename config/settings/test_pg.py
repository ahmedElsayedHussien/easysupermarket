"""
EasySupermarket - PostgreSQL Multi-Tenant Test Settings

Runs the full test suite against PostgreSQL with django-tenants schemas active:
    python manage.py test --settings=config.settings.test_pg
"""
from decouple import config

from .base import *  # noqa: F401,F403

SECRET_KEY = config(
    'SECRET_KEY',
    default='pg-test-only-key-not-used-in-production-9f8a7b6c5d4e3f2a1b0c',
)
DEBUG = False
ALLOWED_HOSTS = ['*']

DATABASES = {
    'default': {
        'ENGINE': 'django_tenants.postgresql_backend',
        'NAME': config('PG_TEST_DB_NAME', default=config('DB_NAME', default='easysupermarket')).strip(),
        'USER': config('DB_USER', default='postgres').strip(),
        'PASSWORD': config('DB_PASSWORD', default='postgres').strip(),
        'HOST': config('DB_HOST', default='127.0.0.1').strip(),
        'PORT': config('DB_PORT', default='5432').strip(),
        'OPTIONS': {
            'connect_timeout': 10,
        },
    }
}

TEST_RUNNER = 'apps.public.tenants.test_runner.TenantAwareTestRunner'

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'easysupermarket-test-pg',
    }
}

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
AXES_ENABLED = False

SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'null': {'class': 'logging.NullHandler'}},
    'root': {'handlers': ['null'], 'level': 'CRITICAL'},
}
