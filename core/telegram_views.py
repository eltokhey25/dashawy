"""Telegram linking, the forgot-password code and the webhook Telegram calls.

* ``GET  /api/auth/telegram-link``          — link status for the signed-in user
* ``POST /api/auth/telegram-link``          — a 15-minute deep link for the signed-in user
* ``POST /api/auth/request-otp``            — {identifier}: 6-digit code by Telegram
* ``POST /api/auth/reset-password``         — {identifier, otp, new_password}
* ``POST /api/telegram/webhook/<secret>``   — called by Telegram, never by the site

The code is stored hashed (SHA-256), lasts 10 minutes and is single-use.
Every "does this account exist" question gets the same answer, so the
endpoints cannot be used to enumerate accounts.
"""

import hashlib
import secrets
from datetime import timedelta

from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth_backend import hash_password
from .exceptions import ApiError, bad_request
from .models import Otp, User, utc_now
from .ratelimit import client_ip, throttle
from .services import telegram
from .services.notifications import notify_login, notify_password_changed
from .validation import is_valid_email, normalize_email, normalize_phone, password_policy_error
from .views import (
    _assert_account_can_sign_in,
    _body,
    _clear_failed_logins,
    _issue_token,
    _profile_for,
    _reset_axes,
    _safe_user,
)

OTP_TTL_MINUTES = 10
OTP_COOLDOWN_SECONDS = 60
SENT_MESSAGE = (
    'لو الحساب موجود ومربوط بتليجرام، الكود اتبعت عليه. '
    'لو حسابك مش مربوط بتليجرام، كلّمنا على الواتساب عشان نساعدك.'
)


def _identifier_key(raw):
    """Normalised email or phone used to look the account up (and key the OTP)."""
    value = str(raw or '').strip()
    if not value:
        return None
    if '@' in value:
        return normalize_email(value) if is_valid_email(value) else None
    return normalize_phone(value) or None


def _find_user(key):
    if not key:
        return None
    if '@' in key:
        return User.objects.filter(email=key).first()
    return User.objects.filter(phone=key).first()


def _hash_otp(code):
    return hashlib.sha256(str(code).encode('utf-8')).hexdigest()


