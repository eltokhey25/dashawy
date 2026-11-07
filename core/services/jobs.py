"""The order lifecycle steps shared by the API views and the Telegram bot.

Placing an order, starting a job with the doorstep code, finishing it and
handing an assigned job back all used to live inside the DRF views. The bot
needs the same transitions with the same guards, so they are here and both
callers raise the same :class:`core.exceptions.ApiError`.
"""

import math
import re
import secrets

from django.db import transaction
from django.db.models import F, Q
from rest_framework import status

from ..exceptions import ApiError, bad_request
from ..models import Request, utc_now
from .assignment import mark_declined, release_technician
from .broadcast import BroadcastError, accept_broadcast, broadcast_request
from .pricing import quote_for
from .start_pin import (
    START_PIN_ATTEMPTS,
    START_PIN_LENGTH,
    START_PIN_LOCK_MINUTES,
    generate_start_pin,
    start_pin_lock_until,
)

# Open orders one customer may have at once.
MAX_OPEN_ORDERS = 3
OPEN_STATUSES = ('pending', 'assigned', 'in_progress')
MAX_WORK_NOTES = 2000
MIN_WORK_NOTES = 5


def open_order_count(customer_id):
    return Request.objects.filter(customer_id=customer_id, status__in=OPEN_STATUSES).count()


def place_order(customer, data, preferred=None):
    """Create a customer's order from validated data and offer it out.

    ``data`` is ``CreateRequestSerializer.validated_data``. Returns
    ``(request, offers)`` where ``offers`` is the broadcast result.
    """
    if open_order_count(customer.pk) >= MAX_OPEN_ORDERS:
        raise bad_request(
            f'عندك {MAX_OPEN_ORDERS} طلبات شغالة بالفعل. '
            'استنى لحد ما تخلص أو ألغِ واحد منهم قبل ما تطلب تاني.'
        )
    created = Request.objects.create(
        customer_id=customer.pk,
        specialty=data['specialty'],
        custom_specialty_text=data.get('custom_specialty_text'),
        description=data['description'],
        location_text=data.get('resolved_location') or None,
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        status='pending',
        preferred_technician=preferred,
        **quote_for(data['specialty']),
    )
    offers = broadcast_request(created.pk)
    created.refresh_from_db()
    return created, offers


def accept_offer(technician, broadcast_id):
    """First technician to accept an offer wins; the customer is told."""
    if technician.availability == 'busy':
        raise bad_request('خلّص الطلب اللي معاك الأول')
    try:
        job = accept_broadcast(broadcast_id, technician)
    except BroadcastError as exc:
        raise ApiError(exc.message, exc.status_code)
    job = Request.objects.select_related('customer__user', 'technician').get(pk=job.pk)
    from .notifications import notify_request_accepted

    notify_request_accepted(job)
    return job


