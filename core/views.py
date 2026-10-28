"""
API views for the Dashawy Django backend.

The module grew phase by phase:

* Phase 2: authentication (``/api/auth/*``).
* Phase 3: customer and technician request flows.
* Phase 4: admin dashboard counters and management endpoints.

The request/response shapes intentionally match the Express routes the existing
frontend was written against: the browser expects ``token`` (not SimpleJWT's
``access``), an ``error`` string on failures, and the same joined field names.
"""

import json
import math
import re
import secrets
from datetime import timedelta
from pathlib import Path

import magic
from axes.utils import reset as reset_axes_attempts
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_settings

from .auth_backend import BcryptAuthBackend, hash_password
from .exceptions import ApiError, bad_request, forbidden, unauthorized
from .models import (
    AdminPasswordResetAudit,
    Consent,
    Customer,
    Request,
    RequestBroadcast,
    Technician,
    User,
    utc_now,
)
from .permissions import IsAdmin, IsCustomer, IsTechnician
from .ratelimit import client_ip, throttle
from .serializers import (
    CreateRequestSerializer,
    RequestSerializer,
    TechnicianDocumentUploadSerializer,
    TechnicianRequestSerializer,
)
from .services.assignment import assign_technician, reassign_after_rejection
from .services.start_pin import (
    START_PIN_ATTEMPTS,
    START_PIN_LENGTH,
    START_PIN_LOCK_MINUTES,
    generate_start_pin,
    start_pin_lock_until,
)
from .services.pricing import quote_for
from .tokens import DashawyRefreshToken
from .upload_validators import save_private_upload
from .validation import (
    is_egypt_mobile,
    is_non_empty_string,
    is_valid_email,
    is_valid_name,
    is_valid_phone,
    local_mobile,
    normalize_email,
    normalize_phone,
    parse_lat_lng,
    password_policy_error,
    resolve_location,
)

# Mirrors the Express per-account brute-force brake.
MAX_ACCOUNT_FAILURES = 5
ACCOUNT_LOCK_MINUTES = 15

AUTH_ERROR = 'البريد الإلكتروني/الهاتف أو كلمة المرور غير صحيحة'
ACCOUNT_BLOCKED = 'تم إيقاف حسابك. تواصل مع الإدارة.'


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def _permissions(user):
    try:
        value = json.loads(user.permissions or '[]')
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _safe_user(user, profile_complete=None):
    """The user object the browser caches, matching safeUserFor() in Node."""
    if profile_complete is None:
        profile_complete = user.role != 'customer' or bool(user.phone)
    return {
        'id': user.id,
        'email': user.email,
        'phone': user.phone or None,
        'role': user.role,
        'is_super_admin': bool(user.is_super_admin),
        'permissions': _permissions(user),
        'profile_complete': bool(profile_complete),
    }


def _json_ready(row):
    """Turn a ``.values()`` row into JSON the way the Node rows looked."""
    if row is None:
        return None
    clean = {}
    for key, value in row.items():
        if hasattr(value, 'strftime'):
            clean[key] = value.strftime('%Y-%m-%d %H:%M:%S')
        else:
            clean[key] = value
    return clean


def _profile_for(user):
    """Return the role-specific profile row, or None for admins."""
    if user.role == 'customer':
        return _json_ready(
            Customer.objects.filter(user_id=user.id).values().first()
        )
    if user.role == 'technician':
        return _json_ready(
            Technician.objects.filter(user_id=user.id).values().first()
        )
    return None


def _issue_token(user):
    """Mint an access token and a refresh token."""
    refresh = DashawyRefreshToken.for_user(user)
    # Kept as claims so a Django-issued token carries the same identity fields
    # the legacy JWT did (userId, role, is_super_admin).
    refresh['role'] = user.role
    refresh['is_super_admin'] = bool(user.is_super_admin)
    access = refresh.access_token
    return str(access), str(refresh)


TEMP_PASSWORD_ALPHABET = (
    'ABCDEFGHJKLMNPQRSTUVWXYZ'
    'abcdefghijkmnopqrstuvwxyz'
    '23456789'
)
TEMP_PASSWORD_LENGTH = 16


def _generate_temporary_password(length=TEMP_PASSWORD_LENGTH):
    """A random password that always satisfies the letters+digit policy."""
    while True:
        value = ''.join(
            secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(length)
        )
        if (
            any(char.islower() for char in value)
            and any(char.isupper() for char in value)
            and any(char.isdigit() for char in value)
        ):
            return value


def _reset_axes(identifier):
    """Clear Axes failures after a successful password check.

    The project authenticates against core.User without calling Django's
    ``login()``, so the ``user_logged_in`` signal that normally resets Axes
    never fires. This keeps a successful login from leaving a stale lockout
    counter behind.
    """
    try:
        reset_axes_attempts(username=str(identifier))
    except Exception:
        # A reset failure must not turn a valid login into a 500. Axes will
        # expire the attempt on its own cooloff window.
        pass


def _password_strength_error(password, email, name):
    """Apply Django's configured validators to a new legacy account."""
    candidate = User(email=email, phone=None, role='customer')
    # core.User has no first_name column, but the similarity validator reads
    # arbitrary attributes and ignores missing model fields.
    candidate.first_name = name
    try:
        validate_password(password, user=candidate)
    except DjangoValidationError as exc:
        codes = {getattr(error, 'code', None) for error in exc.error_list}
        if 'password_too_common' in codes:
            return 'كلمة المرور شائعة جداً، اختر كلمة مرور أقوى'
        if 'password_entirely_numeric' in codes:
            return 'لا يمكن أن تكون كلمة المرور أرقاماً فقط'
        if 'password_too_similar' in codes:
            return 'كلمة المرور مشابهة جداً لبيانات حسابك'
        return 'كلمة المرور غير آمنة'
    return None


# ---------------------------------------------------------------------------
# Sign-in gates and account lockout (parity with auth.routes.js)
# ---------------------------------------------------------------------------


def _is_account_locked(user):
    return bool(user.login_locked_until and user.login_locked_until > utc_now())


