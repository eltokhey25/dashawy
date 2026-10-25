"""Public, unauthenticated read-only endpoints used by the landing page.

Everything here is either derived from the database or from deployment config.
No endpoint invents a rating, a count or a review.
"""

from __future__ import annotations

from django.db.models import Avg
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import bad_request
from .ratelimit import client_ip, throttle
from .models import (
    Consent,
    Request,
    Review,
    ServicePricing,
    Setting,
    Specialty,
    Technician,
)
from .site_config import (
    PUBLIC_AREA_KEYS,
    PUBLIC_AREA_LABELS,
    get_site_config,
    whatsapp_link,
)
from .validation import AREAS_BY_KEY


def _public_cache(response, seconds=60):
    response['Cache-Control'] = f'public, max-age={seconds}'
    return response


class PublicConfigView(APIView):
    """GET /api/public/config — contact/social config for the frontend."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        cfg = get_site_config()
        return _public_cache(
            Response(
                {
                    'site_url': cfg['site_url'],
                    'site_name': cfg['site_name'],
                    'business_phone': cfg['business_phone'],
                    'business_whatsapp': cfg['business_whatsapp'],
                    'business_email': cfg['business_email'],
                    'contact_address': cfg['contact_address'],
                    'social_facebook': cfg['social_facebook'],
                    'social_instagram': cfg['social_instagram'],
                    'social_tiktok': cfg['social_tiktok'],
                    'commission_percent': cfg['commission_percent_label'],
                    'whatsapp_url': whatsapp_link(),
                    'areas': cfg['areas'],
                }
            )
        )


class PublicSettingsView(APIView):
    """Backwards-compatible shape for ``Ui.applySiteSettings``."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        cfg = get_site_config()
        tagline = (
            Setting.objects.filter(key='site_tagline')
            .values_list('value', flat=True)
            .first()
            or ''
        )
        return _public_cache(
            Response(
                {
                    'settings': {
                        'site_name': cfg['site_name'],
                        'site_tagline': tagline,
                        'contact_address': cfg['contact_address'],
                        'contact_phone': cfg['business_phone'],
                        'contact_email': cfg['business_email'],
                        'whatsapp': cfg['business_whatsapp'],
                        'facebook_url': cfg['social_facebook'],
                        'instagram_url': cfg['social_instagram'],
                        'tiktok_url': cfg['social_tiktok'],
                    }
                }
            )
        )


class PublicSpecialtiesView(APIView):
    """GET /api/specialties — enabled services with their indicative price."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        prices = {
            row['service_key']: row['price']
            for row in ServicePricing.objects.values('service_key', 'price', 'is_active')
            if row['is_active'] is not False
        }
        rows = list(
            Specialty.objects.filter(enabled=True).order_by('sort_order', 'id')
        )
        if not rows:
            from .site_config import STATIC_SPECIALTIES

            services = [
                {**item, 'price': _price(prices.get(item['key']))}
                for item in STATIC_SPECIALTIES
            ]
        else:
            services = [
                {
                    'key': row.key,
                    'label': row.label,
                    'icon': row.icon,
                    'price': _price(prices.get(row.key)),
                }
                for row in rows
            ]
        return _public_cache(Response({'specialties': services, 'services': services}))


def _price(raw):
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class PublicAreasView(APIView):
    """GET /api/alexandria-areas — the district dropdowns."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        # The public order form intentionally offers the well-known districts;
        # the full list stays available to the signed-in wizard.
        areas = [
            {'key': key, 'label': PUBLIC_AREA_LABELS[key]}
            for key in PUBLIC_AREA_KEYS
        ] or [
            {'key': key, 'label': AREAS_BY_KEY[key]['label']}
            for key in AREAS_BY_KEY
        ]
        return _public_cache(Response({'areas': areas, 'all_areas': list(AREAS_BY_KEY.values())}))


