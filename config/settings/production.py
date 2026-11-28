"""
EasySupermarket - Production Django Settings

Hardened settings for PostgreSQL + django-tenants deployment and
`manage.py check --deploy --settings=config.settings.production`.
"""
import sys

from decouple import config
from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

from .base import *  # noqa: F401,F403

DEBUG = False

_IS_MGMT_CHECK = len(sys.argv) >= 2 and sys.argv[1] in ('check', 'test')

_raw_secret = config('SECRET_KEY', default='').strip()
if not _raw_secret:
    if _IS_MGMT_CHECK:
        _raw_secret = get_random_secret_key()
    else:
        raise ImproperlyConfigured('SECRET_KEY must be set in the environment for production.')
SECRET_KEY = _raw_secret

ALLOWED_HOSTS = [
    host.strip()
    for host in config('ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',')
    if host.strip()
]

_raw_db_password = config('DB_PASSWORD', default='').strip()
if not _raw_db_password and not _IS_MGMT_CHECK:
    raise ImproperlyConfigured('DB_PASSWORD must be set in the environment for production.')

DATABASES = {
    'default': {
        'ENGINE': 'django_tenants.postgresql_backend',
        'NAME': config('DB_NAME', default='easysupermarket').strip(),
        'USER': config('DB_USER', default='postgres').strip(),
        'PASSWORD': _raw_db_password,
        'HOST': config('DB_HOST', default='localhost').strip(),
        'PORT': config('DB_PORT', default='5432').strip(),
        'CONN_MAX_AGE': config('DB_CONN_MAX_AGE', default=60, cast=int),
        'OPTIONS': {
            'connect_timeout': 10,
        },
    }
}

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in config('CORS_ALLOWED_ORIGINS', default='').split(',')
    if origin.strip()
]

SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
