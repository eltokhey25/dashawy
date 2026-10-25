"""Google Sign-In endpoints for customer accounts."""

from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError, forbidden
from .google_auth import GoogleTokenError, verify_id_token
from .models import Customer, User
from .ratelimit import throttle
from .views import _issue_token, _profile_for, _safe_user


class GoogleConfigView(APIView):
    """GET /api/auth/google/config — public Client ID, or enabled: false."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        client_id = (getattr(settings, 'GOOGLE_CLIENT_ID', '') or '').strip()
        return Response(
            {
                'enabled': bool(client_id),
                'client_id': client_id,
            }
        )


class GoogleLoginView(APIView):
    """POST /api/auth/google — verify the ID token and issue an app session."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        throttle(request, 'google-login', limit=30, window_seconds=900)
        data = request.data if hasattr(request.data, 'get') else {}
        id_token = data.get('id_token') or data.get('credential')

        try:
            claims = verify_id_token(id_token)
        except GoogleTokenError as exc:
            status_code = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if 'غير مُفعّل' in str(exc)
                else status.HTTP_401_UNAUTHORIZED
            )
            raise ApiError(str(exc), status_code)

        google_sub = str(claims['sub'])
        email = claims['email']

        user = User.objects.filter(google_sub=google_sub).first()
        if user is None:
            user = User.objects.filter(email=email).first()
            if user is not None:
                if user.role != 'customer':
                    raise forbidden('هذا البريد مرتبط بحساب فني أو إداري. سجّل الدخول بالطريقة العادية.')
                if user.google_sub and user.google_sub != google_sub:
                    raise forbidden('هذا الحساب مرتبط بحساب جوجل تاني.')
                if user.blocked:
                    raise forbidden('تم إيقاف حسابك. تواصل مع الإدارة.')
                User.objects.filter(pk=user.pk).update(google_sub=google_sub)
                user.google_sub = google_sub
            else:
                name = (claims.get('name') or email.split('@')[0]).strip()[:255]
                user = User.objects.create(
                    email=email,
                    phone=None,
                    # Google accounts have no local password. The value is not
                    # a valid bcrypt hash, so password login fails closed.
                    password_hash='!google-oauth-no-password!',
                    role='customer',
                    google_sub=google_sub,
                )
                Customer.objects.create(user=user, name=name or 'عميل دشاوي')

        if user.blocked:
            raise forbidden('تم إيقاف حسابك. تواصل مع الإدارة.')

        customer = Customer.objects.filter(user_id=user.pk).first()
        if customer is None and user.role == 'customer':
            # A legacy customer row should always exist, but do not leave an
            # account unable to finish onboarding if it does not.
            name = (claims.get('name') or email.split('@')[0]).strip()[:255]
            Customer.objects.create(user=user, name=name or 'عميل دشاوي')
            customer = Customer.objects.filter(user_id=user.pk).first()

        profile_complete = bool(user.phone and customer)
        token, refresh = _issue_token(user)
        safe = _safe_user(user, profile_complete=profile_complete)

        guest_count = 0
        if user.phone:
            from .models import Request

            guest_count = Request.objects.filter(
                customer__isnull=True,
                guest_phone=user.phone,
                status__in=('pending', 'assigned', 'in_progress', 'completed'),
            ).count()

        return Response(
            {
                'token': token,
                'refresh': refresh,
                'user': safe,
                'profile': _profile_for(user),
                'must_change_password': bool(user.must_change_password),
                'profile_complete': profile_complete,
                'guest_orders_count': guest_count,
            }
        )
