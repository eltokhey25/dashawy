"""Per-request chat between the customer and the assigned technician.

Port of ``backend/routes/messages.routes.js``. Polling transport: the chat page
GETs the thread every few seconds. Only the request's customer and its assigned
technician may read or write; everyone else gets 403. Messages can be sent
while the job is ``assigned`` or ``in_progress``; the thread stays readable
afterwards.
"""

from django.db.models import Count
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError, bad_request, forbidden
from .models import Message, Request, utc_now
from .ratelimit import throttle
from .services.notifications import notify_chat_message
from .views import _body

SENDABLE_STATES = ('assigned', 'in_progress')
MAX_BODY_LENGTH = 2000
THREAD_PAGE_SIZE = 200


def _authorize(user, request_id):
    """Return ``(job, role)`` for the caller, or raise 404/400/403."""
    job = (
        Request.objects.filter(pk=request_id)
        .select_related('customer', 'technician')
        .first()
    )
    if job is None:
        raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
    if not job.technician_id:
        raise bad_request('لا يوجد فني مرتبط بالطلب')
    if job.customer and job.customer.user_id == user.pk:
        return job, 'customer'
    if job.technician and job.technician.user_id == user.pk:
        if job.technician.approval_status != 'approved' or job.technician.availability == 'offline':
            raise forbidden('هذا الحساب غير نشط. تواصل مع الإدارة.')
        return job, 'technician'
    raise forbidden('غير مصرح لك بالوصول لهذه المحادثة')


def _other(role):
    return 'technician' if role == 'customer' else 'customer'


def _message_json(message):
    return {
        'id': message.pk,
        'sender_id': message.sender_id,
        'sender_role': message.sender_role,
        'body': message.body,
        'read_at': message.read_at.strftime('%Y-%m-%d %H:%M:%S') if message.read_at else None,
        'created_at': (
            message.created_at.strftime('%Y-%m-%d %H:%M:%S') if message.created_at else None
        ),
    }


def _mark_read(job, role):
    Message.objects.filter(
        request_id=job.pk, sender_role=_other(role), read_at__isnull=True
    ).update(read_at=utc_now())


class MessageUnreadCountView(APIView):
    """GET /api/messages/unread-count — unread messages per request."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        role = request.user.role
        unread = Message.objects.filter(sender_role=_other(role), read_at__isnull=True)
        if role == 'customer':
            unread = unread.filter(request__customer__user_id=request.user.pk)
        elif role == 'technician':
            unread = unread.filter(request__technician__user_id=request.user.pk)
        else:
            unread = unread.none()
        rows = [
            {'request_id': row['request_id'], 'n': row['n']}
            for row in unread.values('request_id').annotate(n=Count('id')).order_by('request_id')
        ]
        return Response({'total': sum(r['n'] for r in rows), 'byRequest': rows})


class MessageThreadView(APIView):
    """GET/POST /api/messages/<request_id> — read or add to the thread."""

    permission_classes = [IsAuthenticated]

    def get(self, request, request_id):
        job, role = _authorize(request.user, request_id)
        latest = list(
            Message.objects.filter(request_id=job.pk).order_by('-created_at', '-id')[
                :THREAD_PAGE_SIZE
            ]
        )
        latest.reverse()
        _mark_read(job, role)
        peer = job.technician.user_id if role == 'customer' else job.customer.user_id
        return Response(
            {
                'messages': [_message_json(m) for m in latest],
                'role': role,
                'peerUserId': peer,
                'canSend': job.status in SENDABLE_STATES,
                'requestStatus': job.status,
            }
        )

    def post(self, request, request_id):
        raw = _body(request).get('body')
        if not isinstance(raw, str) or not raw.strip():
            raise bad_request('الرسالة لا يمكن أن تكون فارغة')
        if len(raw) > MAX_BODY_LENGTH:
            raise bad_request('الرسالة طويلة جدًا (الحد الأقصى 2000 حرف)')
        job, role = _authorize(request.user, request_id)
        if job.status not in SENDABLE_STATES:
            raise bad_request('لا يمكن إرسال رسائل في هذه المرحلة')
        throttle(
            request, 'chat-send', limit=30, window_seconds=60,
            identifier=f'user-{request.user.pk}',
        )
        message = Message.objects.create(
            request_id=job.pk, sender_id=request.user.pk, sender_role=role, body=raw.strip()
        )
        if role == 'customer':
            notify_chat_message(job.technician.user_id, 'العميل', message.body, job.pk, 'technician')
        else:
            notify_chat_message(
                job.customer.user_id, job.technician.name or 'الفني', message.body, job.pk, 'customer'
            )
        return Response({'message': _message_json(message)}, status=status.HTTP_201_CREATED)


class MessageMarkReadView(APIView):
    """POST /api/messages/<request_id>/read — mark the other side's messages read."""

    permission_classes = [IsAuthenticated]

    def post(self, request, request_id):
        job, role = _authorize(request.user, request_id)
        _mark_read(job, role)
        return Response({'ok': True})
