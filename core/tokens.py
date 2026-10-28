"""JWT refresh-token issuance for the legacy ``core.User`` accounts.

SimpleJWT's stock ``RefreshToken.for_user()`` stores the issued token in
``OutstandingToken`` with a foreign key to ``settings.AUTH_USER_MODEL``
(``django.contrib.auth.User``). Dashawy accounts live in ``core.User``, which is
deliberately not the auth model, so passing one to ``for_user()`` raises
``ValueError``.

This subclass keeps the standard token payload and blacklist bookkeeping, but
records the outstanding token with ``user=None``. Logout can then call
``blacklist()`` normally: it finds the existing row by ``jti`` and adds the
``BlacklistedToken`` entry.
"""

import hashlib

from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch

# Every Dashawy token carries a fingerprint of the account's current password
# hash. Authentication compares it with the live row, so a password change —
# including an admin-assisted reset — instantly invalidates every token minted
# with the old password. The claim is a one-way digest; it never contains the
# password or its hash.
PASSWORD_FINGERPRINT_CLAIM = 'pwd_fp'


def password_fingerprint(user) -> str:
    """Stable digest of the user's stored password hash."""
    raw = getattr(user, 'password_hash', None)
    if not raw:
        raw = getattr(user, 'password', '') or ''
    return hashlib.sha256(str(raw).encode('utf-8')).hexdigest()


class DashawyRefreshToken(RefreshToken):
    """A refresh token whose outstanding row is not tied to auth.User."""

    @classmethod
    def for_user(cls, user):
        token = cls()
        token[api_settings.USER_ID_CLAIM] = str(
            getattr(user, api_settings.USER_ID_FIELD)
        )
        token[PASSWORD_FINGERPRINT_CLAIM] = password_fingerprint(user)

        OutstandingToken.objects.create(
            user=None,
            jti=token[api_settings.JTI_CLAIM],
            token=str(token),
            created_at=token.current_time,
            expires_at=datetime_from_epoch(token['exp']),
        )
        return token
