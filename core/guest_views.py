"""Account-free ordering and token-based tracking.

Design notes
------------
* A guest order is a normal ``Request`` row with ``customer_id = NULL`` and the
  guest's name/phone stored on the request. Auto-assignment therefore works
  unchanged: it only needs the specialty, coordinates and status.
* The tracking link contains a 256-bit random token. Only its SHA-256 hash is
  stored, so a database leak does not hand out working links, and IDs cannot be
  enumerated.
* Linking a guest order to a later account requires the tracking token AND a
  phone match. Merely registering the same phone number never auto-links
  somebody else's order.
* The order form now also creates the customer's account: a new phone number
  plus a password becomes an account (email optional) and the order is saved
  on it. A phone that already has an account gets ``code=account_exists``; the
  page then signs in through /api/auth/login (with its lockout rules) and
  resubmits the order with the new token.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth_backend import hash_password
from .exceptions import ApiError, bad_request, forbidden
from .models import (
    Consent,
    Customer,
    GuestSubmissionLog,
    Request,
    RequestBroadcast,
    Technician,
    User,
    utc_now,
)
from .ratelimit import client_ip
from .serializers import GuestCreateRequestSerializer
from .services.broadcast import broadcast_request
from .services.pricing import price_status, quote_for
from .upload_validators import (
    ALLOWED_ORDER_IMAGE_MIME_TYPES,
    open_stored_upload,
    resolve_stored_upload,
    save_private_upload,
    validate_order_image,
)
from .validation import (
    is_valid_email,
    normalize_email,
    normalize_phone,
    password_policy_error,
)

RATE_LIMIT_PER_HOUR = 5


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _request_for_token(token: str):
    if not token or len(token) < 20:
        raise ApiError('رابط المتابعة غير صالح', status.HTTP_404_NOT_FOUND)
    job = Request.objects.filter(guest_token_hash=hash_token(token)).first()
    if job is None:
        raise ApiError('رابط المتابعة غير صالح أو منتهي', status.HTTP_404_NOT_FOUND)
    return job


# Accounts made from the order form without an email get a placeholder under
# the reserved .invalid TLD: users.email is NOT NULL and UNIQUE, and nothing in
# the app sends mail, so the address is never used. They sign in by phone.
PLACEHOLDER_EMAIL_DOMAIN = 'phone.dashawy.invalid'


def placeholder_email(phone: str) -> str:
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    return f'{digits}@{PLACEHOLDER_EMAIL_DOMAIN}'


def is_placeholder_email(email) -> bool:
    return str(email or '').endswith('@' + PLACEHOLDER_EMAIL_DOMAIN)


def _account_for_phone(phone: str):
    """The existing user for a local ``01...`` number, in either stored form."""
    canonical = normalize_phone(phone)
    candidates = {phone, canonical} - {None, ''}
    return User.objects.filter(phone__in=candidates).first()


def _create_order_account(request, data):
    """Validate the form's password/email and create the customer account.

    Returns ``(user, customer)``. Must run inside the order's transaction so a
    failed order never leaves an orphan account behind.
    """
    from .views import _password_strength_error

    password = request.data.get('password')
    raw_email = str(request.data.get('email') or '').strip()

    if not password:
        raise bad_request('اختار كلمة سر لحسابك', code='password_required')
    problem = password_policy_error(password)
    if problem:
        raise bad_request(problem, code='password_invalid')

    if raw_email:
        if not is_valid_email(raw_email):
            raise bad_request('البريد الإلكتروني غير صالح', code='email_invalid')
        email = normalize_email(raw_email)
        if User.objects.filter(email=email).exists():
            raise bad_request(
                'البريد ده مسجّل على حساب تاني. سيبه فاضي أو سجّل دخول بيه.',
                code='email_taken',
            )
    else:
        email = placeholder_email(normalize_phone(data['phone']))

    strength = _password_strength_error(password, email, data['name'])
    if strength:
        raise bad_request(strength, code='password_invalid')

    phone = normalize_phone(data['phone'])
    try:
        with transaction.atomic():
            user = User.objects.create(
                email=email,
                phone=phone,
                password_hash=hash_password(password),
                role='customer',
            )
            customer = Customer.objects.create(
                user=user,
                name=data['name'],
                phone=phone,
                location_text=data.get('resolved_location') or None,
            )
    except IntegrityError:
        raise ApiError(
            'الرقم ده عليه حساب بالفعل. اكتب كلمة السر بتاعته.',
            status.HTTP_409_CONFLICT,
            code='account_exists',
        )
    return user, customer


def _label_for(key: str) -> str:
    from .models import Specialty

    label = Specialty.objects.filter(key=key).values_list('label', flat=True).first()
    if label:
        return label
    from .site_config import STATIC_SPECIALTIES

    for row in STATIC_SPECIALTIES:
        if row['key'] == key:
            return row['label']
    return key


def guest_payload(job: Request, include_photo=True) -> dict:
    """Only the fields a guest needs; never another customer's data."""
    tech = job.technician if job.technician_id else None
    contact_visible = job.status in ('assigned', 'in_progress', 'completed')
    return {
        'id': job.pk,
        'reference': f'#{job.pk}',
        'service': job.specialty,
        'service_label': _label_for(job.specialty),
        'custom_specialty_text': job.custom_specialty_text,
        'description': job.description,
        'area': (job.location_text or '').split('—')[0].strip(),
        'location_text': job.location_text,
        'status': job.status,
        'created_at': job.created_at.strftime('%Y-%m-%d %H:%M:%S')
        if job.created_at
        else None,
        'assigned_at': job.assigned_at.strftime('%Y-%m-%d %H:%M:%S')
        if job.assigned_at
        else None,
        'completed_at': job.completed_at.strftime('%Y-%m-%d %H:%M:%S')
        if job.completed_at
        else None,
        'technician': (
            {
                'name': tech.name,
                'phone': tech.phone if contact_visible else None,
            }
            if tech
            else None
        ),
        'price': price_status(job),
        'start_pin': job.start_pin if job.status == 'assigned' else None,
        'has_photo': bool(job.order_photo_path),
        'can_cancel': job.status in ('pending', 'no_technician', 'assigned'),
        'tracking_url': None,
        'photo_url': None,
        'is_guest': True,
    }