def _note_failed_login(user):
    attempts = (user.failed_login_attempts or 0) + 1
    locked_until = None
    if attempts >= MAX_ACCOUNT_FAILURES:
        locked_until = utc_now() + timedelta(minutes=ACCOUNT_LOCK_MINUTES)
        # Reset the counter when the lock applies so the account gets a fresh
        # budget once the window has passed.
        attempts = 0
    User.objects.filter(pk=user.pk).update(
        failed_login_attempts=attempts, login_locked_until=locked_until
    )
    user.failed_login_attempts = attempts
    user.login_locked_until = locked_until


def _clear_failed_logins(user):
    User.objects.filter(pk=user.pk).update(
        failed_login_attempts=0, login_locked_until=None
    )
    user.failed_login_attempts = 0
    user.login_locked_until = None


def _assert_account_can_sign_in(user):
    """Refuse blocked/disabled accounts with the same messages as Node."""
    if user.blocked:
        raise unauthorized(ACCOUNT_BLOCKED)

    if user.role == 'technician':
        technician = (
            Technician.objects.filter(user_id=user.id)
            .values('approval_status', 'approval_note', 'availability')
            .first()
        )
        if technician:
            if technician['approval_status'] == 'pending':
                raise unauthorized(
                    'حسابك قيد المراجعة من قبل الإدارة. سيتم إشعارك عند الموافقة.'
                )
            if technician['approval_status'] == 'rejected':
                note = (
                    ' — السبب: ' + technician['approval_note']
                    if technician['approval_note']
                    else ''
                )
                raise unauthorized('تم رفض طلب انضمامك.' + note)
            if technician['availability'] == 'offline':
                raise unauthorized('هذا الحساب غير نشط')

    if user.role == 'customer':
        customer = Customer.objects.filter(user_id=user.id).values('blocked').first()
        if customer and customer['blocked']:
            raise unauthorized(
                'حسابك موقوف من قبل الإدارة. يرجى التواصل مع الدعم.'
            )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _body(request):
    """Request body as a mapping; a JSON array/string is treated as empty."""
    data = request.data
    return data if hasattr(data, 'get') else {}


class LoginView(APIView):
    """POST /api/auth/login — email or phone plus password."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = _body(request)
        # `identifier` is the current field; `email` is accepted for older
        # cached clients, exactly as the Express route did.
        identifier = str(data.get('identifier') or data.get('email') or '').strip()
        password = data.get('password')

        if not identifier or not isinstance(password, str) or not password:
            raise unauthorized(AUTH_ERROR)

        if '@' in identifier:
            user = User.objects.filter(email=normalize_email(identifier)).first()
        else:
            phone = normalize_phone(identifier)
            user = User.objects.filter(phone=phone).first() if phone else None

        # Same message whether the account is missing or the password is wrong,
        # so the endpoint cannot be used to enumerate users. Axes stores lockout
        # state on the Django request, not DRF's wrapper; passing the underlying
        # request lets AxesMiddleware return its configured 429 response. The
        # authenticate call runs for unknown identifiers too, so repeated
        # guessing is rate-limited by IP rather than only by existing account.
        auth_request = getattr(request, '_request', request)
        verified = authenticate(
            auth_request, username=identifier, password=password
        )
        if (
            user is None
            or not isinstance(verified, User)
            or verified.pk != user.pk
        ):
            if user is not None:
                _note_failed_login(user)
            raise unauthorized(AUTH_ERROR)

        # Refuse a locked account only after the password check, and with the
        # same generic message: refusing earlier would leak which accounts exist.
        if _is_account_locked(user):
            raise unauthorized(AUTH_ERROR)

        _clear_failed_logins(user)
        _assert_account_can_sign_in(user)
        _reset_axes(identifier)

        token, refresh = _issue_token(user)
        return Response(
            {
                'token': token,
                'refresh': refresh,
                'user': _safe_user(user),
                'profile': _profile_for(user),
                'must_change_password': bool(user.must_change_password),
            }
        )


class RegisterView(APIView):
    """POST /api/auth/register — customer self-registration."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        throttle(request, 'customer-register', limit=20, window_seconds=3600)
        data = _body(request)
        name = data.get('name')
        phone = data.get('phone')
        email = data.get('email')
        password = data.get('password')
        area_key = data.get('area_key')
        location_text = data.get('location_text')
        latitude = data.get('latitude')
        longitude = data.get('longitude')

        accepted = data.get('accept_terms')
        if accepted not in (True, 'true', '1', 1, 'on'):
            raise bad_request('لازم توافق على الشروط وسياسة الخصوصية')

        if not is_valid_name(name):
            raise bad_request('الاسم غير صالح (حرفان على الأقل)')
        if not is_valid_phone(phone):
            raise bad_request('رقم الهاتف غير صالح')
        if not is_valid_email(email):
            raise bad_request('البريد الإلكتروني غير صالح')
        password_problem = password_policy_error(password)
        if password_problem:
            raise bad_request(password_problem)
        strength_problem = _password_strength_error(
            password, normalize_email(email), name.strip()
        )
        if strength_problem:
            raise bad_request(strength_problem)
        if location_text is not None and not is_non_empty_string(location_text, 300):
            raise bad_request('وصف الموقع غير صالح')

        try:
            resolved_location = resolve_location(area_key, location_text)
        except KeyError:
            raise bad_request('المنطقة غير صالحة')

        coordinates = parse_lat_lng(latitude, longitude)
        if (latitude not in (None, '') or longitude not in (None, '')) and coordinates is None:
            raise bad_request('إحداثيات الموقع غير صالحة')

        norm_email = normalize_email(email)
        if User.objects.filter(email=norm_email).exists():
            raise bad_request('هذا البريد الإلكتروني مسجّل بالفعل')

        norm_phone = normalize_phone(phone)
        if not norm_phone or len(re.sub(r'\D', '', norm_phone)) < 8:
            raise bad_request('رقم الهاتف غير صالح')
        if User.objects.filter(phone=norm_phone).exists():
            raise bad_request(
                'هذا الرقم مسجّل بالفعل. جرّب تسجيل الدخول أو استخدم رقمًا آخر.'
            )

        password_hash = hash_password(password)

        try:
            with transaction.atomic():
                user = User.objects.create(
                    email=norm_email,
                    phone=norm_phone,
                    password_hash=password_hash,
                    role='customer',
                )
                Customer.objects.create(
                    user=user,
                    name=name.strip(),
                    phone=norm_phone,
                    location_text=resolved_location,
                    latitude=coordinates[0] if coordinates else None,
                    longitude=coordinates[1] if coordinates else None,
                )
                Consent.objects.create(
                    user=user,
                    consent_type='terms_and_privacy',
                    policy_version=str(data.get('policy_version') or 'v1.0')[:20],
                    ip_address=(request.META.get('REMOTE_ADDR') or '')[:64],
                    user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:1000],
                )
        except IntegrityError:
            # The UNIQUE index is the real guarantee; the checks above merely
            # turn the common case into a field-level 400.
            raise bad_request(
                'هذا البريد الإلكتروني أو رقم الهاتف مسجّل بالفعل'
            )

        token, refresh = _issue_token(user)
        return Response(
            {
                'token': token,
                'refresh': refresh,
                'user': _safe_user(user),
                'profile': _profile_for(user),
                'must_change_password': bool(user.must_change_password),
            },
            status=status.HTTP_201_CREATED,
        )


