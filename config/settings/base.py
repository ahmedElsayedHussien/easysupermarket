"""
EasySupermarket - Base Django Settings
Multi-Tenant Supermarket ERP using django-tenants (PostgreSQL schemas)
"""
from pathlib import Path
import os
import logging
import logging.handlers  # noqa: F401  (RotatingFileHandler used in LOGGING)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# =============================================================================
# DJANGO-TENANTS CONFIGURATION
# =============================================================================

SHARED_APPS = [
    # django-tenants MUST be first
    'django_tenants',

    # Django built-ins that must live in the public schema
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.admin',

    # Third-party shared apps
    'mptt',
    'rest_framework',
    'corsheaders',
    # Brute-force login protection. Shared (public schema) because auth_user is
    # shared: failed attempts / lockouts are global across all tenants.
    'axes',

    # Our public apps
    'apps.public.tenants',
    'apps.public.subscriptions',
]

TENANT_APPS = [
    'django.contrib.admin',
    # Per-tenant apps (each has its own schema tables)
    'apps.tenant.core',
    'apps.tenant.accounting',
    'apps.tenant.inventory',
    'apps.tenant.partners',
    'apps.tenant.invoicing',
    'apps.tenant.einvoicing',
    'apps.tenant.rentals',
    'apps.tenant.maintenance',
    'apps.tenant.hr',
    'apps.tenant.pricing',
]

# INSTALLED_APPS must be SHARED_APPS + unique TENANT_APPS
INSTALLED_APPS = list(SHARED_APPS) + [
    app for app in TENANT_APPS if app not in SHARED_APPS
]

TENANT_MODEL = 'tenants.Tenant'
TENANT_DOMAIN_MODEL = 'tenants.Domain'

ROOT_URLCONF = 'config.urls'
PUBLIC_SCHEMA_URLCONF = 'config.urls_public'

DATABASE_ROUTERS = ['django_tenants.routers.TenantSyncRouter']

# =============================================================================
# MIDDLEWARE
# =============================================================================

MIDDLEWARE = [
    # TenantMainMiddleware MUST be first
    'django_tenants.middleware.main.TenantMainMiddleware',

    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # django-axes: must come after AuthenticationMiddleware; returns the
    # lockout response when a login attempt is blocked.
    'axes.middleware.AxesMiddleware',
    # MessageMiddleware before BranchMiddleware so BranchMiddleware can queue
    # the "account not linked to a branch" warning.
    'django.contrib.messages.middleware.MessageMiddleware',
    'apps.tenant.core.middleware.BranchMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django.middleware.locale.LocaleMiddleware',
]


ROOT_URLCONF = 'config.urls'

# =============================================================================
# TEMPLATES
# =============================================================================

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.i18n',
                'django.template.context_processors.media',
                'django.template.context_processors.static',
                'django.template.context_processors.tz',
                'apps.tenant.core.context_processors.global_context',
            ],
            # |display_username / |display_name available in every template
            # without {% load %} (strips the internal tenant username prefix).
            'builtins': ['apps.tenant.core.templatetags.user_tags'],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# =============================================================================
# STATIC & MEDIA FILES
# =============================================================================

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# STORAGES replaces STATICFILES_STORAGE (removed in Django 5.1; supported since 4.2).
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'},
}

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# =============================================================================
# AUTH SETTINGS
# =============================================================================

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# AxesStandaloneBackend must be first: it blocks authentication for locked-out
# username+IP pairs before the real backend checks the password.
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# =============================================================================
# DJANGO-AXES (brute-force protection)  -- reset: manage.py axes_reset
# =============================================================================

AXES_ENABLED = True
# Read per tenant from SystemSetting.login_max_attempts / login_lockout_minutes
# (defaults 5 attempts / 15 minutes when the table is unavailable, e.g. public schema).
AXES_FAILURE_LIMIT = 'apps.tenant.core.settings_helpers.axes_failure_limit'
AXES_COOLOFF_TIME = 'apps.tenant.core.settings_helpers.axes_cooloff_time'
AXES_LOCKOUT_PARAMETERS = [['username', 'ip_address']]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = 'auth/lockout.html'
AXES_VERBOSE = False

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'

# =============================================================================
# INTERNATIONALIZATION
# =============================================================================

LANGUAGE_CODE = 'ar'
TIME_ZONE = 'Africa/Cairo'
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ('ar', 'العربية'),
    ('en', 'English'),
]

LOCALE_PATHS = [BASE_DIR / 'locale']
USE_THOUSAND_SEPARATOR = True

# =============================================================================
# DEFAULT PRIMARY KEY
# =============================================================================

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# =============================================================================
# REST FRAMEWORK
# =============================================================================

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

# =============================================================================
# CORS (for API access if needed)
# =============================================================================

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = []  # Populated in environment-specific settings

# =============================================================================
# SESSION SETTINGS
# =============================================================================

SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_AGE = 60 * 60 * 12  # 12 hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# =============================================================================
# LOGGING
# =============================================================================

LOG_DIR = BASE_DIR / 'logs'
os.makedirs(LOG_DIR, exist_ok=True)


class _FileLevelFilter(logging.Filter):
    """
    One shared RotatingFileHandler (two handlers on the same file break
    rotation on Windows). The noisy 'django' logger only writes WARNING+ to
    the file (console still gets INFO); 'django.security' keeps INFO.
    """
    def filter(self, record):
        name = record.name
        if (name == 'django' or name.startswith('django.')) and not name.startswith('django.security'):
            return record.levelno >= logging.WARNING
        return True


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{levelname}] {asctime} {name} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '[{levelname}] {message}',
            'style': '{',
        },
    },
    'filters': {
        'file_level': {'()': _FileLevelFilter},
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOG_DIR / 'easysupermarket.log'),
            'maxBytes': 5 * 1024 * 1024,  # 5 MB
            'backupCount': 5,
            'encoding': 'utf-8',
            'delay': True,
            'formatter': 'verbose',
            'filters': ['file_level'],
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'axes': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# =============================================================================
# EMAIL (placeholder - override in environment settings)
# =============================================================================

EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = 'noreply@easysupermarket.local'