class GuestRequestCreateView(APIView):
    """POST /api/guest/requests — the public order form.

    Every order ends up on a customer account: a signed-in customer's order is
    saved on their account, and a visitor's password creates one with the
    order. The JWT is optional here, so the default authentication runs.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        ip = client_ip(request)
        since = utc_now() - timedelta(hours=1)

        # Per-IP and per-phone DB-backed limits (persist across processes).
        if (
            GuestSubmissionLog.objects.filter(
                ip_address=ip, created_at__gte=since
            ).count()
            >= RATE_LIMIT_PER_HOUR
        ):
            raise ApiError(
                'وصلت للحد الأقصى من الطلبات. حاول تاني بعد ساعة.',
                status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = GuestCreateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if (
            GuestSubmissionLog.objects.filter(
                phone=data['phone'], created_at__gte=since
            ).count()
            >= RATE_LIMIT_PER_HOUR
        ):
            raise ApiError(
                'وصلت للحد الأقصى من الطلبات لنفس الرقم. حاول تاني بعد ساعة.',
                status.HTTP_429_TOO_MANY_REQUESTS,
            )

        photo = request.FILES.get('photo')
        detected = None
        if photo is not None:
            try:
                detected = validate_order_image(photo)
            except DjangoValidationError as exc:
                raise bad_request(exc.messages[0] if exc.messages else 'الصورة غير صالحة')

        signed_in = getattr(request.user, 'is_authenticated', False)
        if signed_in and getattr(request.user, 'role', None) != 'customer':
            raise forbidden('الطلب من حساب عميل بس')
        if not signed_in and _account_for_phone(data['phone']) is not None:
            raise ApiError(
                'الرقم ده عليه حساب بالفعل. اكتب كلمة السر بتاعته عشان الطلب '
                'يتسجل في حسابك.',
                status.HTTP_409_CONFLICT,
                code='account_exists',
            )

        quote = quote_for(data['specialty'])
        new_user = None

        with transaction.atomic():
            if signed_in:
                customer = Customer.objects.filter(user_id=request.user.pk).first()
                if customer is None:
                    raise forbidden('كمّل بيانات حسابك الأول')
            else:
                new_user, customer = _create_order_account(request, data)
            Consent.objects.get_or_create(
                user_id=customer.user_id,
                consent_type='terms_and_privacy',
                defaults={
                    'policy_version': str(
                        request.data.get('policy_version') or 'v2.0'
                    )[:20],
                    'ip_address': ip,
                    'user_agent': (request.META.get('HTTP_USER_AGENT') or '')[:1000],
                },
            )
            now = utc_now()
            job = Request.objects.create(
                customer_id=customer.pk,
                guest_name=data['name'],
                guest_phone=data['phone'],
                guest_linked_at=now,
                specialty=data['specialty'],
                custom_specialty_text=data.get('custom_specialty_text'),
                description=data['description'],
                location_text=data.get('resolved_location') or None,
                status='pending',
                **quote,
            )
            GuestSubmissionLog.objects.create(
                ip_address=ip, phone=data['phone'], created_at=utc_now()
            )
            if photo is not None:
                try:
                    path = save_private_upload(
                        photo, 'orders', detected or 'image/jpeg'
                    )
                except DjangoValidationError as exc:
                    raise bad_request(exc.messages[0])
                Request.objects.filter(pk=job.pk).update(order_photo_path=path)
                job.order_photo_path = path

        # Offered to the technicians; the first to accept gets it and the
        # customer is told who is coming.
        offers = broadcast_request(job.pk)
        job.refresh_from_db()

        payload = guest_payload(job, include_photo=False)
        body = {
            'message': 'تم تلقي طلبك وسيتم التواصل معك',
            'request': payload,
            'broadcast': {'sent_to': offers['count']},
            'assigned': False,
            'redirect': '/customer/my-requests.html',
            'account_created': new_user is not None,
        }
        if new_user is not None:
            from .views import _issue_token, _profile_for, _safe_user

            access, refresh = _issue_token(new_user)
            body.update(
                token=access,
                refresh=refresh,
                user=_safe_user(new_user),
                profile=_profile_for(new_user),
                must_change_password=False,
            )
        return Response(body, status=status.HTTP_201_CREATED)


class GuestRequestDetailView(APIView):
    """GET /api/guest/requests/<token> — status for the tracking-link holder."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, token):
        job = _request_for_token(token)
        payload = guest_payload(job)
        payload['tracking_url'] = '/track.html?token=' + token
        payload['photo_url'] = (
            f'/api/guest/requests/{token}/photo' if job.order_photo_path else None
        )
        return Response({'request': payload})