class LogoutView(APIView):
    """POST /api/auth/logout — blacklist the supplied refresh token."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = _body(request)
        refresh = data.get('refresh') or data.get('refresh_token')
        if not refresh or not isinstance(refresh, str):
            raise bad_request('رمز التحديث مطلوب')

        try:
            token = DashawyRefreshToken(refresh)
            token_user_id = token.get(jwt_settings.USER_ID_CLAIM)
            if str(token_user_id) != str(request.user.pk):
                raise bad_request('رمز التحديث غير صالح')
            token.blacklist()
        except TokenError:
            raise bad_request('رمز التحديث غير صالح')

        return Response({'message': 'تم تسجيل الخروج'})


class MeView(APIView):
    """GET /api/auth/me — fresh data for the signed-in account."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = User.objects.filter(pk=request.user.pk).first()
        if user is None:
            raise unauthorized('الحساب غير موجود')
        if user.blocked:
            raise unauthorized(ACCOUNT_BLOCKED)

        safe = _safe_user(user)
        # Extra fields Node returned only from /me: the browser uses these to
        # refresh the change-password prompt and the Telegram settings page.
        safe['must_change_password'] = bool(user.must_change_password)
        safe['telegram_linked'] = bool(user.telegram_chat_id)
        guest_count = 0
        if user.role == 'customer' and user.phone:
            guest_count = Request.objects.filter(
                customer__isnull=True,
                guest_phone=user.phone,
            ).exclude(status='cancelled').count()
        safe['guest_orders_count'] = guest_count

        return Response({'user': safe, 'profile': _profile_for(user)})


class ChangePasswordView(APIView):
    """POST /api/auth/change-password — rotate the signed-in account's password.

    This is also the exit from a forced change: a temporary password issued by
    an administrator sets ``must_change_password``, and saving a new password
    here clears it. The response carries a freshly minted token pair because
    every token tied to the old hash is invalidated by the change.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        throttle(
            request,
            'change-password',
            limit=10,
            window_seconds=3600,
            identifier=f'user:{request.user.pk}',
        )
        data = _body(request)
        current = data.get('current_password')
        new_password = data.get('new_password')

        if not isinstance(current, str) or not current:
            raise bad_request('كلمة المرور الحالية مطلوبة')
        if not isinstance(new_password, str) or not new_password:
            raise bad_request('كلمة المرور الجديدة مطلوبة')

        user = User.objects.filter(pk=request.user.pk).first()
        if user is None:
            raise unauthorized('الحساب غير موجود')
        if not BcryptAuthBackend.verify_password(user, current):
            raise bad_request('كلمة المرور الحالية غير صحيحة')
        if current == new_password:
            raise bad_request('كلمة المرور الجديدة يجب أن تختلف عن الحالية')

        problem = password_policy_error(new_password)
        if problem:
            raise bad_request(problem)
        strength_problem = _password_strength_error(
            new_password, user.email, ''
        )
        if strength_problem:
            raise bad_request(strength_problem)

        new_hash = hash_password(new_password)
        User.objects.filter(pk=user.pk).update(
            password_hash=new_hash,
            must_change_password=False,
            failed_login_attempts=0,
            login_locked_until=None,
        )
        user.password_hash = new_hash
        user.must_change_password = False
        user.failed_login_attempts = 0
        user.login_locked_until = None
        _reset_axes(user.email)

        token, refresh = _issue_token(user)
        return Response(
            {
                'message': 'تم تغيير كلمة المرور بنجاح',
                'token': token,
                'refresh': refresh,
                'user': _safe_user(user),
                'profile': _profile_for(user),
                'must_change_password': False,
            }
        )


# ---------------------------------------------------------------------------
# Customer requests
# ---------------------------------------------------------------------------

PROFILE_INCOMPLETE = 'أكمل بيانات حسابك أولاً للمتابعة'


def _customer_for(user):
    """The signed-in customer's profile, enforcing the legacy sign-in gates.

    ``requireCompleteProfile`` in the Express app checked ``users.phone`` and
    ``ensureCustomerHasProfile`` checked for the profile row. Both are repeated
    here because this is the API boundary, not the login screen: a token issued
    before the checks were added must not bypass them.
    """
    customer = Customer.objects.filter(user_id=user.pk).first()
    if customer is None:
        raise forbidden('حساب العميل غير مكتمل')
    if customer.blocked:
        raise forbidden('حسابك موقوف من قبل الإدارة. يرجى التواصل مع الدعم.')
    if not user.phone:
        raise forbidden(PROFILE_INCOMPLETE, code='profile_incomplete')
    return customer


def _technician_for(user):
    technician = Technician.objects.filter(user_id=user.pk).first()
    if technician is None:
        raise forbidden('حساب الفني غير مكتمل')
    return technician


class CustomerRequestListCreateView(APIView):
    """GET/POST /api/customer/requests."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def get(self, request):
        customer = _customer_for(request.user)
        requests = (
            Request.objects.filter(customer_id=customer.pk)
            .select_related('customer', 'technician')
            .order_by('-created_at', '-id')
        )
        return Response(
            {'requests': RequestSerializer(requests, many=True).data}
        )

    def post(self, request):
        customer = _customer_for(request.user)
        throttle(request, 'customer-order', limit=30, window_seconds=3600)
        serializer = CreateRequestSerializer(data=_body(request))
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        quote = quote_for(data['specialty'])

        created = Request.objects.create(
            customer_id=customer.pk,
            specialty=data['specialty'],
            custom_specialty_text=data.get('custom_specialty_text'),
            description=data['description'],
            location_text=data.get('resolved_location') or None,
            latitude=data.get('latitude'),
            longitude=data.get('longitude'),
            status='pending',
            **quote,
        )

        assignment = assign_technician(created.pk)
        created.refresh_from_db()
        # Read the row rather than trusting the return value, so a concurrent
        # assignment cannot make the message disagree with the request.
        assigned = (
            created.status == 'assigned' and created.technician_id is not None
        )

        payload = RequestSerializer(created).data
        return Response(
            {
                # Exact wording the Phase 3 contract specifies.
                'message': (
                    'تم تعيين فني لطلبك'
                    if assigned
                    else 'تم استلام طلبك بنجاح وسيتم التواصل معك قريباً'
                ),
                'request': payload,
                'assignment': {
                    'assigned': assigned,
                    'technician_id': assignment.get('technician_id'),
                    'reason': assignment.get('reason'),
                },
                'assigned': assigned,
            },
            status=status.HTTP_201_CREATED,
        )


