"""
Authentication API for the Django backend.

Phase 2 provides the three endpoints the existing frontend needs first:

    POST /api/auth/login
    POST /api/auth/register
    GET  /api/auth/me

The request/response shapes intentionally match ``backend/routes/auth.routes.js``:
the browser expects ``token`` (not SimpleJWT's ``access``), an ``error`` string
on failures, and the role-specific ``profile`` object alongside the user.
"""

import json
import re
from datetime import timedelta

from django.contrib.auth import authenticate
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .auth_backend import hash_password
from .exceptions import ApiError, bad_request, forbidden, unauthorized
from .models import Customer, Request, Technician, User, utc_now
from .permissions import IsCustomer, IsTechnician
from .serializers import (
    CreateRequestSerializer,
    RequestSerializer,
    TechnicianRequestSerializer,
)
from .services.assignment import assign_technician, reassign_after_rejection
from .validation import (
    is_non_empty_string,
    is_valid_email,
    is_valid_name,
    is_valid_phone,
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
    """Mint an access token (and a refresh token for future use)."""
    refresh = RefreshToken.for_user(user)
    # Kept as claims so a Django-issued token carries the same identity fields
    # the Express JWT did (userId, role, is_super_admin).
    refresh['role'] = user.role
    refresh['is_super_admin'] = bool(user.is_super_admin)
    access = refresh.access_token
    return str(access), str(refresh)


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
        # so the endpoint cannot be used to enumerate users.
        verified = (
            authenticate(request, username=identifier, password=password)
            if user is not None
            else None
        )
        if user is None or verified is None or verified.pk != user.pk:
            if user is not None:
                _note_failed_login(user)
            raise unauthorized(AUTH_ERROR)

        # Refuse a locked account only after the password check, and with the
        # same generic message: refusing earlier would leak which accounts exist.
        if _is_account_locked(user):
            raise unauthorized(AUTH_ERROR)

        _clear_failed_logins(user)
        _assert_account_can_sign_in(user)

        token, _refresh = _issue_token(user)
        return Response(
            {
                'token': token,
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
        data = _body(request)
        name = data.get('name')
        phone = data.get('phone')
        email = data.get('email')
        password = data.get('password')
        area_key = data.get('area_key')
        location_text = data.get('location_text')
        latitude = data.get('latitude')
        longitude = data.get('longitude')

        if not is_valid_name(name):
            raise bad_request('الاسم غير صالح (حرفان على الأقل)')
        if not is_valid_phone(phone):
            raise bad_request('رقم الهاتف غير صالح')
        if not is_valid_email(email):
            raise bad_request('البريد الإلكتروني غير صالح')
        password_problem = password_policy_error(password)
        if password_problem:
            raise bad_request(password_problem)
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
        except IntegrityError:
            # The UNIQUE index is the real guarantee; the checks above merely
            # turn the common case into a field-level 400.
            raise bad_request(
                'هذا البريد الإلكتروني أو رقم الهاتف مسجّل بالفعل'
            )

        token, _refresh = _issue_token(user)
        return Response(
            {
                'token': token,
                'user': _safe_user(user),
                'profile': _profile_for(user),
                'must_change_password': bool(user.must_change_password),
            },
            status=status.HTTP_201_CREATED,
        )


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

        return Response({'user': safe, 'profile': _profile_for(user)})


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
        serializer = CreateRequestSerializer(data=_body(request))
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        created = Request.objects.create(
            customer_id=customer.pk,
            specialty=data['specialty'],
            custom_specialty_text=data.get('custom_specialty_text'),
            description=data['description'],
            location_text=data.get('resolved_location') or None,
            latitude=data.get('latitude'),
            longitude=data.get('longitude'),
            status='pending',
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


class TechnicianRequestAcceptView(APIView):
    """POST /api/technician/requests/<id>/accept — start the job."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request, pk):
        technician = _technician_for(request.user)
        job = Request.objects.filter(pk=pk, technician_id=technician.pk).first()
        if job is None:
            raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
        if job.status != 'assigned':
            raise bad_request('لا يمكن بدء هذا الطلب في حالته الحالية')

        now = utc_now()
        claimed = Request.objects.filter(
            pk=job.pk, technician_id=technician.pk, status='assigned'
        ).update(status='in_progress', started_at=now, updated_at=now)
        if not claimed:
            raise bad_request('لا يمكن بدء هذا الطلب في حالته الحالية')

        job.refresh_from_db()
        return Response(
            {
                'message': 'تم بدء تنفيذ الطلب',
                'request': TechnicianRequestSerializer(job).data,
            }
        )


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
                'request': RequestSerializer(job).data,
                'assignment': {
                    'assigned': reassigned,
                    'technician_id': assignment.get('technician_id'),
                    'reason': assignment.get('reason'),
                },
                'reassigned': reassigned,
            }
        )
