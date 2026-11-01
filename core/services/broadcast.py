"""Offer a new request to technicians; the first one to accept gets it.

Python port of ``backend/services/broadcast.js``. A new request stays
``pending`` and every eligible technician receives a ``request_broadcasts`` row
with status ``sent``. The technician dashboard lists those offers
(``incoming_for_technician``) with accept and decline buttons:

* accept: the request moves to ``assigned`` with that technician, a Start PIN is
  minted, every other live offer is closed (``expired``) and the customer is
  told who is coming;
* decline: only that technician's offer closes; the request stays open for
  everyone else and is never re-offered to them.

The legacy auto-assign service (``assignment.assign_technician``) is kept but
no longer runs when a request is created.
"""

from django.db import transaction

from ..models import Request, RequestBroadcast, Technician, utc_now
from .assignment import GENERALIST_SPECIALTY, declined_technician_ids
from .payments import over_debt_limit_ids
from .start_pin import generate_start_pin

SENT = 'sent'
ACCEPTED = 'accepted'
DECLINED = 'declined'
EXPIRED = 'expired'

# Statuses a request can be offered out from. 'no_technician' is re-opened.
BROADCASTABLE_STATUSES = ('pending', 'no_technician')


class BroadcastError(Exception):
    """An accept/decline that cannot go through; carries an HTTP status."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def eligible_technicians(request):
    """Approved, available technicians for this request's trade.

    Exact specialty matches and generalists are both offered the job: with
    first-accept-wins there is no reason to hold a generalist back while a
    specialist may not answer. Technicians who declined this request are left
    out so a refusal is honoured.
    """
    # A technician whose unpaid commission reached the ceiling is not offered
    # new work until they settle with the platform.
    excluded = declined_technician_ids(request) | over_debt_limit_ids()
    specialties = {request.specialty, GENERALIST_SPECIALTY}
    return list(
        Technician.objects.filter(
            availability='available',
            approval_status='approved',
            specialty__in=specialties,
        ).exclude(pk__in=excluded)
    )


def broadcast_request(request_id):
    """Offer ``request_id`` to every eligible technician.

    Safe to call again: existing ``sent`` rows get a fresh ``sent_at``,
    ``expired`` rows are re-opened, and ``declined`` rows are left alone.
    Returns ``{"count": <live offers>, "technician_ids": [...]}``.
    """
    request = Request.objects.filter(pk=request_id).first()
    if request is None or request.status not in BROADCASTABLE_STATUSES:
        return {'count': 0, 'technician_ids': []}

    now = utc_now()
    newly_offered = []
    with transaction.atomic():
        if request.status == 'no_technician':
            Request.objects.filter(pk=request.pk).update(
                status='pending', updated_at=now
            )
            request.status = 'pending'

        for technician in eligible_technicians(request):
            row = RequestBroadcast.objects.filter(
                request_id=request.pk, technician_id=technician.pk
            ).first()
            if row is None:
                RequestBroadcast.objects.create(
                    request_id=request.pk,
                    technician_id=technician.pk,
                    status=SENT,
                    sent_at=now,
                )
                newly_offered.append(technician.user_id)
            elif row.status in (SENT, EXPIRED):
                if row.status == EXPIRED:
                    newly_offered.append(technician.user_id)
                RequestBroadcast.objects.filter(pk=row.pk).update(
                    status=SENT, sent_at=now, responded_at=None
                )

    if newly_offered:
        from .notifications import notify_new_offer

        notify_new_offer(newly_offered, request)

    live = list(
        RequestBroadcast.objects.filter(request_id=request.pk, status=SENT)
        .values_list('technician_id', flat=True)
    )
    return {'count': len(live), 'technician_ids': live}


def accept_broadcast(broadcast_id, technician):
    """First technician to accept wins. Returns the assigned ``Request``.

    Raises :class:`BroadcastError` (404/409) when the offer is gone or another
    technician won the race; the guarded ``UPDATE`` is the real arbiter.
    """
    row = RequestBroadcast.objects.filter(
        pk=broadcast_id, technician_id=technician.pk
    ).first()
    if row is None:
        raise BroadcastError('عرض الطلب غير موجود', 404)

    request = Request.objects.filter(pk=row.request_id).first()
    if request is None:
        raise BroadcastError('الطلب غير موجود', 404)
    if row.status != SENT or request.status != 'pending':
        if request.status == 'cancelled':
            raise BroadcastError('العميل لغى الطلب', 409)
        if row.status == DECLINED:
            raise BroadcastError('إنت رفضت الطلب ده قبل كده', 409)
        raise BroadcastError('فني تاني قبل الطلب ده', 409)

    if technician.pk in over_debt_limit_ids():
        raise BroadcastError(
            'حسابك موقوف مؤقتاً بسبب تجاوز حد المديونية. يرجى سداد المستحقات.', 403
        )

    now = utc_now()
    with transaction.atomic():
        claimed = Request.objects.filter(pk=request.pk, status='pending').update(
            technician_id=technician.pk,
            status='assigned',
            assigned_at=now,
            updated_at=now,
            started_at=None,
            start_pin=generate_start_pin(),
            start_pin_attempts=0,
            start_pin_locked_until=None,
        )
        if not claimed:
            raise BroadcastError('فني تاني قبل الطلب ده', 409)
        RequestBroadcast.objects.filter(pk=row.pk).update(
            status=ACCEPTED, responded_at=now
        )
        RequestBroadcast.objects.filter(request_id=request.pk, status=SENT).exclude(
            pk=row.pk
        ).update(status=EXPIRED, responded_at=now)
        Technician.objects.filter(pk=technician.pk).update(availability='busy')

    request.refresh_from_db()
    return request


def decline_broadcast(broadcast_id, technician):
    """Close one technician's offer; the request stays open for the rest."""
    row = RequestBroadcast.objects.filter(
        pk=broadcast_id, technician_id=technician.pk
    ).first()
    if row is None:
        raise BroadcastError('عرض الطلب غير موجود', 404)
    updated = RequestBroadcast.objects.filter(pk=row.pk, status=SENT).update(
        status=DECLINED, responded_at=utc_now()
    )
    if not updated:
        raise BroadcastError('الطلب ده اتعامل معاه خلاص', 409)


