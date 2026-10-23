"""Automatic technician assignment.

Python port of ``backend/services/assignment.js``. The Express service was
written when every technician on the platform handled every dish service, so it
deliberately ignored the request's specialty. Phase 3 requires specialty
matching, while the live technician pool still consists mostly of generalists
(``dish_general``). The compromise implemented here is:

* prefer technicians whose specialty exactly matches the request;
* when there are none, fall back to ``dish_general`` generalists;
* never assign a technician working another trade.

The service reports outcomes instead of raising: "nobody is available" is a
normal result for the caller, not an exceptional condition.
"""

import json
import math

from django.db import transaction

from ..models import Request, RequestBroadcast, Technician, utc_now

EARTH_RADIUS_KM = 6371.0
GENERALIST_SPECIALTY = 'dish_general'

NO_TECHNICIAN = 'no_technician_available'
REQUEST_NOT_FOUND = 'request_not_found'
REQUEST_NOT_PENDING = 'request_not_pending'


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in kilometres, or ``None`` with a missing point.

    A missing coordinate must not be read as ``(0, 0)``: that would look like a
    confident (and very wrong) distance. Returning ``None`` keeps candidates
    with coordinates ordered ahead of candidates without them.
    """
    values = (lat1, lng1, lat2, lng2)
    if any(value is None or value == '' for value in values):
        return None
    try:
        lat1, lng1, lat2, lng2 = (float(value) for value in values)
    except (TypeError, ValueError):
        return None

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def declined_technician_ids(request):
    """Technician ids that must not be offered this request again.

    The legacy column ``requests.declined_technicians`` is the primary record;
    ``request_broadcasts`` is unioned in so a decline written by either path is
    honoured. Malformed JSON is treated as "nobody declined" rather than
    breaking assignment for the request.
    """
    ids = set()
    try:
        stored = json.loads(request.declined_technicians or '[]')
    except (TypeError, ValueError):
        stored = []
    if isinstance(stored, list):
        for value in stored:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue

    ids.update(
        RequestBroadcast.objects.filter(
            request_id=request.pk, status='declined'
        ).values_list('technician_id', flat=True)
    )
    return ids


def _candidate_technicians(request, excluded_ids):
    """Approved, available technicians eligible for this request.

    Exact specialty matches win outright. Generalists are used only when no
    exact match is available, so a dish installer is not sent to a receiver
    job while a receiver technician is free.
    """
    available = (
        Technician.objects.filter(
            availability='available', approval_status='approved'
        )
        .exclude(pk__in=excluded_ids)
    )

    exact = list(available.filter(specialty=request.specialty))
    if exact:
        return exact
    if request.specialty != GENERALIST_SPECIALTY:
        return list(available.filter(specialty=GENERALIST_SPECIALTY))
    return []


def _rank_key(technician, request):
    """Sort key: nearest first when possible, otherwise oldest registration.

    ``created_at`` can be equal for rows inserted in the same second (the live
    schema uses second precision), so the id is the deterministic tie-breaker.
    """
    distance = None
    if request.latitude is not None and request.longitude is not None:
        distance = haversine_km(
            request.latitude,
            request.longitude,
            technician.latitude,
            technician.longitude,
        )
    registered = technician.created_at
    return (
        0 if distance is not None else 1,
        distance if distance is not None else 0.0,
        registered.timestamp() if registered else 0.0,
        technician.pk,
    )


def assign_technician(request_id):
    """Choose and lock in one technician for ``request_id``.

    Returns ``{"assigned": True, "technician_id": ...}`` on success or
    ``{"assigned": False, "reason": ...}`` otherwise. A request that is not
    pending is left untouched.
    """
    request = Request.objects.filter(pk=request_id).first()
    if request is None:
        return {'assigned': False, 'reason': REQUEST_NOT_FOUND}
    if request.status != 'pending':
        return {'assigned': False, 'reason': REQUEST_NOT_PENDING}

    excluded = declined_technician_ids(request)
    candidates = _candidate_technicians(request, excluded)
    if not candidates:
        return {'assigned': False, 'reason': NO_TECHNICIAN}

    candidates.sort(key=lambda technician: _rank_key(technician, request))
    chosen = candidates[0]
    now = utc_now()

    with transaction.atomic():
        # Conditional update: if another worker assigned (or the customer
        # cancelled) between the read above and here, zero rows change and the
        # loser reports the race instead of overwriting a valid assignment.
        claimed = Request.objects.filter(pk=request.pk, status='pending').update(
            technician_id=chosen.pk,
            status='assigned',
            assigned_at=now,
            updated_at=now,
            started_at=None,
            start_pin_attempts=0,
            start_pin_locked_until=None,
        )
        if not claimed:
            return {'assigned': False, 'reason': REQUEST_NOT_PENDING}

        Technician.objects.filter(pk=chosen.pk).update(availability='busy')

        # Keep the broadcast table consistent with the direct assignment. It is
        # also the durable record used by the "already declined" filter above.
        RequestBroadcast.objects.update_or_create(
            request_id=request.pk,
            technician_id=chosen.pk,
            defaults={'status': 'accepted', 'responded_at': now},
        )

    return {'assigned': True, 'technician_id': chosen.pk}


def release_technician(technician_id):
    """Return a technician to the available pool, if they still exist."""
    if technician_id is None:
        return
    Technician.objects.filter(pk=technician_id).update(availability='available')


def mark_declined(request_id, technician_id):
    """Record that a technician refused this request.

    Both the legacy JSON column and the broadcast row are written because the
    two are read by different legacy screens.
    """
    request = Request.objects.filter(pk=request_id).first()
    if request is None:
        return

    excluded = declined_technician_ids(request)
    excluded.add(int(technician_id))
    now = utc_now()

    Request.objects.filter(pk=request.pk).update(
        declined_technicians=json.dumps(sorted(excluded)),
        updated_at=now,
    )
    RequestBroadcast.objects.update_or_create(
        request_id=request.pk,
        technician_id=technician_id,
        defaults={'status': 'declined', 'responded_at': now},
    )


def reassign_after_rejection(request_id, technician_id):
    """Exclude the rejecting technician, free them, then run assignment again.

    The caller must have already moved the request back to ``pending`` and
    cleared its technician; this function deliberately only owns the
    bookkeeping around the rejected technician.
    """
    mark_declined(request_id, technician_id)
    release_technician(technician_id)
    return assign_technician(request_id)