class CustomerRequestDetailView(APIView):
    """GET /api/customer/requests/<id> — one request owned by the customer."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def get(self, request, pk):
        customer = _customer_for(request.user)
        job = (
            Request.objects.filter(pk=pk, customer_id=customer.pk)
            .select_related('customer', 'technician')
            .first()
        )
        if job is None:
            raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
        return Response({'request': RequestSerializer(job).data})


# ---------------------------------------------------------------------------
# Technician jobs
# ---------------------------------------------------------------------------


class TechnicianRequestListView(APIView):
    """GET /api/technician/requests — active jobs for the signed-in technician."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def get(self, request):
        technician = _technician_for(request.user)
        jobs = (
            Request.objects.filter(
                technician_id=technician.pk,
                status__in=('assigned', 'in_progress'),
            )
            .select_related('customer', 'technician')
            .order_by('-created_at', '-id')
        )
        return Response(
            {'requests': TechnicianRequestSerializer(jobs, many=True).data}
        )


def _start_job_with_pin(request, pk):
    """Move an assigned job to ``in_progress`` after checking the door code.

    This is the **only** status transition into ``in_progress``. The assigned
    technician must submit the four-digit code the customer received; wrong
    guesses increment ``start_pin_attempts`` and lock the code after
    ``START_PIN_ATTEMPTS`` failures for ``START_PIN_LOCK_MINUTES``.
    """
    technician = _technician_for(request.user)
    job = Request.objects.filter(pk=pk, technician_id=technician.pk).first()
    if job is None:
        raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
    if job.status != 'assigned':
        raise bad_request('لا يمكن بدء هذا الطلب في حالته الحالية')

    # Rows assigned before this flow existed have no code yet. Generating one
    # lazily keeps those jobs startable instead of permanently stuck.
    if not job.start_pin:
        Request.objects.filter(
            Q(start_pin__isnull=True) | Q(start_pin=''), pk=job.pk
        ).update(start_pin=generate_start_pin())
        job.refresh_from_db(fields=['start_pin'])

    now = utc_now()
    if job.start_pin_locked_until and job.start_pin_locked_until > now:
        minutes = max(
            1, math.ceil((job.start_pin_locked_until - now).total_seconds() / 60)
        )
        raise ApiError(
            f'المحاولات مقفلة. حاول تاني بعد {minutes} دقيقة',
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
    if job.start_pin_locked_until:
        # The lock has expired; start the technician with a clean counter.
        Request.objects.filter(pk=job.pk).update(
            start_pin_attempts=0, start_pin_locked_until=None
        )
        job.start_pin_attempts = 0
        job.start_pin_locked_until = None

    submitted = str(_body(request).get('pin') or '').strip()
    valid_shape = re.fullmatch(r'\d{%d}' % START_PIN_LENGTH, submitted) is not None
    if not valid_shape or not secrets.compare_digest(
        submitted, job.start_pin or ''
    ):
        Request.objects.filter(pk=job.pk).update(
            start_pin_attempts=F('start_pin_attempts') + 1
        )
        job.refresh_from_db(fields=['start_pin_attempts'])
        if job.start_pin_attempts >= START_PIN_ATTEMPTS:
            Request.objects.filter(pk=job.pk).update(
                start_pin_locked_until=start_pin_lock_until(now)
            )
            raise ApiError(
                'محاولات كتير غلط. الكود مقفول لمدة '
                f'{START_PIN_LOCK_MINUTES} دقيقة',
                status.HTTP_429_TOO_MANY_REQUESTS,
            )
        remaining = START_PIN_ATTEMPTS - job.start_pin_attempts
        label = 'محاولة واحدة' if remaining == 1 else f'{remaining} محاولات'
        raise ApiError(
            f'الكود غلط. باقي لك {label}', status.HTTP_400_BAD_REQUEST
        )

    claimed = Request.objects.filter(
        pk=job.pk, technician_id=technician.pk, status='assigned'
    ).update(
        status='in_progress',
        started_at=now,
        updated_at=now,
        # Single use: once the job starts the code is no longer a valid way in.
        start_pin=None,
        start_pin_attempts=0,
        start_pin_locked_until=None,
    )
    if not claimed:
        raise bad_request('لا يمكن بدء هذا الطلب في حالته الحالية')

    job.refresh_from_db()
    return Response(
        {
            'message': 'تم بدء تنفيذ الطلب',
            'request': TechnicianRequestSerializer(job).data,
        }
    )


class TechnicianRequestStartView(APIView):
    """POST /api/technician/requests/<id>/start — verify the customer's code."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request, pk):
        return _start_job_with_pin(request, pk)


class TechnicianRequestAcceptView(APIView):
    """Deprecated alias for ``/start``; still requires the arrival code.

    Kept so an older client that calls ``/accept`` is not silently bypassing
    the gate: without a valid ``pin`` it is rejected like any other attempt.
    """

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request, pk):
        return _start_job_with_pin(request, pk)


class TechnicianRequestRejectView(APIView):
    """POST /api/technician/requests/<id>/reject — decline and reassign."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request, pk):
        technician = _technician_for(request.user)
        job = Request.objects.filter(pk=pk, technician_id=technician.pk).first()
        if job is None:
            raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
        if job.status != 'assigned':
            raise bad_request('لا يمكن رفض هذا الطلب في حالته الحالية')

        now = utc_now()
        released = Request.objects.filter(
            pk=job.pk, technician_id=technician.pk, status='assigned'
        ).update(
            technician_id=None,
            status='pending',
            assigned_at=None,
            updated_at=now,
        )
        if not released:
            raise bad_request('لا يمكن رفض هذا الطلب في حالته الحالية')

        # Records the decline (JSON column + broadcast row), frees the
        # technician and runs assignment again among the remaining candidates.
        assignment = reassign_after_rejection(job.pk, technician.pk)
        job.refresh_from_db()
        reassigned = bool(assignment.get('assigned'))

        return Response(
            {
                'message': (
                    'تم رفض الطلب وإعادة تعيينه لفني آخر'
                    if reassigned
                    else 'تم رفض الطلب، ولا يوجد فني متاح حالياً'
                ),
                'request': TechnicianRequestSerializer(job).data,
                'assignment': {
                    'assigned': reassigned,
                    'technician_id': assignment.get('technician_id'),
                    'reason': assignment.get('reason'),
                },
                'reassigned': reassigned,
            }
        )


# ---------------------------------------------------------------------------
# Profile completion and guest-order linking
# ---------------------------------------------------------------------------


class CompleteProfileView(APIView):
    """POST /api/auth/complete-profile — add phone/area to a customer account.

    This is the existing phone-completion flow reused by Google sign-up. It
    changes the account's phone but deliberately never links guest orders by
    phone alone; the separate guest-link endpoint needs the tracking token too.
    """

    permission_classes = [IsAuthenticated, IsCustomer]

    def post(self, request):
        throttle(request, 'complete-profile', limit=20, window_seconds=3600)
        data = _body(request)
        user = request.user
        customer = Customer.objects.filter(user_id=user.pk).first()
        if customer is None:
            customer = Customer.objects.create(user=user, name='عميل دشاوي')

        name = data.get('name')
        if not is_valid_name(name):
            raise bad_request('اكتب اسم صحيح (حرفين على الأقل)')

        local = local_mobile(data.get('phone'))
        if not local:
            raise bad_request('اكتب رقم موبايل مصري صحيح يبدأ بـ 010 أو 011 أو 012 أو 015')
        phone = normalize_phone(local)
        if User.objects.filter(phone=phone).exclude(pk=user.pk).exists():
            raise bad_request('هذا الرقم مسجّل بالفعل. جرّب رقمًا آخر.')
        if Customer.objects.filter(phone=phone).exclude(pk=customer.pk).exists():
            raise bad_request('هذا الرقم مسجّل بالفعل. جرّب رقمًا آخر.')

        area_key = data.get('area_key')
        location_text = data.get('location_text')
        if location_text is not None and not is_non_empty_string(location_text, 300):
            raise bad_request('وصف الموقع غير صالح')
        try:
            resolved_location = resolve_location(area_key, location_text)
        except KeyError:
            raise bad_request('المنطقة غير صالحة')

        accepted = data.get('accept_terms')
        already_consented = Consent.objects.filter(
            user_id=user.pk, consent_type='terms_and_privacy'
        ).exists()
        if not already_consented and accepted not in (True, 'true', '1', 1, 'on'):
            raise bad_request('لازم توافق على الشروط وسياسة الخصوصية')

        with transaction.atomic():
            User.objects.filter(pk=user.pk).update(phone=phone)
            Customer.objects.filter(pk=customer.pk).update(
                name=name.strip(), phone=phone, location_text=resolved_location
            )
            if not already_consented:
                Consent.objects.create(
                    user_id=user.pk,
                    consent_type='terms_and_privacy',
                    policy_version=str(data.get('policy_version') or 'v1.0')[:20],
                    ip_address=(request.META.get('REMOTE_ADDR') or '')[:64],
                    user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:1000],
                )

        user.refresh_from_db()
        customer.refresh_from_db()
        # Guest rows store the local ``01...`` form while the account column is
        # canonical ``+20...``; check both so the count is not silently zero.
        guest_count = Request.objects.filter(
            customer__isnull=True, guest_phone__in={local, phone}
        ).exclude(status='cancelled').count()

        return Response(
            {
                'user': _safe_user(user, profile_complete=True),
                'profile': _profile_for(user),
                'guest_orders_count': guest_count,
                'message': 'تم حفظ بياناتك',
            }
        )


