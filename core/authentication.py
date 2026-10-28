"""
DRF authentication class backed by core.User and SimpleJWT.

SimpleJWT's stock JWTAuthentication resolves the token subject through
``settings.AUTH_USER_MODEL`` (django.contrib.auth.User). Dashawy accounts live
in the legacy ``users`` table, so this subclass keeps all of SimpleJWT's
signature/expiry validation and only re-targets the user lookup.
"""

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.settings import api_settings

from .exceptions import unauthorized
from .models import User
from .tokens import PASSWORD_FINGERPRINT_CLAIM, password_fingerprint


class DashawyJWTAuthentication(JWTAuthentication):
    """Authenticate ``Authorization: Bearer <access>`` against core.User."""

    def get_user(self, validated_token):
        try:
            user_id = validated_token[api_settings.USER_ID_CLAIM]
        except KeyError as exc:
            raise InvalidToken(
                'الرمز لا يحتوي على هوية مستخدم صالحة'
            ) from exc

        try:
            user = User.objects.get(**{api_settings.USER_ID_FIELD: user_id})
        except (User.DoesNotExist, ValueError, TypeError) as exc:
            raise AuthenticationFailed('الحساب غير موجود') from exc

        if user.blocked:
            raise unauthorized('تم إيقاف حسابك. تواصل مع الإدارة.')

        # A password reset (or a voluntary password change) rotates the hash;
        # tokens carrying the previous fingerprint must stop working even
        # though their signature and expiry are still valid. Tokens minted
        # before this claim existed are refused for the same reason.
        if validated_token.get(PASSWORD_FINGERPRINT_CLAIM) != password_fingerprint(
            user
        ):
            raise AuthenticationFailed(
                'انتهت صلاحية الجلسة بعد تغيير كلمة المرور. سجّل الدخول من جديد.',
                code='password_changed',
            )

        return user
