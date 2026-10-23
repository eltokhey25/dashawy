"""
Django settings for the Dashawy backend.

Phase 1 of the Node.js -> Django migration: the project is wired up to the
existing SQLite database and the legacy schema is exposed through the `core`
app's models. The Node/Express API remains in `backend/` and is still the
running backend until later phases replace it.
"""

import os
import re
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Reuse the repository's existing .env file (it currently holds the Node
# variables; Django-specific keys can be added alongside them).
load_dotenv(BASE_DIR / '.env')


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-development-only-change-me-before-deploying',
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DJANGO_DEBUG', '1').lower() in ('1', 'true', 'yes', 'on')

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        'DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]'
    ).split(',')
    if host.strip()
]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    # Local
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
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
        'DIRS': [],
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
# (which still uses auth.User) keeps working.
AUTHENTICATION_BACKENDS = [
    'core.auth_backend.BcryptAuthBackend',
    'django.contrib.auth.backends.ModelBackend',
]


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
# CORS
# ---------------------------------------------------------------------------
#
# Wide open for Phase 1 so the existing frontend can be pointed at Django
# during testing. Narrow this to CORS_ALLOWED_ORIGINS before production.

CORS_ALLOW_ALL_ORIGINS = True


# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

# The legacy schema stores datetimes as naive UTC text ('YYYY-MM-DD HH:MM:SS',
# SQLite's datetime('now')). Keeping USE_TZ off means Django reads and writes
# that same format instead of injecting timezone offsets the Node app would
# then have to parse.
USE_TZ = False


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