class GuestOrderLinkView(APIView):
    """POST /api/customer/guest-orders/link — link one order using its token.

    A phone match alone is not enough: the customer must present the unguessable
    tracking token from the guest confirmation page.
    """

    permission_classes = [IsAuthenticated, IsCustomer]

    def post(self, request):
        throttle(request, 'guest-link', limit=20, window_seconds=3600)
        from .guest_views import hash_token

        customer = _customer_for(request.user)
        data = _body(request)
        token = str(data.get('token') or '').strip()
        if not token or len(token) < 20:
            raise bad_request('الصق رابط المتابعة أو الكود اللي وصلك بعد الطلب')
        if token.startswith('http'):
            token = token.split('token=', 1)[-1]

        job = Request.objects.filter(guest_token_hash=hash_token(token)).first()
        if job is None:
            raise bad_request('رابط المتابعة غير صالح')
        if job.customer_id is not None:
            raise bad_request('الطلب ده مرتبط بحساب بالفعل')

        # The account stores ``+2010...`` while a guest order stores the local
        # ``010...`` form. Compare the canonical local-mobile form, never the
        # raw string, or a legitimate owner is told the number does not match.
        guest_local = local_mobile(job.guest_phone)
        account_locals = {
            local_mobile(phone)
            for phone in (request.user.phone, customer.phone)
            if local_mobile(phone)
        }
        if not guest_local or guest_local not in account_locals:
            raise forbidden('الرقم المسجّل في الطلب مختلف عن رقم حسابك')

        now = utc_now()
        updated = Request.objects.filter(
            pk=job.pk, customer__isnull=True
        ).update(
            customer_id=customer.pk,
            guest_token_hash=None,
            guest_linked_at=now,
            updated_at=now,
        )
        if not updated:
            raise bad_request('الطلب ده مرتبط بحساب بالفعل')

        job.refresh_from_db()
        return Response(
            {
                'message': 'تم ربط الطلب بحسابك',
                'request': RequestSerializer(job).data,
            }
        )


