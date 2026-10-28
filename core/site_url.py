"""Canonical public-origin helpers shared by settings and template context.

PythonAnywhere exposes the app at ``<account>.pythonanywhere.com``. A bare
``pythonanywhere.com`` origin is never the deployed app: it is the hosting
provider's root domain. Correcting that mistake here keeps robots.txt, the
sitemap, canonical tags and the manifest pointing at the real host even when
an environment variable was set to the bare domain.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

PYTHONANYWHERE_ROOT_HOSTS = frozenset({'pythonanywhere.com', 'www.pythonanywhere.com'})
PYTHONANYWHERE_FALLBACK_DOMAIN = 'dashawy.pythonanywhere.com'


def _host(value: str) -> str:
    raw = (value or '').strip()
    if not raw:
        return ''
    if '://' not in raw:
        raw = 'https://' + raw
    try:
        return (urlparse(raw).hostname or '').lower().rstrip('.')
    except ValueError:
        return ''


def _pythonanywhere_domain() -> str:
    """The actual app subdomain, preferring the host's own environment."""
    for candidate in (
        os.environ.get('PYTHONANYWHERE_DOMAIN', ''),
        PYTHONANYWHERE_FALLBACK_DOMAIN,
    ):
        host = _host(candidate)
        if host and host not in PYTHONANYWHERE_ROOT_HOSTS:
            return host
    return PYTHONANYWHERE_FALLBACK_DOMAIN


def normalize_site_url(value: str) -> str:
    """Return a usable canonical origin, never the bare PythonAnywhere root."""
    raw = (value or '').strip().rstrip('/')
    if not raw:
        return ''
    if _host(raw) in PYTHONANYWHERE_ROOT_HOSTS:
        return 'https://' + _pythonanywhere_domain()
    return raw