def start_job(technician, pk, submitted_pin):
    """Move an assigned job to ``in_progress`` after checking the door code.

    This is the **only** status transition into ``in_progress``. The assigned
    technician must submit the four-digit code the customer received; wrong
    guesses increment ``start_pin_attempts`` and lock the code after
    ``START_PIN_ATTEMPTS`` failures for ``START_PIN_LOCK_MINUTES``.
    """
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
    if job.start_pin_locked_until and job.start_pin_locked_until <= now:
        # The lock has expired; start the technician with a clean counter.
        Request.objects.filter(pk=job.pk, start_pin_locked_until__lte=now).update(
            start_pin_attempts=0, start_pin_locked_until=None
        )

    # Spend one attempt BEFORE looking at the code, in a single guarded
    # UPDATE. Checking first and counting afterwards let a burst of parallel
    # guesses all pass the check before any of them was counted, so the
    # five-attempt budget was no limit at all against 10,000 requests at once.
    spent = (
        Request.objects.filter(pk=job.pk, technician_id=technician.pk, status='assigned')
        .filter(Q(start_pin_locked_until__isnull=True) | Q(start_pin_locked_until__lte=now))
        .filter(start_pin_attempts__lt=START_PIN_ATTEMPTS)
        .update(start_pin_attempts=F('start_pin_attempts') + 1)
    )
    if not spent:
        job.refresh_from_db(fields=['start_pin_attempts', 'start_pin_locked_until', 'status'])
        if job.status != 'assigned':
            raise bad_request('لا يمكن بدء هذا الطلب في حالته الحالية')
        if not job.start_pin_locked_until or job.start_pin_locked_until <= now:
            # Attempts ran out without a lock being written yet (a parallel
            # request is about to); write it here too.
            Request.objects.filter(pk=job.pk, start_pin_locked_until__isnull=True).update(
                start_pin_locked_until=start_pin_lock_until(now)
            )
            job.refresh_from_db(fields=['start_pin_locked_until'])
        minutes = max(
            1, math.ceil((job.start_pin_locked_until - now).total_seconds() / 60)
        )
        raise ApiError(
            f'المحاولات مقفلة. حاول تاني بعد {minutes} دقيقة',
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    submitted = str(submitted_pin or '').strip()
    valid_shape = re.fullmatch(r'\d{%d}' % START_PIN_LENGTH, submitted) is not None
    if not valid_shape or not secrets.compare_digest(submitted, job.start_pin or ''):
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
        raise ApiError(f'الكود غلط. باقي لك {label}', status.HTTP_400_BAD_REQUEST)

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

    job = Request.objects.select_related('customer__user', 'technician').get(pk=job.pk)
    from .notifications import notify_job_started

    notify_job_started(job)
    return job


def complete_job(technician, pk, work_notes):
    """``in_progress`` -> ``completed``; frees the technician, tells the customer."""
    job = Request.objects.filter(pk=pk, technician_id=technician.pk).first()
    if job is None:
        raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
    if job.status != 'in_progress':
        raise bad_request('فقط طلبات العمل الجارية يمكن إكمالها')

    notes = str(work_notes or '').strip()[:MAX_WORK_NOTES]
    if len(notes) < MIN_WORK_NOTES:
        raise bad_request(f'يرجى كتابة ما تم إنجازه ({MIN_WORK_NOTES} أحرف على الأقل)')

    now = utc_now()
    with transaction.atomic():
        updated = Request.objects.filter(
            pk=job.pk, technician_id=technician.pk, status='in_progress'
        ).update(
            status='completed',
            completed_at=now,
            completed_by_technician_at=now,
            work_notes=notes,
            updated_at=now,
        )
        if not updated:
            raise bad_request('فقط طلبات العمل الجارية يمكن إكمالها')
        release_technician(technician.pk)

    job = Request.objects.select_related('customer__user', 'technician').get(pk=job.pk)
    from .notifications import notify_job_completed

    notify_job_completed(job)
    return job


def reject_assigned_job(technician, pk):
    """Hand an assigned job back and offer it to the other technicians.

    Returns the broadcast result (``{'count': .., 'technician_ids': [..]}``).
    """
    job = Request.objects.filter(pk=pk, technician_id=technician.pk).first()
    if job is None:
        raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
    if job.status != 'assigned':
        raise bad_request('لا يمكن رفض هذا الطلب في حالته الحالية')

    now = utc_now()
    released = Request.objects.filter(
        pk=job.pk, technician_id=technician.pk, status='assigned'
    ).update(technician_id=None, status='pending', assigned_at=None, updated_at=now)
    if not released:
        raise bad_request('لا يمكن رفض هذا الطلب في حالته الحالية')

    # Record the decline, free the technician and offer the request to
    # the remaining technicians again.
    mark_declined(job.pk, technician.pk)
    release_technician(technician.pk)
    offers = broadcast_request(job.pk)

    from .notifications import notify_technician_withdrew

    notify_technician_withdrew(Request.objects.select_related('customer__user').get(pk=job.pk))
    return offers