# ---------------------------------------------------------------------------
# Technician registration (with private documents)
# ---------------------------------------------------------------------------


class TechnicianRegisterView(APIView):
    """POST /api/auth/register-technician — multipart onboarding.

    The two identity documents are validated by magic bytes, stored under
    ``MEDIA_ROOT/technicians/<user id>/`` with random names, and are only ever
    streamed by the admin-only document endpoint below.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        throttle(request, 'technician-register', limit=10, window_seconds=3600)
        data = request.data

        name = data.get('name')
        phone = data.get('phone')
        email = data.get('email')
        password = data.get('password')
        area_key = data.get('area_key')
        location_text = data.get('location_text')

        if not is_valid_name(name):
            raise bad_request('الاسم غير صالح (حرفان على الأقل)')
        local = local_mobile(phone)
        if not local:
            raise bad_request('رقم الهاتف غير صالح')
        if not is_valid_email(email):
            raise bad_request('البريد الإلكتروني غير صالح')
        password_problem = password_policy_error(password)
        if password_problem:
            raise bad_request(password_problem)
        if data.get('accept_terms') not in (True, 'true', '1', 1, 'on'):
            raise bad_request('لازم توافق على الشروط وسياسة الخصوصية')
        if data.get('sensitive_consent') not in (True, 'true', '1', 1, 'on'):
            raise bad_request('لازم توافق على معالجة مستندات التحقق')

        try:
            resolved_location = resolve_location(area_key, location_text)
        except KeyError:
            raise bad_request('المنطقة غير صالحة')
        coordinates = parse_lat_lng(data.get('latitude'), data.get('longitude'))
        if (
            data.get('latitude') not in (None, '')
            or data.get('longitude') not in (None, '')
        ) and coordinates is None:
            raise bad_request('إحداثيات الموقع غير صالحة')

        norm_email = normalize_email(email)
        norm_phone = normalize_phone(local)
        if User.objects.filter(email=norm_email).exists():
            raise bad_request('هذا البريد الإلكتروني مسجّل بالفعل')
        if User.objects.filter(phone=norm_phone).exists():
            raise bad_request('هذا الرقم مسجّل بالفعل. جرّب تسجيل الدخول.')

        documents = TechnicianDocumentUploadSerializer(data={
            'criminal_record': request.FILES.get('criminal_record'),
            'national_id': request.FILES.get('national_id'),
        })
        documents.is_valid(raise_exception=True)

        password_hash = hash_password(password)
        try:
            with transaction.atomic():
                user = User.objects.create(
                    email=norm_email,
                    phone=norm_phone,
                    password_hash=password_hash,
                    role='technician',
                )
                technician = Technician.objects.create(
                    user=user,
                    name=name.strip(),
                    phone=norm_phone,
                    specialty='dish_general',
                    location_text=resolved_location,
                    latitude=coordinates[0] if coordinates else None,
                    longitude=coordinates[1] if coordinates else None,
                    approval_status='pending',
                    availability='offline',
                )
                Consent.objects.create(
                    user=user,
                    consent_type='terms_privacy_sensitive_documents',
                    policy_version=str(data.get('policy_version') or 'v1.0')[:20],
                    ip_address=(request.META.get('REMOTE_ADDR') or '')[:64],
                    user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:1000],
                )
        except IntegrityError:
            raise bad_request('هذا البريد الإلكتروني أو رقم الهاتف مسجّل بالفعل')

        # Save documents after the row exists so the directory can be named by
        # the account id. A failed validation above already stopped us.
        from .upload_validators import validate_document_upload

        saved = {}
        try:
            for field, upload in (
                ('criminal_record', request.FILES.get('criminal_record')),
                ('national_id', request.FILES.get('national_id')),
            ):
                mime = validate_document_upload(upload)
                saved[field] = save_private_upload(
                    upload, f'technicians/user-{user.pk}', mime
                )
            Technician.objects.filter(pk=technician.pk).update(
                criminal_record_path=saved.get('criminal_record'),
                national_id_path=saved.get('national_id'),
            )
        except DjangoValidationError as exc:
            # Do not leave a half-onboarded account without its documents.
            User.objects.filter(pk=user.pk).delete()
            raise bad_request(exc.messages[0] if exc.messages else 'المستند غير صالح')

        return Response(
            {
                'message': 'تم استلام طلبك — قيد المراجعة',
                'status': 'pending',
            },
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

ADMIN_REQUEST_STATUSES = (
    'pending',
    'assigned',
    'in_progress',
    'completed',
    'cancelled',
    'no_technician',
)


def _admin_technician_queryset():
    """Technician columns plus the joined account fields the dashboard reads."""
    fields = [field.name for field in Technician._meta.fields]
    return Technician.objects.select_related('user').values(
        *fields,
        'user__email',
        'user__phone',
        'user__created_at',
    )


def _admin_technician_row(row):
    """Rename the joined columns to the names used by the Express API."""
    if row is None:
        return None
    clean = dict(row)
    clean['user_id'] = clean.pop('user', None)
    clean['email'] = clean.pop('user__email', None)
    clean['account_phone'] = clean.pop('user__phone', None)
    clean['account_created_at'] = clean.pop('user__created_at', None)
    return _json_ready(clean)


def _admin_technician_payload(technician_id):
    row = _admin_technician_queryset().filter(pk=technician_id).first()
    return _admin_technician_row(row)


class AdminStatsView(APIView):
    """GET /api/admin/stats — dashboard counters and chart data."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        status_counts = {key: 0 for key in ADMIN_REQUEST_STATUSES}
        for row in Request.objects.values('status').annotate(n=Count('id')):
            if row['status'] in status_counts:
                status_counts[row['status']] = row['n']

        total_requests = Request.objects.count()
        total_customers = Customer.objects.count()
        total_technicians = Technician.objects.count()
        available_technicians = Technician.objects.filter(
            availability='available'
        ).count()
        busy_technicians = Technician.objects.filter(availability='busy').count()
        pending_technicians = Technician.objects.filter(
            approval_status='pending'
        ).count()

        durations = []
        for created_at, assigned_at in Request.objects.filter(
            assigned_at__isnull=False
        ).values_list('created_at', 'assigned_at'):
            if created_at and assigned_at:
                durations.append((assigned_at - created_at).total_seconds() / 60)
        avg_response_minutes = (
            round(sum(durations) / len(durations)) if durations else None
        )

        since = utc_now() - timedelta(days=6)
        by_day = {}
        for created_at in Request.objects.filter(
            created_at__gte=since
        ).values_list('created_at', flat=True):
            if created_at:
                day = created_at.strftime('%Y-%m-%d')
                by_day[day] = by_day.get(day, 0) + 1

        newest = (
            Request.objects.select_related('customer', 'technician')
            .order_by('-created_at', '-id')[:8]
        )

        return Response(
            {
                # Phase 4 contract: flat counters.
                'totalRequests': total_requests,
                'pendingRequests': status_counts['pending'],
                'activeTechnicians': available_technicians,
                'totalCustomers': total_customers,
                'completedRequests': status_counts['completed'],
                'cancelledRequests': status_counts['cancelled'],
                # Extra fields the existing dashboard renders (charts, cards,
                # recent requests). Kept alongside the flat counters so both
                # the new contract and the legacy frontend work.
                'byStatus': status_counts,
                'totalTechnicians': total_technicians,
                'pendingTechnicians': pending_technicians,
                'availableTechnicians': available_technicians,
                'busyTechnicians': busy_technicians,
                'avgResponseMinutes': avg_response_minutes,
                'byDay': by_day,
                'newestRequests': RequestSerializer(newest, many=True).data,
            }
        )


