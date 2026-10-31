"""Technician profile, the public technician pages, privacy requests and the
Telegram link status.

Ported from the deleted Node routes the pages still call:

* technician: ``GET/PUT profile-details``, ``POST profile-image``,
  ``POST portfolio`` and ``DELETE portfolio/<id>``;
* public: ``GET public/technicians`` and ``GET public/technicians/<id>``;
* any signed-in user: ``POST data-subject/request``,
  ``GET data-subject/my-requests`` and ``GET auth/telegram-link``.

Profile pictures and portfolio photos are public (served by the
``/uploads/technicians/profiles|portfolio/`` route); they go through the same
upload storage and magic-byte check as every other upload.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from django.db.models import Avg, Count, Q
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError, bad_request
from .models import (
    DataSubjectRequest,
    Request,
    Review,
    Technician,
    TechnicianPortfolio,
    TechnicianProfile,
    utc_now,
)
from .permissions import IsTechnician
from .serializers import enabled_specialty_keys
from .upload_validators import resolve_stored_upload, save_private_upload, validate_order_image
from .validation import is_non_empty_string, resolve_location
from .views import _body, _json_ready, _technician_for

MAX_PORTFOLIO_IMAGES = 5
MAX_BIO_LENGTH = 1000
MAX_PUBLIC_REVIEWS = 50
DATA_REQUEST_TYPES = ('access', 'rectification', 'erasure', 'restriction', 'portability')


def _when(value):
    return value.strftime('%Y-%m-%d %H:%M:%S') if value else None


def _profile_json(technician_id):
    row = TechnicianProfile.objects.filter(technician_id=technician_id).values().first()
    if row is None:
        return None
    row = _json_ready(row)
    row['technician_id'] = row.pop('technician', row.get('technician_id'))
    return row


def _portfolio_json(technician_id):
    return [
        {
            'id': p.pk,
            'technician_id': technician_id,
            'image_path': p.image_path,
            'caption': p.caption,
            'created_at': _when(p.created_at),
        }
        for p in TechnicianPortfolio.objects.filter(technician_id=technician_id).order_by(
            '-created_at', '-id'
        )
    ]


def _stats(technician_id, visible_only=False):
    reviews = Review.objects.filter(technician_id=technician_id)
    if visible_only:
        reviews = reviews.exclude(hidden=True)
    agg = reviews.aggregate(avg=Avg('rating'), n=Count('id'))
    return {
        'review_count': agg['n'] or 0,
        'avg_rating': round(agg['avg'], 1) if agg['avg'] else None,
        'completed_count': Request.objects.filter(
            technician_id=technician_id, status='completed'
        ).count(),
    }


def _delete_upload(stored_path, required_prefix):
    """Remove an earlier image, but only inside this technician's folder."""
    name = resolve_stored_upload(stored_path)
    if name and name.startswith(required_prefix):
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 - a stale file must not fail the request
            pass


def _validated_image(upload):
    try:
        return validate_order_image(upload)
    except DjangoValidationError as exc:
        raise bad_request(exc.messages[0] if exc.messages else 'الصورة غير صالحة')


# ---------------------------------------------------------------------------
# Technician's own profile
# ---------------------------------------------------------------------------


