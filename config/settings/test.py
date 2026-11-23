"""
EasySupermarket - Test Settings

Runs the test suite on an in-memory SQLite database WITHOUT django_tenants,
so tests never touch the real PostgreSQL database configured in .env.

    venv\Scripts\python.exe manage.py test --settings=config.settings.test
"""
import os

# Make sure nothing downstream that reads DB_ENGINE picks PostgreSQL.
os.environ['DB_ENGINE'] = 'sqlite'

from .base import *  # noqa: F401,F403

SECRET_KEY = 'test'
DEBUG = False
ALLOWED_HOSTS = ['*']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
        'TEST': {'NAME': ':memory:'},
    }
}

# Mirror the sqlite branch of development.py: drop django_tenants entirely.
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != 'django_tenants']  # noqa: F405
MIDDLEWARE = [m for m in MIDDLEWARE if 'django_tenants' not in m]  # noqa: F405
DATABASE_ROUTERS = []
ROOT_URLCONF = 'config.urls'

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'easysupermarket-test',
    }
}

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# Brute-force protection is switched on only in the dedicated axes test.
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
