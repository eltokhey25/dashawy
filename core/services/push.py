"""Web Push notifications to the browsers a user subscribed.

Port of ``backend/services/notifications.js``. The VAPID key pair comes from
``VAPID_PUBLIC_KEY`` / ``VAPID_PRIVATE_KEY`` / ``VAPID_SUBJECT``; without both
keys push is disabled and every send is a no-op. Sending never raises: a
failed push must not turn the action that triggered it into an error.
Subscriptions the push service reports gone (404/410) are deleted.
"""

import json
import logging
import os

from ..models import PushSubscription, utc_now

logger = logging.getLogger(__name__)

MAX_PER_MESSAGE = 10


def _keys():
    public = os.environ.get('VAPID_PUBLIC_KEY', '').strip()
    private = os.environ.get('VAPID_PRIVATE_KEY', '').strip()
    subject = os.environ.get('VAPID_SUBJECT', '').strip() or 'mailto:admin@example.com'
    return public, private, subject


def is_enabled():
    public, private, _ = _keys()
    return bool(public and private)


def public_key():
    return _keys()[0] if is_enabled() else None


# Browsers only ever hand out endpoints on their vendor's push service. Anything
# else is a user-supplied URL the server would then POST to (SSRF, or a slow
# host that stalls every broadcast).
PUSH_HOSTS = (
    'fcm.googleapis.com',
    'android.googleapis.com',
    'updates.push.services.mozilla.com',
    'web.push.apple.com',
    'push.apple.com',
)
PUSH_HOST_SUFFIXES = ('.notify.windows.com', '.push.services.mozilla.com')


def endpoint_allowed(endpoint):
    from urllib.parse import urlparse

    try:
        parsed = urlparse(endpoint)
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and (
        host in PUSH_HOSTS or host.endswith(PUSH_HOST_SUFFIXES)
    )


def save_subscription(user_id, subscription, user_agent=None):
    endpoint = (subscription or {}).get('endpoint')
    keys = (subscription or {}).get('keys') or {}
    if not endpoint or not keys.get('p256dh') or not keys.get('auth'):
        raise ValueError('Invalid subscription')
    if not endpoint_allowed(endpoint) or len(endpoint) > 2000:
        raise ValueError('Unsupported push endpoint')
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            'user_id': user_id,
            'p256dh': keys['p256dh'],
            'auth': keys['auth'],
            'user_agent': (user_agent or '')[:500] or None,
            'last_used_at': utc_now(),
        },
    )


def remove_subscription(user_id, endpoint):
    deleted, _ = PushSubscription.objects.filter(user_id=user_id, endpoint=endpoint).delete()
    return deleted


def send_to_user(user_id, payload):
    """Send ``payload`` (title/body/url/tag) to every browser of ``user_id``."""
    if not user_id or not is_enabled():
        return {'sent': 0, 'failed': 0}
    from pywebpush import WebPushException, webpush

    _, private, subject = _keys()
    body = json.dumps(payload, ensure_ascii=False)
    sent = failed = 0
    for sub in PushSubscription.objects.filter(user_id=user_id)[:MAX_PER_MESSAGE]:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                },
                data=body,
                vapid_private_key=private,
                vapid_claims={'sub': subject},
                timeout=5,
            )
            PushSubscription.objects.filter(pk=sub.pk).update(last_used_at=utc_now())
            sent += 1
        except WebPushException as exc:
            failed += 1
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            if code in (404, 410):
                PushSubscription.objects.filter(pk=sub.pk).delete()
            else:
                logger.warning('Push delivery failed for user %s: %s', user_id, exc)
        except Exception:  # noqa: BLE001 - never break the caller
            failed += 1
            logger.warning('Push delivery failed for user %s', user_id, exc_info=True)
    return {'sent': sent, 'failed': failed}


def notify(user_id, payload):
    try:
        return send_to_user(user_id, payload)
    except Exception:  # noqa: BLE001
        logger.warning('Push notify failed', exc_info=True)
        return {'sent': 0, 'failed': 0}