class PublicStatsView(APIView):
    """GET /api/public/stats — counters backed only by real rows.

    Each counter has an explicit threshold. A counter whose data is below the
    threshold is returned with ``visible: false`` so the landing page can hide
    it rather than showing a misleading number.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    COMPLETED_THRESHOLD = 10
    TECHNICIANS_THRESHOLD = 3
    RATING_THRESHOLD = 3
    RESPONSE_THRESHOLD = 10

    def get(self, request):
        completed = Request.objects.filter(status='completed').count()
        technicians = Technician.objects.filter(
            approval_status='approved', user__blocked=False
        ).count()

        review_qs = Review.objects.filter(
            hidden=False, request__status='completed'
        )
        rating_count = review_qs.count()
        average = review_qs.aggregate(value=Avg('rating'))['value']
        average = round(float(average), 1) if average is not None else None

        durations = []
        for created_at, assigned_at in Request.objects.filter(
            assigned_at__isnull=False
        ).values_list('created_at', 'assigned_at'):
            if created_at and assigned_at:
                minutes = (assigned_at - created_at).total_seconds() / 60
                if minutes >= 0:
                    durations.append(minutes)
        avg_response = (
            round(sum(durations) / len(durations)) if durations else None
        )

        items = [
            {
                'key': 'completed',
                'value': completed,
                'suffix': '+',
                'label': 'طلب مكتمل',
                'visible': completed >= self.COMPLETED_THRESHOLD,
            },
            {
                'key': 'technicians',
                'value': technicians,
                'suffix': '+',
                'label': 'فني معتمد',
                'visible': technicians >= self.TECHNICIANS_THRESHOLD,
            },
            {
                'key': 'rating',
                'value': average,
                'suffix': ' / 5',
                'label': 'متوسط تقييم العملاء',
                'visible': (
                    average is not None and rating_count >= self.RATING_THRESHOLD
                ),
            },
            {
                'key': 'response',
                'value': avg_response,
                'suffix': ' دقيقة',
                'label': 'متوسط وقت تعيين الفني',
                'visible': (
                    avg_response is not None
                    and len(durations) >= self.RESPONSE_THRESHOLD
                ),
            },
        ]
        return _public_cache(
            Response(
                {
                    'items': items,
                    'visible': any(item['visible'] for item in items),
                    'raw': {
                        'completed': completed,
                        'approved_technicians': technicians,
                        'reviews': rating_count,
                        'average_rating': average,
                        'response_samples': len(durations),
                    },
                }
            )
        )


class PublicReviewsView(APIView):
    """GET /api/public/reviews — real, non-hidden reviews with a comment."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get('limit', 6)), 1), 12)
        except (TypeError, ValueError):
            limit = 6

        rows = (
            Review.objects.filter(
                hidden=False,
                request__status='completed',
                comment__isnull=False,
            )
            .exclude(comment='')
            .select_related('customer', 'technician', 'request')
            .order_by('-created_at', '-id')[:limit]
        )
        reviews = []
        for row in rows:
            name = (row.customer.name or '').strip() if row.customer_id else ''
            parts = name.split()
            reviewer = parts[0] if parts else 'عميل'
            if len(parts) > 1:
                reviewer += ' ' + parts[1][0] + '.'
            location = ''
            if row.request and row.request.location_text:
                location = row.request.location_text.split('—')[0].strip()
            reviews.append(
                {
                    'rating': row.rating,
                    'comment': row.comment,
                    'created_at': row.created_at,
                    'reviewer_name': reviewer,
                    'technician_name': row.technician.name if row.technician_id else '',
                    'area': location,
                }
            )
        return _public_cache(Response({'reviews': reviews, 'count': len(reviews)}))


CONSENT_TYPES = frozenset(
    {
        'privacy_policy',
        'terms_and_privacy',
        'terms_privacy_sensitive_documents',
    }
)


class ConsentView(APIView):
    """POST /api/consent — record an explicit consent for a signed-in account."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        throttle(request, 'consent', limit=20, window_seconds=3600)
        data = request.data if hasattr(request.data, 'get') else {}
        consent_type = str(data.get('consent_type') or '').strip()
        policy_version = str(data.get('policy_version') or 'v1.0').strip()
        if consent_type not in CONSENT_TYPES:
            raise bad_request('نوع الموافقة غير صالح')
        if len(policy_version) > 20:
            raise bad_request('نسخة السياسة غير صالحة')

        Consent.objects.create(
            user_id=request.user.pk,
            consent_type=consent_type,
            policy_version=policy_version,
            ip_address=client_ip(request),
            user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:1000],
        )
        return Response({'message': 'تم تسجيل الموافقة'}, status=201)
