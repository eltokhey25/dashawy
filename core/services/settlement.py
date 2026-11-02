"""Settle cash confirmations that one side (or both) never finished.

The commission is booked only when the request settles, so a technician who
simply never reports the cash would never owe it. After
``settlement_grace_hours`` (48 by default) a completed job is settled on
whatever is known, most reliable first:

* the customer's figure when only the customer reported (the technician had
  their chance to contest it);
* the technician's figure when only the technician reported (it is against
  their own interest to overstate);
* the specialty's guide price when nobody reported anything.

Disputes stay with the admin, and a job with no guide price and no reports is
left in the admin's pending queue.
"""

from datetime import timedelta

from django.db.models import Exists, OuterRef

from ..models import Request, WalletTransaction, utc_now
from .payments import SETTLED, _settle, settlement_grace_hours

AUTO_NOTES = {
    'customer': 'تسوية تلقائية على مبلغ العميل: الفني لم يسجّل المبلغ خلال {hours:g} ساعة',
    'technician': 'تسوية تلقائية على مبلغ الفني: العميل لم يؤكد المبلغ خلال {hours:g} ساعة',
    'guide': 'تسوية تلقائية على السعر الاسترشادي: لم يسجّل الطرفان المبلغ خلال {hours:g} ساعة',
}


def stale_requests(now=None):
    hours = settlement_grace_hours()
    cutoff = (now or utc_now()) - timedelta(hours=hours)
    booked = WalletTransaction.objects.filter(
        request_id=OuterRef('pk'), type=WalletTransaction.TYPE_DEBT
    )
    return (
        Request.objects.filter(
            status='completed',
            technician_id__isnull=False,
            completed_at__lte=cutoff,
        )
        .exclude(amount_status__in=SETTLED + ('disputed',))
        .exclude(payment_status__in=('confirmed', 'refunded'))
        .annotate(booked=Exists(booked))
        .filter(booked=False)
        .order_by('completed_at', 'id')
    )


def settle_stale_reports(now=None):
    """Settle every stalled confirmation; returns what was done per request."""
    hours = settlement_grace_hours()
    settled, skipped = [], []
    for job in stale_requests(now):
        if job.customer_reported_amount is not None:
            basis, amount = 'customer', job.customer_reported_amount
        elif job.technician_reported_amount is not None:
            basis, amount = 'technician', job.technician_reported_amount
        elif job.service_price and float(job.service_price) > 0:
            basis, amount = 'guide', job.service_price
        else:
            skipped.append(job.pk)
            continue
        result = _settle(job.pk, amount, 'resolved', note=AUTO_NOTES[basis].format(hours=hours))
        if result.get('status') == 'already_finalized':
            continue
        settled.append({'request_id': job.pk, 'basis': basis, 'amount': result['final_amount']})
    return {'ok': True, 'settled': settled, 'skipped': skipped, 'grace_hours': hours}
