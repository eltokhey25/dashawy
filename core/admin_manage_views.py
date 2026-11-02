"""Admin management: admin accounts, technicians, customers, requests,
reviews and data-subject requests.

Ported from ``backend/routes/admin.routes.js``. Each view carries the section
permission the Node route required (super admin for admin accounts).
"""

import json

from django.db import transaction
from django.db.models import Avg, Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth_backend import hash_password
from .exceptions import ApiError, bad_request, forbidden
from .models import (
    Consent,
    Customer,
    DataSubjectRequest,
    Request,
    RequestBroadcast,
    Review,
    Technician,
    User,
    utc_now,
)
from .permissions import AdminPermission, IsSuperAdmin
from .serializers import AdminRequestSerializer, enabled_specialty_keys
from .services.assignment import GENERALIST_SPECIALTY, release_technician
from .services.broadcast import broadcast_request, close_offers
from .services.start_pin import generate_start_pin
from .services.uploads import delete_upload_later, delete_user_uploads
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
from .views import _body, _json_ready

VALID_PERMISSIONS = (
    'stats',
    'customers',
    'technicians',
    'requests',
    'reviews',
    'specialties',
    'settings',
    'data_requests',
)
AVAILABILITY = ('available', 'busy', 'offline')


def _not_found(message):
    return ApiError(message, status.HTTP_404_NOT_FOUND)


def _clean_permissions(value):
    if not isinstance(value, list):
        return []
    return [p for p in value if p in VALID_PERMISSIONS]


def _admin_json(user):
    try:
        permissions = json.loads(user.permissions or '[]')
    except (TypeError, ValueError):
        permissions = []
    return {
        'id': user.pk,
        'email': user.email,
        'is_super_admin': 1 if user.is_super_admin else 0,
        'blocked': 1 if user.blocked else 0,
        'must_change_password': 1 if user.must_change_password else 0,
        'created_at': user.created_at.strftime('%Y-%m-%d %H:%M:%S') if user.created_at else None,
        'permissions': permissions if isinstance(permissions, list) else [],
    }


def _technician_json(technician):
    row = _json_ready(Technician.objects.filter(pk=technician.pk).values().first())
    row['user_id'] = row.pop('user', row.get('user_id'))
    row['email'] = technician.user.email if technician.user_id else None
    return row


# ---------------------------------------------------------------------------
# Admin accounts (super admin only)
# ---------------------------------------------------------------------------


def _admin_or_404(pk):
    user = User.objects.filter(pk=pk, role='admin').first()
    if user is None:
        raise _not_found('المسؤول غير موجود')
    return user