def close_offers(request_id):
    """Expire every live offer, e.g. when the customer cancels."""
    RequestBroadcast.objects.filter(request_id=request_id, status=SENT).update(
        status=EXPIRED, responded_at=utc_now()
    )


def _area_only(location_text):
    """'سموحة — شارع ..، عمارة ..' -> 'سموحة'."""
    return (location_text or '').split(' — ')[0].strip() or None


def incoming_for_technician(technician):
    """Live offers for one technician, newest first."""
    rows = (
        RequestBroadcast.objects.filter(
            technician_id=technician.pk, status=SENT, request__status='pending'
        )
        .select_related('request', 'request__customer')
        .order_by('-sent_at', '-id')[:20]
    )
    offers = []
    for row in rows:
        job = row.request
        offers.append(
            {
                'broadcast_id': row.pk,
                'sent_at': row.sent_at.strftime('%Y-%m-%d %H:%M:%S'),
                'request_id': job.pk,
                'specialty': job.specialty,
                'custom_specialty_text': job.custom_specialty_text,
                'description': job.description,
                # Area only: the street/building and the phone are shared
                # with the technician who accepts, not with every offer.
                'location_text': _area_only(job.location_text),
                'status': job.status,
                'created_at': (
                    job.created_at.strftime('%Y-%m-%d %H:%M:%S')
                    if job.created_at
                    else None
                ),
                'service_price': job.service_price,
                'customer_name': job.display_customer_name,
                'customer_area': _area_only(job.location_text),
            }
        )
    return offers