class TelegramLinkView(APIView):
    """GET link status; POST a deep link — both for the signed-in account only.

    Linking by a typed phone or email would let anyone attach their own
    Telegram to someone else's account and then reset its password, so the
    link token is always issued for ``request.user`` and nobody else.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {
                'linked': bool(request.user.telegram_chat_id),
                'bot_username': telegram.bot_username(),
                'enabled': telegram.is_configured(),
            }
        )

    def post(self, request):
        if not telegram.is_configured():
            raise ApiError('ربط تليجرام غير متاح حالياً', status.HTTP_503_SERVICE_UNAVAILABLE)
        user = request.user
        throttle(request, 'telegram-link', limit=10, window_seconds=900, identifier=f'user-{user.pk}')
        if user.telegram_chat_id:
            raise ApiError(
                'الحساب ده مربوط بتليجرام بالفعل.', status.HTTP_409_CONFLICT, code='telegram_already_linked'
            )
        token = telegram.create_link_token(user)
        return Response(
            {
                'token': token,
                'expires_in_minutes': telegram.LINK_TTL_MINUTES,
                'bot_username': telegram.bot_username(),
                'deep_link': telegram.deep_link(token),
            }
        )


class RequestOtpView(APIView):
    """POST /api/auth/request-otp — send a password-reset code by Telegram."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        key = _identifier_key(_body(request).get('identifier') or _body(request).get('email'))
        if not key:
            raise bad_request('اكتب البريد الإلكتروني أو رقم الموبايل المسجّل')
        throttle(request, 'otp-request-ip', limit=15, window_seconds=900)
        throttle(request, 'otp-request', limit=5, window_seconds=900, identifier=key)

        user = _find_user(key)
        if user is None or not user.telegram_chat_id:
            # Same answer as a linked account, so nobody can probe for accounts
            # or learn which ones have Telegram.
            return Response({'success': True, 'message': SENT_MESSAGE})

        now = utc_now()
        recent = Otp.objects.filter(
            email=key, created_at__gt=now - timedelta(seconds=OTP_COOLDOWN_SECONDS)
        ).exists()
        if recent:
            raise ApiError(
                'طلبت كود من شوية. استنى دقيقة وحاول تاني.',
                status.HTTP_429_TOO_MANY_REQUESTS,
                code='otp_rate_limited',
            )

        code = f'{secrets.randbelow(900000) + 100000}'
        with transaction.atomic():
            Otp.objects.filter(email=key, used=False).update(used=True)
            row = Otp.objects.create(
                email=key,
                otp_hash=_hash_otp(code),
                expires_at=now + timedelta(minutes=OTP_TTL_MINUTES),
                used=False,
            )
            Otp.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()

        sent = telegram.send_message(
            user.telegram_chat_id,
            f'🔑 كود استعادة كلمة السر في دشاوي: {code}\n'
            f'الكود صالح لمدة {OTP_TTL_MINUTES} دقايق. لو مش إنت اللي طلبته، تجاهل الرسالة.',
        )
        if not sent:
            Otp.objects.filter(pk=row.pk).delete()
            user.refresh_from_db(fields=['telegram_chat_id'])
            if not user.telegram_chat_id:
                # The bot was blocked and the account got unlinked: same
                # answer as above, for the same reason.
                return Response({'success': True, 'message': SENT_MESSAGE})
            raise ApiError(
                'تعذر إرسال الكود حالياً، حاول تاني بعد شوية.',
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({'success': True, 'message': SENT_MESSAGE})


class ResetPasswordView(APIView):
    """POST /api/auth/reset-password — {identifier, otp, new_password}."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = _body(request)
        key = _identifier_key(data.get('identifier') or data.get('email'))
        code = str(data.get('otp') or '').strip()
        if not key or not (code.isdigit() and len(code) == 6):
            raise bad_request('الكود غير صحيح')
        throttle(request, 'otp-verify-ip', limit=20, window_seconds=900)
        throttle(request, 'otp-verify', limit=10, window_seconds=900, identifier=key)

        problem = password_policy_error(data.get('new_password'))
        if problem:
            raise bad_request(problem)

        now = utc_now()
        record = (
            Otp.objects.filter(email=key, used=False, expires_at__gt=now)
            .order_by('-id')
            .first()
        )
        if record is None or not secrets.compare_digest(record.otp_hash, _hash_otp(code)):
            raise bad_request('الكود غير صحيح')
        if not Otp.objects.filter(pk=record.pk, used=False).update(used=True):
            raise bad_request('الكود غير صحيح')

        user = _find_user(key)
        if user is None:
            raise ApiError('الحساب غير موجود', status.HTTP_404_NOT_FOUND)
        _assert_account_can_sign_in(user)
        User.objects.filter(pk=user.pk).update(
            password_hash=hash_password(data['new_password']),
            must_change_password=False,
        )
        user.refresh_from_db()
        _clear_failed_logins(user)
        _reset_axes(user.email)
        if user.phone:
            _reset_axes(user.phone)
        notify_password_changed(user, client_ip(request))
        notify_login(user, client_ip(request))

        token, refresh = _issue_token(user)
        return Response(
            {
                'message': 'تم تغيير كلمة السر وتسجيل دخولك',
                'token': token,
                'refresh': refresh,
                'user': _safe_user(user),
                'profile': _profile_for(user),
                'must_change_password': False,
            }
        )


@csrf_exempt
def telegram_webhook(request, secret):
    """POST /api/telegram/webhook/<secret> — one update from Telegram."""
    from django.http import HttpResponse, JsonResponse

    expected = telegram.webhook_secret()
    header = request.META.get('HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN', '')
    if (
        request.method != 'POST'
        or not expected
        or not secrets.compare_digest(secret, expected)
        or not secrets.compare_digest(header, expected)
    ):
        return HttpResponse(status=404)
    try:
        import json

        update = json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return JsonResponse({'ok': False}, status=400)
    try:
        result = telegram.handle_update(update)
    except Exception:  # noqa: BLE001 - Telegram retries on non-200; never loop
        import logging

        logging.getLogger(__name__).warning('Telegram update failed', exc_info=True)
        result = 'error'
    return JsonResponse({'ok': True, 'result': result})