class TechnicianProfileDetailsView(APIView):
    """GET/PUT /api/technician/profile-details."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def get(self, request):
        tech = _technician_for(request.user)
        recent = [
            {
                **_json_ready(row),
                'request_id': row['request_id'],
                'customer_name': row['customer__name'],
            }
            for row in Review.objects.filter(technician_id=tech.pk)
            .order_by('-created_at', '-id')
            .values(
                'id', 'request_id', 'customer_id', 'technician_id', 'rating', 'comment',
                'technician_reply', 'replied_at', 'created_at', 'hidden', 'customer__name',
            )[:20]
        ]
        for row in recent:
            row.pop('customer__name', None)
        return Response(
            {
                'technician': {
                    'id': tech.pk,
                    'name': tech.name,
                    'phone': tech.phone,
                    'specialty': tech.specialty,
                    'location_text': tech.location_text,
                    'latitude': tech.latitude,
                    'longitude': tech.longitude,
                    'availability': tech.availability,
                    'approval_status': tech.approval_status,
                    'approved_at': _when(getattr(tech, 'approved_at', None)),
                    'created_at': _when(tech.created_at),
                },
                'profile': _profile_json(tech.pk),
                'portfolio': _portfolio_json(tech.pk),
                'stats': _stats(tech.pk),
                'recentReviews': recent,
            }
        )

    def put(self, request):
        tech = _technician_for(request.user)
        data = _body(request)
        updates = {}
        if 'bio' in data:
            if not is_non_empty_string(data.get('bio'), MAX_BIO_LENGTH):
                raise bad_request('النبذة التعريفية غير صالحة')
            updates['bio'] = data['bio'].strip()
        if 'years_experience' in data:
            try:
                years = int(str(data.get('years_experience')))
            except (TypeError, ValueError):
                years = -1
            if not 0 <= years <= 60:
                raise bad_request('عدد سنوات الخبرة غير صالح')
            updates['years_experience'] = years
        if not updates:
            raise bad_request('لا يوجد بيانات للتحديث')
        updates['updated_at'] = utc_now()
        TechnicianProfile.objects.update_or_create(technician_id=tech.pk, defaults=updates)
        return Response({'message': 'تم تحديث البيانات', 'profile': _profile_json(tech.pk)})


class TechnicianProfileImageView(APIView):
    """POST /api/technician/profile-image — multipart ``profile_image``."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request):
        tech = _technician_for(request.user)
        upload = request.FILES.get('profile_image')
        if upload is None:
            raise bad_request('لم يتم إرفاق صورة')
        mime = _validated_image(upload)
        folder = f'technicians/profiles/tech-{tech.pk}'
        previous = (
            TechnicianProfile.objects.filter(technician_id=tech.pk)
            .values_list('profile_image_path', flat=True)
            .first()
        )
        path = save_private_upload(upload, folder, mime)
        TechnicianProfile.objects.update_or_create(
            technician_id=tech.pk,
            defaults={'profile_image_path': path, 'updated_at': utc_now()},
        )
        if previous and previous != path:
            _delete_upload(previous, folder + '/')
        return Response({'message': 'تم تحديث الصورة الشخصية', 'profile': _profile_json(tech.pk)})


class TechnicianPortfolioView(APIView):
    """POST /api/technician/portfolio — multipart ``portfolio_images`` (max 5)."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request):
        tech = _technician_for(request.user)
        files = request.FILES.getlist('portfolio_images')
        if not files:
            raise bad_request('لم يتم إرفاق أي صور')
        if len(files) > MAX_PORTFOLIO_IMAGES:
            raise bad_request(f'الحد الأقصى {MAX_PORTFOLIO_IMAGES} صور')
        caption = str(request.data.get('caption') or '').strip() or None
        if caption and len(caption) > 300:
            raise bad_request('الوصف طويل جداً')
        mimes = [_validated_image(f) for f in files]

        folder = f'technicians/portfolio/tech-{tech.pk}'
        existing = TechnicianPortfolio.objects.filter(technician_id=tech.pk)
        overflow = existing.count() + len(files) - MAX_PORTFOLIO_IMAGES
        if overflow > 0:
            # The gallery keeps the newest five: drop the oldest to make room.
            for old in existing.order_by('created_at', 'id')[:overflow]:
                _delete_upload(old.image_path, folder + '/')
                old.delete()

        added = []
        for upload, mime in zip(files, mimes):
            path = save_private_upload(upload, folder, mime)
            row = TechnicianPortfolio.objects.create(
                technician_id=tech.pk, image_path=path, caption=caption
            )
            added.append({'id': row.pk, 'image_path': path, 'caption': caption})
        return Response(
            {
                'message': 'تم إضافة الصور إلى معرض الأعمال',
                'added': added,
                'portfolio': _portfolio_json(tech.pk),
            },
            status=status.HTTP_201_CREATED,
        )


class TechnicianPortfolioItemView(APIView):
    """DELETE /api/technician/portfolio/<id>."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def delete(self, request, pk):
        tech = _technician_for(request.user)
        row = TechnicianPortfolio.objects.filter(pk=pk, technician_id=tech.pk).first()
        if row is None:
            raise ApiError('الصورة غير موجودة', status.HTTP_404_NOT_FOUND)
        _delete_upload(row.image_path, f'technicians/portfolio/tech-{tech.pk}/')
        row.delete()
        return Response({'message': 'تم حذف الصورة'})


# ---------------------------------------------------------------------------
# Public technician pages
# ---------------------------------------------------------------------------


