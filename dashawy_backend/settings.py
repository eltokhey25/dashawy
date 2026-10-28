"""
Django settings for the Dashawy backend.

The project reads the legacy SQLite schema through the ``core`` app and serves
both the JSON API and the existing static frontend. Production behavior is
driven by environment variables (see ``.env.example``); debug mode defaults to
off and a missing production secret is a startup error.
"""

import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from core.site_url import normalize_site_url

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

# The test runner sets DEBUG=False after settings import. Detect it here so the
# test client is not redirected to HTTPS by SECURE_SSL_REDIRECT; production
# and `check --deploy` never take this branch.
RUNNING_TESTS = (
    'test' in sys.argv
    or 'pytest' in Path(sys.argv[0]).name
    or os.environ.get('PYTEST_CURRENT_TEST') is not None
)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DJANGO_DEBUG', 'False') == 'True'

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if not DEBUG and not RUNNING_TESTS:
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is not True.'
        )
    SECRET_KEY = 'django-insecure-development-only-change-me-before-deploying'

# ---------------------------------------------------------------------------
# Public site configuration (domain-agnostic)
# ---------------------------------------------------------------------------
#
# SITE_URL is the single source of truth for the canonical domain. Changing it
# (and the matching env values below) is all a custom-domain move requires; see
# the final report for the Google Cloud Console origins.

def _default_site_url() -> str:
    """Derive a sensible origin without embedding a domain in the source.

    Production sets ``SITE_URL`` in the environment. PythonAnywhere also
    exports ``PYTHONANYWHERE_DOMAIN``, so a forgotten variable still produces
    the correct canonical URLs; local development falls back to localhost.
    """
    domain = os.environ.get('PYTHONANYWHERE_DOMAIN', '').strip()
    if domain:
        return 'https://' + domain
    return 'http://localhost:8000'


SITE_URL = normalize_site_url(
    os.environ.get('SITE_URL') or _default_site_url()
)
SITE_HOST = urlparse(SITE_URL).netloc if '://' in SITE_URL else SITE_URL
SITE_ORIGIN = (
    SITE_URL
    if '://' in SITE_URL
    else ('https://' + SITE_URL if SITE_URL else '')
)

BUSINESS_PHONE = os.environ.get('BUSINESS_PHONE', '').strip()
BUSINESS_WHATSAPP = os.environ.get('BUSINESS_WHATSAPP', '').strip()
BUSINESS_EMAIL = os.environ.get('BUSINESS_EMAIL', '').strip()
SOCIAL_FACEBOOK = os.environ.get('SOCIAL_FACEBOOK', '').strip()
SOCIAL_INSTAGRAM = os.environ.get('SOCIAL_INSTAGRAM', '').strip()
SOCIAL_TIKTOK = os.environ.get('SOCIAL_TIKTOK', '').strip()

# The per-request commission column remains authoritative for historical
# requests; this is only the default applied to new ones. The value in the
# admin-editable settings table wins when the env var is absent.
COMMISSION_RATE = os.environ.get('COMMISSION_RATE', '20').strip() or '20'

# Google Sign-In. Leave empty to hide the button entirely.
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()

# Optional data-protection contact shown in the legal pages.
PRIVACY_EMAIL = os.environ.get('PRIVACY_EMAIL', '').strip() or BUSINESS_EMAIL

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        'DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1'
    ).split(',')
    if host.strip()
]
# A domain switch must not produce DisallowedHost before the ops team remembers
# to edit DJANGO_ALLOWED_HOSTS. SITE_HOST is always accepted in addition.
if SITE_HOST and SITE_HOST not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(SITE_HOST)
if 'testserver' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('testserver')


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third party
    'axes',
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    # Local
    'core',
]

MIDDLEWARE = [
    # Axes must be first so lockouts are enforced before any request reaches a
    # view or another middleware that could short-circuit the response.
    'axes.middleware.AxesMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'core.middleware.SecurityHeadersMiddleware',
    # CorsMiddleware must sit as high as possible, and in particular before
    # CommonMiddleware so CORS headers survive redirects and error responses.
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'dashawy_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        # Renderable pages/partials live outside the static asset tree so a
        # static-file mapping can never expose raw Django template tags.
        # ``frontend/`` keeps only CSS, JS and images; it stays in DIRS so any
        # not-yet-moved template continues to resolve during the transition.
        'DIRS': [
            BASE_DIR / 'core' / 'templates' / 'frontend',
            BASE_DIR / 'frontend',
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'dashawy_backend.wsgi.application'


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
#
# Point Django at the SQLite file the Express app has been using, so the data
# already in production is visible to the new backend. The tables are managed
# by the legacy schema; `core` models map onto them with matching db_table
# names and the initial migration is applied with --fake-initial.

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'database' / 'fixrequest.db',
        # The Node app keeps the database in WAL mode and writes concurrently
        # with the Django process during the migration window.
        'OPTIONS': {
            'timeout': 20,
        },
    }
}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
#
# The legacy users table stores its own password_hash (bcryptjs in the Node
# app, verified by core/auth_backend.py), so Django's auth User model is kept
# separate and is only used for the admin site. core.User is the application
# account.

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Legacy passwords are bcryptjs `$2a$/$2b$` hashes in users.password_hash.
# BcryptAuthBackend verifies those; ModelBackend stays last so the Django admin
# (which still uses auth.User) keeps working. Axes is first so it can reject a
# locked-out identifier before either password backend is consulted.
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'core.auth_backend.BcryptAuthBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# django-axes: five failures for a username or source IP trigger a 15-minute
# cooloff. The LoginView explicitly resets the counters after a successful
# password check because this project authenticates against core.User rather
# than calling Django's login().
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']
AXES_RESET_ON_SUCCESS = True
AXES_ENABLED = True
# Keep the API's flattened error shape on lockout instead of Axes' HTML page.
AXES_LOCKOUT_CALLABLE = 'core.axes.lockout_response'


# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    # SimpleJWT's stock JWTAuthentication resolves users through
    # settings.AUTH_USER_MODEL (django.contrib.auth.User). Dashawy's accounts
    # live in core.User, so the subclass below re-targets that lookup while
    # reusing all of SimpleJWT's token validation.
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'core.authentication.DashawyJWTAuthentication',
    ],
    # Secure by default: only the public auth endpoints opt out with AllowAny.
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    # The legacy rows store 'YYYY-MM-DD HH:MM:SS' (SQLite datetime('now'));
    # keep the wire format identical so existing JS date parsing is unchanged.
    'DATETIME_FORMAT': '%Y-%m-%d %H:%M:%S',
    # The legacy frontend reads `error` (and optionally `code`), not DRF's
    # `detail` / field-error dictionary.
    'EXCEPTION_HANDLER': 'core.exceptions.api_exception_handler',
}


def _jwt_lifetime(variable, default):
    """Parse a Node-style duration ('7d', '12h', '30m', '45s') from the env."""
    raw = os.environ.get(variable, '').strip()
    match = re.fullmatch(r'(\d+)\s*([smhd])', raw)
    if not match:
        return default
    value, unit = int(match.group(1)), match.group(2)
    units = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days'}
    return timedelta(**{units[unit]: value})


# ---------------------------------------------------------------------------
# SimpleJWT
# ---------------------------------------------------------------------------
#
# core.User is deliberately NOT AUTH_USER_MODEL: it is a plain legacy table
# with its own password_hash, no Django permission tables and no auth manager.
# Making it the auth model would force auth/admin migrations and schema changes
# for no Phase 2 benefit. Instead, DashawyJWTAuthentication pins the token
# subject to core.User.id and the custom backend verifies the legacy hash.
#
# SIGNING_KEY reuses JWT_SECRET so a token minted by Django during the cutover
# is signed with the same secret the Express app already trusts. Tokens minted
# by Node cannot be validated by Django (they carry no `token_type` claim), but
# the frontend simply logs in again when it first talks to Django.
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': _jwt_lifetime('JWT_EXPIRES_IN', timedelta(days=7)),
    'REFRESH_TOKEN_LIFETIME': _jwt_lifetime('JWT_REFRESH_EXPIRES_IN', timedelta(days=7)),
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': os.environ.get('JWT_SECRET') or SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    # The Express app used this claim name (logged in as `userId`); keeping it
    # lets Django-issued tokens be read by any remaining Node code.
    'USER_ID_CLAIM': 'userId',
    'CHECK_USER_IS_ACTIVE': False,  # DashawyJWTAuthentication checks `blocked`.
}


# ---------------------------------------------------------------------------
# CORS / CSRF
# ---------------------------------------------------------------------------
#
# The frontend is normally served same-origin by Django, so CORS is only needed
# for split-port development. Only explicit origins are accepted; there is no
# wildcard fallback.

CORS_ALLOW_ALL_ORIGINS = False
_default_cors = 'http://localhost:8000,' + SITE_ORIGIN
CORS_ALLOWED_ORIGINS = [
    origin.strip().rstrip('/')
    for origin in os.environ.get('CORS_ALLOWED_ORIGINS', _default_cors).split(',')
    if origin.strip()
]

# Django's CSRF middleware stays active (see MIDDLEWARE). These origins are
# trusted for the Django-rendered admin site and any future cookie-authenticated
# form. The JSON API itself uses Bearer JWTs, not CSRF-protected cookies.
_default_csrf = 'http://localhost:8000,' + SITE_ORIGIN
CSRF_TRUSTED_ORIGINS = [
    origin.strip().rstrip('/')
    for origin in os.environ.get(
        'CSRF_TRUSTED_ORIGINS', _default_csrf
    ).split(',')
    if origin.strip()
]


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
#
# Development keeps these off so runserver works over plain HTTP. Production
# enables the full set. SECURE_SSL_REDIRECT is skipped under the test runner so
# the test client's HTTP requests are not turned into 301 redirects.

SECURE_SSL_REDIRECT = not DEBUG and not RUNNING_TESTS
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_BROWSER_XSS_FILTER = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = not DEBUG
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
X_FRAME_OPTIONS = 'DENY'
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
# PythonAnywhere terminates HTTPS at its proxy and forwards the original
# scheme; without this Django would see every request as plain HTTP.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = 'ar'

TIME_ZONE = 'UTC'

USE_I18N = True

# The legacy schema stores datetimes as naive UTC text ('YYYY-MM-DD HH:MM:SS',
# SQLite's datetime('now')). Keeping USE_TZ off means Django reads and writes
# that same format instead of injecting timezone offsets the Node app would
# then have to parse.
USE_TZ = False


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'

# The frontend directory holds the CSS, JS and image assets as well as the
# HTML pages. Registering it lets `collectstatic` (and the staticfiles finder)
# see the same files the catch-all frontend view serves during development.
STATICFILES_DIRS = [BASE_DIR / 'frontend']

# Private uploaded documents (the legacy app used ``uploads/``). There is
# deliberately no MEDIA_URL or URL route for this tree: technician identity
# documents must be streamed by an authenticated view, never served as public
# static content.
MEDIA_ROOT = BASE_DIR / 'uploads'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