class AdminRequestListView(APIView):
    """GET /api/admin/requests — every request, optionally filtered."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        requests = Request.objects.select_related(
            'customer', 'technician'
        ).order_by('-created_at', '-id')

        status_filter = request.query_params.get('status')
        if status_filter:
            requests = requests.filter(status=status_filter)

        specialty_filter = request.query_params.get('specialty')
        if specialty_filter:
            requests = requests.filter(specialty=specialty_filter)

        return Response(
            {'requests': RequestSerializer(requests, many=True).data}
        )


class AdminTechnicianListView(APIView):
    """GET /api/admin/technicians — all technicians with account details."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        rows = _admin_technician_queryset().order_by('-created_at', '-id')
        return Response(
            {'technicians': [_admin_technician_row(row) for row in rows]}
        )


class AdminTechnicianVerifyView(APIView):
    """POST /api/admin/technicians/<id>/verify — toggle document verification."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, pk):
        technician = Technician.objects.filter(pk=pk).first()
        if technician is None:
            raise ApiError('الفني غير موجود', status.HTTP_404_NOT_FOUND)

        verified = not bool(technician.documents_verified)
        Technician.objects.filter(pk=technician.pk).update(
            documents_verified=verified
        )
        technician.refresh_from_db()

        return Response(
            {
                'message': (
                    'تم توثيق مستندات الفني'
                    if verified
                    else 'تم إلغاء توثيق مستندات الفني'
                ),
                'documents_verified': verified,
                'technician': _admin_technician_payload(technician.pk),
            }
        )


class AdminTechnicianDetailView(APIView):
    """GET /api/admin/technicians/<id> — profile plus recent requests."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, pk):
        row = _admin_technician_payload(pk)
        if row is None:
            raise ApiError('الفني غير موجود', status.HTTP_404_NOT_FOUND)
        recent = (
            Request.objects.filter(technician_id=pk)
            .select_related('customer', 'technician')
            .order_by('-created_at', '-id')[:5]
        )
        return Response(
            {
                'technician': row,
                'recentRequests': RequestSerializer(recent, many=True).data,
            }
        )


class AdminTechnicianApproveView(APIView):
    """POST /api/admin/technicians/<id>/approve — activate a reviewed account."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, pk):
        technician = Technician.objects.filter(pk=pk).first()
        if technician is None:
            raise ApiError('الفني غير موجود', status.HTTP_404_NOT_FOUND)
        if not technician.national_id_path or not technician.criminal_record_path:
            raise bad_request('مينفعش نعتمد الفني قبل رفع البطاقة والفيش الجنائي')
        Technician.objects.filter(pk=pk).update(
            approval_status='approved',
            approval_note=None,
            approved_at=utc_now(),
            approved_by=request.user.pk,
            availability='available',
            documents_verified=True,
        )
        technician.refresh_from_db()
        return Response(
            {
                'message': 'تم اعتماد الفني وتفعيل حسابه',
                'technician': _admin_technician_payload(pk),
            }
        )


class AdminTechnicianRejectView(APIView):
    """POST /api/admin/technicians/<id>/reject — refuse onboarding with a note."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, pk):
        technician = Technician.objects.filter(pk=pk).first()
        if technician is None:
            raise ApiError('الفني غير موجود', status.HTTP_404_NOT_FOUND)
        note = str(_body(request).get('note') or '').strip()[:1000]
        Technician.objects.filter(pk=pk).update(
            approval_status='rejected',
            approval_note=note or None,
            availability='offline',
            documents_verified=False,
        )
        technician.refresh_from_db()
        return Response(
            {
                'message': 'تم رفض طلب الفني',
                'technician': _admin_technician_payload(pk),
            }
        )