def _public_technician(tech, stats, profile=None):
    row = {
        'id': tech.pk,
        'name': tech.name,
        'specialty': tech.specialty,
        'location_text': tech.location_text,
        'latitude': tech.latitude,
        'longitude': tech.longitude,
        'availability': tech.availability,
        'approved_at': _when(getattr(tech, 'approved_at', None)),
        'created_at': _when(tech.created_at),
        'avg_rating': stats['avg_rating'],
        'review_count': stats['review_count'],
        'completed_count': stats['completed_count'],
    }
    if profile is not None:
        row.update(
            bio=profile.bio,
            years_experience=profile.years_experience,
            profile_image_path=profile.profile_image_path,
        )
    else:
        row.update(bio=None, years_experience=None, profile_image_path=None)
    return row


class PublicTechnicianListView(APIView):
    """GET /api/public/technicians?area=&specialty= — approved technicians."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        rows = Technician.objects.filter(approval_status='approved').select_related('profile')
        area = request.query_params.get('area')
        if area:
            try:
                rows = rows.filter(location_text=resolve_location(area, ''))
            except KeyError:
                raise bad_request('المنطقة غير صالحة')
        specialty = request.query_params.get('specialty')
        if specialty:
            if specialty not in enabled_specialty_keys():
                raise bad_request('نوع الخدمة غير صالح')
            rows = rows.filter(specialty=specialty)

        technicians = []
        for tech in rows:
            profile = getattr(tech, 'profile', None)
            technicians.append(_public_technician(tech, _stats(tech.pk, True), profile))
        technicians.sort(
            key=lambda t: (-(t['avg_rating'] or 0), -t['review_count'], t['name'] or '')
        )
        return Response({'technicians': technicians})


class PublicTechnicianDetailView(APIView):
    """GET /api/public/technicians/<id> — profile, gallery and visible reviews."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, pk):
        tech = Technician.objects.filter(pk=pk, approval_status='approved').first()
        if tech is None:
            raise ApiError('الفني غير موجود', status.HTTP_404_NOT_FOUND)
        stats = _stats(tech.pk, True)
        profile = TechnicianProfile.objects.filter(technician_id=tech.pk).first()
        reviews = [
            {
                'id': r.pk,
                'rating': r.rating,
                'comment': r.comment,
                'technician_reply': r.technician_reply,
                'replied_at': _when(r.replied_at),
                'created_at': _when(r.created_at),
            }
            for r in Review.objects.filter(technician_id=tech.pk)
            .exclude(hidden=True)
            .order_by('-created_at', '-id')[:MAX_PUBLIC_REVIEWS]
        ]
        return Response(
            {
                'technician': _public_technician(tech, stats),
                'profile': (
                    {
                        'bio': profile.bio,
                        'years_experience': profile.years_experience,
                        'profile_image_path': profile.profile_image_path,
                        'updated_at': _when(profile.updated_at),
                    }
                    if profile
                    else None
                ),
                'portfolio': [
                    {k: v for k, v in item.items() if k != 'technician_id'}
                    for item in _portfolio_json(tech.pk)
                ],
                'stats': stats,
                'reviews': reviews,
            }
        )


# ---------------------------------------------------------------------------
# Privacy requests and Telegram status
# ---------------------------------------------------------------------------


class DataSubjectRequestView(APIView):
    """POST /api/data-subject/request — {request_type, note?}."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = _body(request)
        kind = data.get('request_type')
        if kind not in DATA_REQUEST_TYPES:
            raise bad_request('نوع الطلب غير صالح')
        note = str(data.get('note') or '').strip()[:1000] or None
        DataSubjectRequest.objects.create(user_id=request.user.pk, request_type=kind, admin_note=note)
        return Response(
            {'message': 'تم استلام طلبك. سيتم الرد خلال 30 يوماً كحد أقصى وفقاً للقانون.'},
            status=status.HTTP_201_CREATED,
        )


class DataSubjectMyRequestsView(APIView):
    """GET /api/data-subject/my-requests."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = []
        for row in DataSubjectRequest.objects.filter(user_id=request.user.pk).order_by(
            '-requested_at'
        ).values():
            row = _json_ready(row)
            row['user_id'] = row.pop('user', row.get('user_id'))
            rows.append(row)
        return Response({'requests': rows})


class TelegramLinkStatusView(APIView):
    """GET /api/auth/telegram-link — whether this account has Telegram linked.

    Linking itself is paused (see telegram-link.html); this only reports the
    status the dashboards show.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {'linked': bool(request.user.telegram_chat_id), 'bot_username': None, 'enabled': False}
        )
