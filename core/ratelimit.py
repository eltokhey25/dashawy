"""Lightweight fixed-window rate limiting for the public API.

The project deliberately does not add a new dependency. ``django-axes`` already
handles password lockouts; this helper covers the endpoints Axes does not see
(registration, Google sign-in, guest orders, profile completion).

The cache backend is Django's default (LocMem in this project). That is
per-process, so on a multi-worker host the effective limit is per worker; it is
still enough to stop a single script hammering one endpoint, and the persistent
``GuestSubmissionLog`` adds a database-backed per-phone limit for guest orders.
"""

from __future__ import annotations

import ipaddress
import time

from django.core.cache import cache

from .exceptions import ApiError


def _usable_ip(value: str) -> str:
    """Return a normalised IP string, or ``''`` when the value is unusable."""
    candidate = (value or '').strip()
    if not candidate:
        return ''
    # X-Forwarded-For may carry "1.2.3.4:5678" or "[::1]:443".
    if candidate.startswith('['):
        candidate = candidate[1:].split(']', 1)[0]
    elif candidate.count(':') == 1:
        candidate = candidate.split(':', 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ''


def client_ip(request) -> str:
    """Best-effort client IP, safe behind PythonAnywhere's reverse proxy.

    ``REMOTE_ADDR`` is authoritative when it is a public address, because a
    direct client cannot fake it. When it is loopback/private, the app is
    behind a proxy (PythonAnywhere included), so the right-most
    ``X-Forwarded-For`` entry — the one appended by that trusted proxy — is
    used instead. A client-supplied value to its left cannot override it.
    """
    meta = getattr(request, 'META', {})
    remote = (meta.get('REMOTE_ADDR') or '').strip()

    proxied = False
    try:
        parsed = ipaddress.ip_address(remote)
        # ``is_global`` is False for loopback, private, link-local, CGNAT and
        # documentation ranges — all cases where a proxy is in front of us.
        proxied = not parsed.is_global
    except ValueError:
        proxied = True

    if proxied:
        forwarded = meta.get('HTTP_X_FORWARDED_FOR') or ''
        for part in reversed(forwarded.split(',')):
            address = _usable_ip(part)
            if address:
                return address[:64]

    return (remote or 'unknown')[:64]


def throttle(request, scope: str, limit: int, window_seconds: int = 3600,
             identifier: str = '') -> None:
    """Raise a 429 ``ApiError`` once ``limit`` calls are exceeded.

    The key includes a time bucket, so counters expire automatically and no
    cleanup job is needed.
    """
    if limit <= 0:
        return
    bucket = int(time.time() // max(window_seconds, 1))
    who = (identifier or client_ip(request)).strip()[:120]
    key = f'rl:{scope}:{who}:{bucket}'
    current = cache.get(key, 0)
    if current >= limit:
        raise ApiError(
            'عدد المحاولات كبير جدًا. حاول تاني بعد شوية.',
            429,
        )
    # add() then set() is not atomic on every backend, but a couple of extra
    # requests in a race are not security-relevant for these limits.
    cache.set(key, current + 1, window_seconds + 5)
