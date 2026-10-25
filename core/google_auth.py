"""Server-side verification of Google Identity Services ID tokens.

No extra dependency: PyJWT already ships with SimpleJWT, and Google's public
JWKS is fetched with ``urllib`` and cached for an hour. The browser's claims are
never trusted — signature, issuer, audience, expiry and ``email_verified`` are
all checked here.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import jwt
from django.conf import settings

GOOGLE_CERTS_URL = 'https://www.googleapis.com/oauth2/v3/certs'
GOOGLE_ISSUERS = ('accounts.google.com', 'https://accounts.google.com')
CERT_CACHE_SECONDS = 3600

_lock = threading.Lock()
_certs_cache = {'fetched_at': 0.0, 'keys': {}}


class GoogleTokenError(Exception):
    """A token that must not be trusted."""


def _fetch_certs():
    with urllib.request.urlopen(GOOGLE_CERTS_URL, timeout=8) as response:
        payload = json.loads(response.read().decode('utf-8'))
    keys = {}
    for jwk in payload.get('keys', []):
        kid = jwk.get('kid')
        if kid and jwk.get('kty') == 'RSA':
            keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    if not keys:
        raise GoogleTokenError('تعذر التحقق من حساب جوجل')
    return keys


def _public_keys(force=False):
    now = time.time()
    if force or now - _certs_cache['fetched_at'] > CERT_CACHE_SECONDS or not _certs_cache['keys']:
        with _lock:
            if force or now - _certs_cache['fetched_at'] > CERT_CACHE_SECONDS or not _certs_cache['keys']:
                _certs_cache['keys'] = _fetch_certs()
                _certs_cache['fetched_at'] = now
    return _certs_cache['keys']


def verify_id_token(id_token: str) -> dict:
    """Return verified claims, or raise :class:`GoogleTokenError`."""
    client_id = (getattr(settings, 'GOOGLE_CLIENT_ID', '') or '').strip()
    if not client_id:
        raise GoogleTokenError('تسجيل الدخول بجوجل غير مُفعّل حالياً')
    if not isinstance(id_token, str) or not id_token.strip():
        raise GoogleTokenError('رمز جوجل مطلوب')

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise GoogleTokenError('رمز جوجل غير صالح') from exc

    kid = header.get('kid')
    if not kid:
        raise GoogleTokenError('رمز جوجل غير صالح')

    keys = _public_keys()
    key = keys.get(kid)
    if key is None:
        # Key rotation: refresh once before rejecting.
        try:
            key = _public_keys(force=True).get(kid)
        except GoogleTokenError:
            key = None
    if key is None:
        raise GoogleTokenError('رمز جوجل غير صالح')

    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=['RS256'],
            audience=client_id,
            issuer=list(GOOGLE_ISSUERS),
            options={'require': ['exp', 'iat', 'aud', 'iss', 'sub']},
        )
    except jwt.ExpiredSignatureError as exc:
        raise GoogleTokenError('انتهت صلاحية تسجيل الدخول بجوجل. حاول تاني.') from exc
    except jwt.PyJWTError as exc:
        raise GoogleTokenError('تعذر التحقق من حساب جوجل') from exc

    if not claims.get('sub'):
        raise GoogleTokenError('رمز جوجل غير صالح')
    if claims.get('email_verified') not in (True, 'true', 'True', 1):
        raise GoogleTokenError('لازم تأكيد الإيميل في حساب جوجل أولاً')
    email = (claims.get('email') or '').strip().lower()
    if not email or '@' not in email:
        raise GoogleTokenError('حساب جوجل لا يحتوي على بريد إلكتروني صالح')
    claims['email'] = email
    return claims