class AdminTechnicianDocumentView(APIView):
    """GET /api/admin/technicians/<id>/document/<type> — private document stream.

    This is the ONLY route that can return a national ID or criminal record.
    The files live under ``MEDIA_ROOT`` with no ``MEDIA_URL`` route, and the
    path is re-resolved and checked so a tampered database value cannot escape
    the uploads directory.
    """

    permission_classes = [IsAuthenticated, IsAdmin]

    DOCUMENT_FIELDS = {
        'national_id': 'national_id_path',
        'criminal_record': 'criminal_record_path',
    }

    def get(self, request, pk, doc_type):
        if doc_type not in self.DOCUMENT_FIELDS:
            raise Http404('المستند غير موجود')
        technician = Technician.objects.filter(pk=pk).values(
            self.DOCUMENT_FIELDS[doc_type]
        ).first()
        if not technician:
            raise Http404('الفني غير موجود')
        stored = technician.get(self.DOCUMENT_FIELDS[doc_type])
        if not stored:
            raise Http404('المستند غير موجود')

        relative = str(stored).replace('\\', '/').lstrip('/')
        if relative.startswith('uploads/'):
            relative = relative[len('uploads/'):]
        root = Path(settings.MEDIA_ROOT).resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise Http404('المستند غير موجود')
        # Only the two private document trees are ever served here; a database
        # row pointing at a profile image must not be readable through this
        # admin endpoint either.
        if 'technicians/profiles/' in target.as_posix() or 'technicians/portfolio/' in target.as_posix():
            raise Http404('المستند غير موجود')
        if not target.is_file():
            raise Http404('المستند غير موجود')

        detected = magic.from_file(str(target), mime=True)
        if detected not in ('image/jpeg', 'image/png', 'application/pdf'):
            raise Http404('المستند غير موجود')
        response = FileResponse(open(target, 'rb'), content_type=detected)
        response['Content-Disposition'] = f'inline; filename="{doc_type}"'
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class AdminCustomerListView(APIView):
    """GET /api/admin/customers — all customers with account details."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        fields = [field.name for field in Customer._meta.fields]
        rows = (
            Customer.objects.select_related('user')
            .annotate(requests_count=Count('requests'))
            .values(
                *fields,
                'user__email',
                'user__phone',
                'requests_count',
            )
            .order_by('-created_at', '-id')
        )

        customers = []
        for row in rows:
            clean = dict(row)
            clean['user_id'] = clean.pop('user', None)
            clean['email'] = clean.pop('user__email', None)
            clean['account_phone'] = clean.pop('user__phone', None)
            customers.append(_json_ready(clean))

        return Response({'customers': customers})


class AdminUserResetPasswordView(APIView):
    """POST /api/admin/users/<id>/reset-password — issue a temporary password.

    The plaintext password is generated server-side, returned exactly once in
    this response (``Cache-Control: no-store``), and never written to the
    database or to any log. The target account is forced through the
    change-password screen on its next sign-in, and every JWT minted with the
    previous password stops authenticating immediately.
    """

    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, pk):
        throttle(
            request,
            'admin-password-reset',
            limit=10,
            window_seconds=3600,
            identifier=f'admin:{request.user.pk}:{client_ip(request)}',
        )

        target = User.objects.filter(pk=pk).first()
        if target is None:
            raise ApiError('المستخدم غير موجود', status.HTTP_404_NOT_FOUND)

        temporary_password = _generate_temporary_password()
        now = utc_now()
        with transaction.atomic():
            User.objects.filter(pk=target.pk).update(
                password_hash=hash_password(temporary_password),
                must_change_password=True,
                failed_login_attempts=0,
                login_locked_until=None,
            )
            AdminPasswordResetAudit.objects.create(
                admin_id=request.user.pk,
                target_id=target.pk,
                created_at=now,
                ip_address=client_ip(request),
            )

        # A reset also clears an Axes lockout: the user is about to sign in with
        # a brand-new credential, and leaving them blocked would defeat the
        # admin's action.
        _reset_axes(target.email)

        target.refresh_from_db()
        safe = _safe_user(target)
        safe['must_change_password'] = True
        response = Response(
            {
                'message': (
                    'تم إنشاء كلمة مرور مؤقتة. اعرضها للمستخدم مرة واحدة، '
                    'وسيُطلب منه تغييرها عند أول دخول.'
                ),
                'temporary_password': temporary_password,
                'temp_password': temporary_password,
                'must_change_password': True,
                'user': safe,
            }
        )
        response['Cache-Control'] = 'no-store, max-age=0'
        response['Pragma'] = 'no-cache'
        return response


class AdminRequestCancelView(APIView):
    """POST /api/admin/requests/<id>/cancel — force-cancel a request."""

    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, pk):
        job = Request.objects.filter(pk=pk).first()
        if job is None:
            raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
        if job.status in ('completed', 'cancelled'):
            raise bad_request('لا يمكن إلغاء طلب في حالته الحالية')

        now = utc_now()
        with transaction.atomic():
            Request.objects.filter(pk=job.pk).update(
                status='cancelled', updated_at=now
            )
            if job.technician_id:
                Technician.objects.filter(pk=job.technician_id).update(
                    availability='available'
                )
            # Outstanding offers must stop appearing in technicians' inboxes.
            RequestBroadcast.objects.filter(
                request_id=job.pk, status='sent'
            ).update(status='expired', responded_at=now)

        job.refresh_from_db()
        return Response(
            {
                'message': 'تم إلغاء الطلب',
                'request': RequestSerializer(job).data,
            }
        )