class AdminAccountListView(APIView):
    """GET/POST /api/admin/admins."""

    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        admins = User.objects.filter(role='admin').order_by('-is_super_admin', 'id')
        return Response({'admins': [_admin_json(u) for u in admins]})

    def post(self, request):
        data = _body(request)
        email = data.get('email')
        if not is_valid_email(email):
            raise bad_request('البريد الإلكتروني غير صالح')
        problem = password_policy_error(data.get('password'))
        if problem:
            raise bad_request(problem)
        email = normalize_email(email)
        if User.objects.filter(email=email).exists():
            raise bad_request('هذا البريد الإلكتروني مستخدم بالفعل')
        permissions = _clean_permissions(data.get('permissions'))
        is_super = data.get('is_super_admin') in (True, 1, '1')
        user = User.objects.create(
            email=email,
            password_hash=hash_password(data['password']),
            role='admin',
            is_super_admin=is_super,
            must_change_password=False,
            permissions=json.dumps(permissions),
        )
        return Response(
            {
                'message': 'تم إضافة المسؤول بنجاح',
                'admin': {
                    'id': user.pk,
                    'email': email,
                    'is_super_admin': is_super,
                    'permissions': permissions,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class AdminAccountDetailView(APIView):
    """PUT (permissions) / DELETE /api/admin/admins/<id>."""

    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def put(self, request, pk):
        target = _admin_or_404(pk)
        if target.is_super_admin:
            raise forbidden('لا يمكن تعديل صلاحيات المدير العام')
        permissions = _clean_permissions(_body(request).get('permissions'))
        User.objects.filter(pk=target.pk).update(permissions=json.dumps(permissions))
        return Response({'message': 'تم تحديث الصلاحيات', 'permissions': permissions})

    def delete(self, request, pk):
        if int(pk) == request.user.pk:
            raise bad_request('مش ممكن تحذف حسابك بنفسك')
        target = _admin_or_404(pk)
        if target.is_super_admin and User.objects.filter(
            role='admin', is_super_admin=True
        ).count() <= 1:
            raise bad_request('مش ممكن تحذف آخر مدير عام. عيّن مدير عام تاني الأول.')
        target.delete()
        return Response({'message': 'تم حذف المسؤول'})


class AdminAccountResetPasswordView(APIView):
    """POST /api/admin/admins/<id>/reset-password — {new_password}."""

    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def post(self, request, pk):
        password = _body(request).get('new_password')
        problem = password_policy_error(password)
        if problem:
            raise bad_request(problem)
        target = _admin_or_404(pk)
        User.objects.filter(pk=target.pk).update(password_hash=hash_password(password))
        return Response({'message': 'تم تغيير كلمة المرور. يمكن للمسؤول تسجيل الدخول مباشرة.'})


class AdminAccountToggleView(APIView):
    """POST /api/admin/admins/<id>/toggle-active."""

    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def post(self, request, pk):
        target = _admin_or_404(pk)
        if target.is_super_admin:
            raise forbidden('لا يمكن إيقاف حساب المدير العام')
        if target.pk == request.user.pk:
            raise forbidden('لا يمكنك إيقاف حسابك الشخصي')
        blocked = not bool(target.blocked)
        User.objects.filter(pk=target.pk).update(blocked=blocked)
        return Response(
            {
                'message': 'تم إيقاف حساب المسؤول' if blocked else 'تم تفعيل حساب المسؤول',
                'blocked': blocked,
            }
        )


# ---------------------------------------------------------------------------
# Technicians
# ---------------------------------------------------------------------------


class AdminTechnicianPendingView(APIView):
    """GET /api/admin/technicians/pending — sign-ups awaiting approval."""

    permission_classes = [IsAuthenticated, AdminPermission('technicians')]

    def get(self, request):
        rows = []
        for tech in (
            Technician.objects.filter(approval_status='pending')
            .select_related('user')
            .order_by('-created_at')
        ):
            row = _technician_json(tech)
            row['account_created_at'] = (
                tech.user.created_at.strftime('%Y-%m-%d %H:%M:%S') if tech.user.created_at else None
            )
            rows.append(row)
        return Response({'technicians': rows})


def _technician_fields(data, partial):
    """Validate the editable technician fields; returns the column updates."""
    updates = {}
    if 'name' in data or not partial:
        if not is_valid_name(data.get('name')):
            raise bad_request('الاسم غير صالح')
        updates['name'] = data['name'].strip()
    if 'phone' in data or not partial:
        if not is_valid_phone(data.get('phone')):
            raise bad_request('رقم الهاتف غير صالح')
        updates['phone'] = normalize_phone(data['phone'])
    if data.get('specialty') not in (None, ''):
        if data['specialty'] not in set(enabled_specialty_keys()) | {GENERALIST_SPECIALTY}:
            raise bad_request('التخصص غير صالح')
        updates['specialty'] = data['specialty']
    elif not partial:
        updates['specialty'] = GENERALIST_SPECIALTY
    if data.get('availability') not in (None, ''):
        if data['availability'] not in AVAILABILITY:
            raise bad_request('حالة التوفر غير صالحة')
        updates['availability'] = data['availability']
    if data.get('area_key') not in (None, ''):
        try:
            updates['location_text'] = resolve_location(data['area_key'], '')
        except KeyError:
            raise bad_request('المنطقة غير صالحة')
    elif 'location_text' in data and data['location_text'] not in (None, ''):
        if not is_non_empty_string(data['location_text'], 300):
            raise bad_request('وصف الموقع غير صالح')
        updates['location_text'] = data['location_text'].strip()
    lat, lng = data.get('latitude'), data.get('longitude')
    if lat not in (None, '') or lng not in (None, ''):
        coords = parse_lat_lng(lat, lng)
        if coords is None:
            raise bad_request('إحداثيات الموقع غير صالحة')
        updates['latitude'], updates['longitude'] = coords
    return updates


def create_technician(request):
    """POST /api/admin/technicians — an approved technician made by an admin."""
    data = _body(request)
    email = data.get('email')
    if not is_valid_email(email):
        raise bad_request('البريد الإلكتروني غير صالح')
    email = normalize_email(email)
    if User.objects.filter(email=email).exists():
        raise bad_request('هذا البريد الإلكتروني مستخدم بالفعل')
    problem = password_policy_error(data.get('password'))
    if problem:
        raise bad_request(problem)
    fields = _technician_fields(data, partial=False)
    fields.setdefault('availability', 'available')
    with transaction.atomic():
        user = User.objects.create(
            email=email, password_hash=hash_password(data['password']), role='technician'
        )
        tech = Technician.objects.create(
            user=user, documents_verified=True, approval_status='approved', **fields
        )
    return Response(
        {'message': 'تم إضافة الفني بنجاح', 'technician': _technician_json(tech)},
        status=status.HTTP_201_CREATED,
    )


def update_technician(request, pk):
    """PUT /api/admin/technicians/<id> — profile fields, email or password."""
    tech = Technician.objects.filter(pk=pk).select_related('user').first()
    if tech is None:
        raise _not_found('الفني غير موجود')
    data = _body(request)
    updates = _technician_fields(data, partial=True)
    with transaction.atomic():
        if data.get('email') not in (None, ''):
            if not is_valid_email(data['email']):
                raise bad_request('البريد الإلكتروني غير صالح')
            email = normalize_email(data['email'])
            if User.objects.filter(email=email).exclude(pk=tech.user_id).exists():
                raise bad_request('هذا البريد الإلكتروني مستخدم بالفعل')
            User.objects.filter(pk=tech.user_id).update(email=email)
        if data.get('password') not in (None, ''):
            problem = password_policy_error(data['password'])
            if problem:
                raise bad_request(problem)
            User.objects.filter(pk=tech.user_id).update(password_hash=hash_password(data['password']))
        if updates:
            Technician.objects.filter(pk=tech.pk).update(**updates)
    tech.refresh_from_db()
    return Response(
        {
            'message': 'تم تحديث بيانات الفني',
            'technician': _technician_json(tech),
            'pickup': {'offered': 0, 'request_id': None},
        }
    )


def delete_technician(request, pk):
    """DELETE /api/admin/technicians/<id> — removes the account too."""
    tech = Technician.objects.filter(pk=pk).first()
    if tech is None:
        raise _not_found('الفني غير موجود')
    with transaction.atomic():
        delete_user_uploads(tech.user_id)
        User.objects.filter(pk=tech.user_id).delete()
    return Response({'message': 'تم حذف الفني'})


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


class AdminCustomerDetailView(APIView):
    """GET/DELETE /api/admin/customers/<id>."""

    permission_classes = [IsAuthenticated, AdminPermission('customers')]

    def get(self, request, pk):
        customer = Customer.objects.filter(pk=pk).select_related('user').first()
        if customer is None:
            raise _not_found('العميل غير موجود')
        row = _json_ready(Customer.objects.filter(pk=pk).values().first())
        row['user_id'] = row.pop('user', row.get('user_id'))
        row['email'] = customer.user.email
        row['account_created_at'] = (
            customer.user.created_at.strftime('%Y-%m-%d %H:%M:%S')
            if customer.user.created_at
            else None
        )
        jobs = Request.objects.filter(customer_id=pk)
        stats = {
            'total': jobs.count(),
            'completed': jobs.filter(status='completed').count(),
            'cancelled': jobs.filter(status='cancelled').count(),
            'active': jobs.filter(status__in=('pending', 'assigned', 'in_progress')).count(),
        }
        recent = jobs.select_related('customer', 'technician').order_by('-created_at')[:10]
        return Response(
            {
                'customer': row,
                'stats': stats,
                'recentRequests': AdminRequestSerializer(recent, many=True).data,
            }
        )

    def delete(self, request, pk):
        customer = Customer.objects.filter(pk=pk).first()
        if customer is None:
            raise _not_found('العميل غير موجود')
        with transaction.atomic():
            for job in Request.objects.filter(customer_id=pk).exclude(technician_id=None):
                if job.status in ('assigned', 'in_progress'):
                    release_technician(job.technician_id)
            delete_user_uploads(customer.user_id)
            User.objects.filter(pk=customer.user_id).delete()
        return Response({'message': 'تم حذف العميل وجميع طلباته'})


class AdminCustomerBlockView(APIView):
    """PATCH /api/admin/customers/<id>/block — {blocked}."""

    permission_classes = [IsAuthenticated, AdminPermission('customers')]

    def patch(self, request, pk):
        customer = Customer.objects.filter(pk=pk).first()
        if customer is None:
            raise _not_found('العميل غير موجود')
        blocked = bool(_body(request).get('blocked'))
        # The account flag is what sign-in checks, so set both.
        Customer.objects.filter(pk=pk).update(blocked=blocked)
        User.objects.filter(pk=customer.user_id).update(blocked=blocked)
        return Response({'message': 'تم إيقاف العميل' if blocked else 'تم تفعيل العميل'})


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _request_or_404(pk):
    job = Request.objects.filter(pk=pk).first()
    if job is None:
        raise _not_found('الطلب غير موجود')
    return job


class AdminRequestAssignView(APIView):
    """POST /api/admin/requests/<id>/assign — {technician_id}."""

    permission_classes = [IsAuthenticated, AdminPermission('requests')]

    def post(self, request, pk):
        job = _request_or_404(pk)
        if job.status not in ('pending', 'no_technician'):
            raise bad_request('فقط طلب بانتظار فني يمكن تعيين فني له')
        tech = Technician.objects.filter(pk=_body(request).get('technician_id')).first()
        if tech is None:
            raise _not_found('الفني غير موجود')
        if tech.approval_status != 'approved':
            raise bad_request('هذا الفني غير معتمد')
        now = utc_now()
        with transaction.atomic():
            updated = Request.objects.filter(
                pk=job.pk, status__in=('pending', 'no_technician')
            ).update(
                technician_id=tech.pk,
                status='assigned',
                assigned_at=now,
                updated_at=now,
                start_pin=generate_start_pin(),
                start_pin_attempts=0,
                start_pin_locked_until=None,
                started_at=None,
            )
            if not updated:
                raise bad_request('فقط طلب بانتظار فني يمكن تعيين فني له')
            Technician.objects.filter(pk=tech.pk).update(availability='busy')
            RequestBroadcast.objects.filter(request_id=job.pk, status='sent').update(
                status='expired', responded_at=now
            )
        return Response(
            {
                'message': f'تم تعيين {tech.name} على الطلب',
                'technician': {'id': tech.pk, 'name': tech.name, 'phone': tech.phone},
            }
        )


class AdminRequestUnassignView(APIView):
    """POST /api/admin/requests/<id>/unassign — back to pending."""

    permission_classes = [IsAuthenticated, AdminPermission('requests')]

    def post(self, request, pk):
        job = _request_or_404(pk)
        if job.status not in ('assigned', 'in_progress'):
            raise bad_request('لا يمكن إلغاء التعيين في هذه الحالة')
        with transaction.atomic():
            Request.objects.filter(pk=job.pk).update(
                technician_id=None,
                status='pending',
                assigned_at=None,
                start_pin=None,
                updated_at=utc_now(),
            )
            release_technician(job.technician_id)
        return Response({'message': 'تم إلغاء التعيين وعاد الطلب إلى الانتظار'})


class AdminRequestRebroadcastView(APIView):
    """POST /api/admin/requests/<id>/rebroadcast — offer it to technicians again."""

    permission_classes = [IsAuthenticated, AdminPermission('requests')]

    def post(self, request, pk):
        job = _request_or_404(pk)
        if job.status not in ('pending', 'no_technician'):
            raise bad_request('لا يمكن إعادة بث الطلب في حالته الحالية')
        result = broadcast_request(job.pk)
        return Response(
            {
                'message': (
                    f"تم إعادة بث الطلب إلى {result['count']} فني"
                    if result['count']
                    else 'لم يتم العثور على فنيين متاحين — تأكد من التخصص أو أضف فنيين جدد'
                ),
                'sent_to': result['count'],
                'technician_ids': result['technician_ids'],
                'reason': None,
            }
        )


class AdminRequestQueuedView(APIView):
    """GET /api/admin/requests/queued — pending requests nobody has taken."""

    permission_classes = [IsAuthenticated, AdminPermission('requests')]

    def get(self, request):
        rows = (
            Request.objects.filter(status='pending')
            .select_related('customer', 'technician')
            .order_by('created_at', 'id')
        )
        data = AdminRequestSerializer(rows, many=True).data
        return Response({'requests': data, 'count': len(data)})


class AdminRequestBroadcastsView(APIView):
    """GET /api/admin/requests/<id>/broadcasts — who was offered it."""

    permission_classes = [IsAuthenticated, AdminPermission('requests')]

    def get(self, request, pk):
        _request_or_404(pk)
        rows = []
        for row in (
            RequestBroadcast.objects.filter(request_id=pk)
            .select_related('technician')
            .order_by('-sent_at', '-id')
        ):
            rows.append(
                {
                    'id': row.pk,
                    'request_id': pk,
                    'technician_id': row.technician_id,
                    'status': row.status,
                    'sent_at': row.sent_at.strftime('%Y-%m-%d %H:%M:%S') if row.sent_at else None,
                    'responded_at': (
                        row.responded_at.strftime('%Y-%m-%d %H:%M:%S')
                        if row.responded_at
                        else None
                    ),
                    'technician_name': row.technician.name,
                    'technician_phone': row.technician.phone,
                }
            )
        return Response({'broadcasts': rows, 'count': len(rows)})


class AdminRequestDeleteView(APIView):
    """DELETE /api/admin/requests/<id> — remove the request permanently."""

    permission_classes = [IsAuthenticated, AdminPermission('requests')]

    def delete(self, request, pk):
        job = _request_or_404(pk)
        with transaction.atomic():
            if job.status in ('assigned', 'in_progress'):
                release_technician(job.technician_id)
            close_offers(job.pk)
            delete_upload_later(job.order_photo_path)
            job.delete()
        return Response({'message': 'تم حذف الطلب نهائياً'})


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def _reviews_filtered(params):
    rows = Review.objects.select_related('customer__user', 'technician')
    if params.get('technician_id'):
        rows = rows.filter(technician_id=params['technician_id'])
    if params.get('rating'):
        rows = rows.filter(rating=params['rating'])
    if params.get('hidden') in ('0', '1'):
        rows = rows.filter(hidden=params['hidden'] == '1')
    search = (params.get('search') or '').strip()
    if search:
        rows = rows.filter(
            Q(comment__icontains=search)
            | Q(customer__name__icontains=search)
            | Q(technician__name__icontains=search)
        )
    return rows


class AdminReviewListView(APIView):
    """GET /api/admin/reviews?technician_id&rating&hidden&search&limit&offset."""

    permission_classes = [IsAuthenticated, AdminPermission('reviews')]

    def get(self, request):
        params = request.query_params
        try:
            limit = min(max(int(params.get('limit', 50)), 1), 200)
        except ValueError:
            limit = 50
        try:
            offset = max(int(params.get('offset', 0)), 0)
        except ValueError:
            offset = 0
        rows = _reviews_filtered(params)
        total = rows.count()
        reviews = []
        for review in rows.order_by('-created_at', '-id')[offset : offset + limit]:
            row = _json_ready(Review.objects.filter(pk=review.pk).values().first())
            row['request_id'] = row.pop('request', row.get('request_id'))
            row['customer_id'] = row.pop('customer', row.get('customer_id'))
            row['technician_id'] = review.technician_id
            row['hidden'] = 1 if review.hidden else 0
            row['customer_name'] = review.customer.name
            row['customer_phone'] = review.customer.phone
            row['customer_email'] = review.customer.user.email
            row['technician_name'] = review.technician.name
            reviews.append(row)
        return Response({'reviews': reviews, 'total': total, 'limit': limit, 'offset': offset})


class AdminReviewStatsView(APIView):
    """GET /api/admin/reviews/stats."""

    permission_classes = [IsAuthenticated, AdminPermission('reviews')]

    def get(self, request):
        visible = Review.objects.exclude(hidden=True)
        avg = visible.aggregate(a=Avg('rating'))['a']
        return Response(
            {
                'total': Review.objects.count(),
                'hidden': Review.objects.filter(hidden=True).count(),
                'avgRating': round(avg, 2) if avg else 0,
                'byRating': {str(i): visible.filter(rating=i).count() for i in range(1, 6)},
            }
        )


class AdminReviewActionView(APIView):
    """POST /api/admin/reviews/<id>/(hide|unhide)."""

    permission_classes = [IsAuthenticated, AdminPermission('reviews')]

    def post(self, request, pk, action):
        if not Review.objects.filter(pk=pk).exists():
            raise _not_found('المراجعة غير موجودة')
        if action == 'hide':
            reason = str(_body(request).get('reason') or '').strip()[:500] or None
            Review.objects.filter(pk=pk).update(
                hidden=True, hidden_reason=reason, hidden_at=utc_now(), hidden_by=request.user.pk
            )
            return Response({'message': 'تم إخفاء المراجعة'})
        Review.objects.filter(pk=pk).update(
            hidden=False, hidden_reason=None, hidden_at=None, hidden_by=None
        )
        return Response({'message': 'تم إظهار المراجعة'})


class AdminReviewDeleteView(APIView):
    """DELETE /api/admin/reviews/<id>."""

    permission_classes = [IsAuthenticated, AdminPermission('reviews')]

    def delete(self, request, pk):
        deleted, _ = Review.objects.filter(pk=pk).delete()
        if not deleted:
            raise _not_found('المراجعة غير موجودة')
        return Response({'message': 'تم حذف المراجعة نهائياً'})


# ---------------------------------------------------------------------------
# Data-subject requests (privacy law)
# ---------------------------------------------------------------------------


class AdminDataRequestListView(APIView):
    """GET /api/admin/data-subject-requests."""

    permission_classes = [IsAuthenticated, AdminPermission('data_requests')]

    def get(self, request):
        rows = []
        for item in DataSubjectRequest.objects.select_related('user').order_by('-requested_at'):
            row = _json_ready(DataSubjectRequest.objects.filter(pk=item.pk).values().first())
            row['user_id'] = row.pop('user', row.get('user_id'))
            row['user_email'] = item.user.email
            rows.append(row)
        return Response({'requests': rows})


class AdminDataRequestUpdateView(APIView):
    """PATCH /api/admin/data-subject-requests/<id> — {status, admin_note}."""

    permission_classes = [IsAuthenticated, AdminPermission('data_requests')]

    def patch(self, request, pk):
        if not DataSubjectRequest.objects.filter(pk=pk).exists():
            raise _not_found('الطلب غير موجود')
        data = _body(request)
        DataSubjectRequest.objects.filter(pk=pk).update(
            status=str(data.get('status') or 'completed')[:20],
            admin_note=data.get('admin_note') or None,
            completed_at=utc_now(),
        )
        return Response({'message': 'تم تحديث الطلب'})


class AdminUserEraseView(APIView):
    """DELETE /api/admin/users/<id>/erase — delete every record of a user."""

    permission_classes = [IsAuthenticated, AdminPermission('data_requests')]

    def delete(self, request, pk):
        user = User.objects.filter(pk=pk).first()
        if user is None:
            raise _not_found('المستخدم غير موجود')
        if user.role == 'admin':
            raise forbidden('لا يمكن مسح حساب مسؤول من هنا')
        with transaction.atomic():
            # Files first: the rows that name them are about to cascade away.
            delete_user_uploads(user.pk)
            if user.role == 'technician':
                tech = Technician.objects.filter(user_id=pk).first()
                if tech:
                    Request.objects.filter(technician_id=tech.pk).delete()
            Consent.objects.filter(user_id=pk).delete()
            DataSubjectRequest.objects.filter(user_id=pk).delete()
            # Customer and their requests go with the account (CASCADE).
            user.delete()
        return Response({'message': 'تم حذف جميع بيانات المستخدم نهائياً'})
