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
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError, bad_request
from .models import GuestSubmissionLog, Request, RequestBroadcast, Technician, utc_now
from .ratelimit import client_ip
from .serializers import GuestCreateRequestSerializer
from .services.assignment import assign_technician
from .services.pricing import price_status, quote_for
from .site_config import get_site_config
from .upload_validators import save_private_upload, validate_order_image

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
    """POST /api/guest/requests — create an order without an account."""

    authentication_classes = []
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

        token = secrets.token_urlsafe(32)
        quote = quote_for(data['specialty'])

        with transaction.atomic():
            job = Request.objects.create(
                customer_id=None,
                guest_name=data['name'],
                guest_phone=data['phone'],
                guest_token_hash=hash_token(token),
                guest_token_created_at=utc_now(),
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

        assignment = assign_technician(job.pk)
        job.refresh_from_db()

        payload = guest_payload(job, include_photo=False)
        cfg = get_site_config()
        tracking_url = f"{cfg['site_url']}/track.html?token={token}"
        payload['tracking_url'] = '/track.html?token=' + token

        return Response(
            {
                'message': (
                    'تم استلام طلبك وربطك بفني'
                    if assignment.get('assigned')
                    else 'تم استلام طلبك، وهنحوّله لأقرب فني متاح في أسرع وقت'
                ),
                'tracking_token': token,
                'tracking_url': tracking_url,
                'request': payload,
                'assigned': bool(assignment.get('assigned')),
            },
            status=status.HTTP_201_CREATED,
        )


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
        from pathlib import Path

        from django.conf import settings
        import magic

        relative = job.order_photo_path.lstrip('/')
        if relative.startswith('uploads/'):
            relative = relative[len('uploads/'):]
        target = (Path(settings.MEDIA_ROOT) / relative).resolve()
        try:
            target.relative_to(Path(settings.MEDIA_ROOT).resolve())
        except ValueError:
            raise Http404('لا توجد صورة لهذا الطلب')
        if not target.is_file():
            raise Http404('لا توجد صورة لهذا الطلب')
        mime = magic.from_file(str(target), mime=True)
        if not str(mime).startswith('image/'):
            raise Http404('لا توجد صورة لهذا الطلب')
        response = FileResponse(open(target, 'rb'), content_type=mime)
        response['Cache-Control'] = 'private, max-age=300'
        response['Content-Disposition'] = 'inline'
        return response
