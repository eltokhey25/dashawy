"""Lightweight fixed-window rate limiting for the public API.

The project deliberately does not add a new dependency. ``django-axes`` already
handles password lockouts; this helper covers the endpoints Axes does not see
(registration, Google sign-in, guest orders, profile completion).

Counters live in the ``rate_counters`` table (``RateCounter``): shared by
every serverless instance, and incremented by one guarded UPDATE so a burst
of parallel requests cannot all pass on the same stale count.
"""

from __future__ import annotations

import ipaddress
import time
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F

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
             identifier: str = '', count: bool = True) -> None:
    """Raise a 429 ``ApiError`` once ``limit`` calls are exceeded.

    The key includes a time bucket, so counters expire automatically and no
    cleanup job is needed. With ``count=False`` the limit is only checked, not
    consumed, so a caller can refuse early and charge only failed attempts.
    """
    if limit <= 0:
        return
    from .models import RateCounter, utc_now

    bucket = int(time.time() // max(window_seconds, 1))
    who = (identifier or client_ip(request)).strip()[:120]
    key = f'rl:{scope}:{who}:{bucket}'[:255]

    def refuse():
        raise ApiError('عدد المحاولات كبير جدًا. حاول تاني بعد شوية.', 429)

    if not count:
        current = RateCounter.objects.filter(key=key).values_list('count', flat=True).first()
        if (current or 0) >= limit:
            refuse()
        return

    # Spend one unit only while under the limit, in one statement.
    if RateCounter.objects.filter(key=key, count__lt=limit).update(count=F('count') + 1):
        return
    if RateCounter.objects.filter(key=key).exists():
        refuse()
    try:
        with transaction.atomic():
            RateCounter.objects.create(
                key=key,
                count=1,
                expires_at=utc_now() + timedelta(seconds=window_seconds + 5),
            )
    except IntegrityError:
        # A parallel request created the row first: take the guarded path.
        if not RateCounter.objects.filter(key=key, count__lt=limit).update(
            count=F('count') + 1
        ):
            refuse()