class GuestRequestCancelView(APIView):
    """POST /api/guest/requests/<token>/cancel — guest cancels before work starts."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, token):
        job = _request_for_token(token)
        if job.status not in ('pending', 'no_technician', 'assigned'):
            raise bad_request('مش ممكن تلغي الطلب في حالته الحالية')

        now = utc_now()
        with transaction.atomic():
            technician_id = job.technician_id
            updated = Request.objects.filter(
                pk=job.pk, status__in=('pending', 'no_technician', 'assigned')
            ).update(
                status='cancelled',
                cancel_reason='ألغى العميل الطلب من رابط المتابعة',
                updated_at=now,
            )
            if not updated:
                raise bad_request('مش ممكن تلغي الطلب في حالته الحالية')
            if technician_id:
                Technician.objects.filter(pk=technician_id).update(
                    availability='available'
                )
            RequestBroadcast.objects.filter(
                request_id=job.pk, status='sent'
            ).update(status='expired', responded_at=now)

        job.refresh_from_db()
        payload = guest_payload(job)
        payload['tracking_url'] = '/track.html?token=' + token
        return Response({'message': 'تم إلغاء الطلب', 'request': payload})


class GuestRequestPhotoView(APIView):
    """GET /api/guest/requests/<token>/photo — the guest's own uploaded photo."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, token):
        job = _request_for_token(token)
        if not job.order_photo_path:
            raise Http404('لا توجد صورة لهذا الطلب')
        name = resolve_stored_upload(job.order_photo_path)
        opened = open_stored_upload(name) if name else None
        if opened is None:
            raise Http404('لا توجد صورة لهذا الطلب')
        handle, mime = opened
        # Same allow-list as the upload check: an SVG (image/svg+xml) rendered
        # inline from this origin would be a stored-XSS vector, so anything but
        # a raster image is refused even if it somehow reached storage.
        if mime not in ALLOWED_ORDER_IMAGE_MIME_TYPES:
            handle.close()
            raise Http404('لا توجد صورة لهذا الطلب')
        response = FileResponse(handle, content_type=mime)
        response['Cache-Control'] = 'private, max-age=300'
        response['Content-Disposition'] = 'inline'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
