"""
bcrypt authentication backend for the legacy ``users`` table.

The Express app hashed passwords with bcryptjs at cost 12. Those hashes are
standard ``$2a$``/``$2b$`` bcrypt strings, so Python's ``bcrypt`` verifies them
directly — no rehash or password reset is needed for existing accounts.

Django's ModelBackend remains in AUTHENTICATION_BACKENDS after this one so the
admin site (which uses auth.User) keeps working.
"""

import bcrypt
from django.contrib.auth.backends import BaseBackend

from .models import User
from .validation import normalize_email, normalize_phone

# bcrypt only looks at the first 72 bytes. bcryptjs silently truncated longer
# inputs; Python bcrypt 5.x raises ValueError instead. Truncating here keeps
# existing over-long legacy passwords working and matches what Node did.
BCRYPT_MAX_BYTES = 72
BCRYPT_ROUNDS = 12


def password_bytes(password):
    """UTF-8 password bytes, truncated to bcrypt's 72-byte window."""
    return str(password).encode('utf-8')[:BCRYPT_MAX_BYTES]


def hash_password(password):
    """Hash a new password exactly as the Node app did (cost 12)."""
    return bcrypt.hashpw(
        password_bytes(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    ).decode('utf-8')


class BcryptAuthBackend(BaseBackend):
    """Authenticate a core.User against its legacy bcrypt password hash."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = kwargs.get('email') or username
        if not identifier or not isinstance(password, str) or not password:
            return None

        identifier = str(identifier).strip()
        if '@' in identifier:
            user = User.objects.filter(email=normalize_email(identifier)).first()
        else:
            phone = normalize_phone(identifier)
            user = User.objects.filter(phone=phone).first() if phone else None

        if user is None or not self.verify_password(user, password):
            return None
        return user

    @staticmethod
    def verify_password(user, password):
        if not user.password_hash:
            return False
        try:
            return bcrypt.checkpw(
                password_bytes(password), user.password_hash.encode('utf-8')
            )
        except (ValueError, TypeError):
            # A malformed/unsupported hash must fail closed, not 500 the login.
            return False

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
