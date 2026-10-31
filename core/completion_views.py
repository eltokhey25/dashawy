"""Finishing a job: completion, the cash confirmation and reviews.

Ported from the deleted Node routes the dashboards still call:

* technician: ``POST requests/<id>/complete`` (work notes, frees them),
  ``POST requests/<id>/report-payment`` (cash received) and
  ``POST reviews/<id>/reply``;
* customer: ``POST requests/<id>/report-payment`` (cash paid),
  ``POST requests/<id>/pay`` (cash declared), ``POST requests/<id>/review``,
  ``GET requests/pending-actions`` (drives the mandatory review prompt),
  ``GET pending-reviews`` and ``GET my-reviews``.
"""

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError, bad_request
from .guest_views import _label_for
from .models import Request, Review, utc_now
from .permissions import IsCustomer, IsTechnician
from .services.assignment import release_technician
from .services.payments import (
    PaymentError,
    customer_report,
    ensure_wallet,
    settlement_view,
    technician_report,
)
from .validation import is_non_empty_string
from .views import _body, _customer_for, _json_ready, _technician_for

MAX_COMMENT_LENGTH = 1000
MAX_REPLY_LENGTH = 1000
MAX_WORK_NOTES = 2000


def _not_found(message='الطلب غير موجود'):
    return ApiError(message, status.HTTP_404_NOT_FOUND)


def _payment_error(exc):
    return ApiError(exc.message, exc.status_code)


def _review_json(review, technician_name=None):
    row = _json_ready(
        Review.objects.filter(pk=review.pk).values().first()
    )
    row['request_id'] = row.pop('request', row.get('request_id'))
    row['customer_id'] = row.pop('customer', row.get('customer_id'))
    row['technician_id'] = row.pop('technician', row.get('technician_id'))
    if technician_name is not None:
        row['technician_name'] = technician_name
    return row


# ---------------------------------------------------------------------------
# Technician
# ---------------------------------------------------------------------------


class TechnicianRequestCompleteView(APIView):
    """POST /api/technician/requests/<id>/complete — in_progress -> completed."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request, pk):
        technician = _technician_for(request.user)
        job = Request.objects.filter(pk=pk, technician_id=technician.pk).first()
        if job is None:
            raise _not_found()
        if job.status != 'in_progress':
            raise bad_request('فقط طلبات العمل الجارية يمكن إكمالها')

        notes = str(_body(request).get('work_notes') or '').strip()[:MAX_WORK_NOTES]
        if len(notes) < 5:
            raise bad_request('يرجى كتابة ما تم إنجازه (5 أحرف على الأقل)')

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

        return Response(
            {
                'message': 'تم إكمال المهمة بنجاح',
                'status': 'completed',
                'pickup': {'offered': 0, 'request_id': None},
            }
        )


class TechnicianReportPaymentView(APIView):
    """POST /api/technician/requests/<id>/report-payment — cash received."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request, pk):
        technician = _technician_for(request.user)
        try:
            result = technician_report(pk, technician.pk, _body(request).get('amount'))
        except PaymentError as exc:
            raise _payment_error(exc)
        body = {'ok': True, **result, **settlement_view(pk)}
        body['total_debt'] = ensure_wallet(technician.pk).total_debt
        return Response(body)


class TechnicianReviewReplyView(APIView):
    """POST /api/technician/reviews/<id>/reply — one reply per review."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def post(self, request, pk):
        technician = _technician_for(request.user)
        reply = _body(request).get('reply')
        if not is_non_empty_string(reply, MAX_REPLY_LENGTH):
            raise bad_request('الرد غير صالح')
        review = Review.objects.filter(pk=pk, technician_id=technician.pk).first()
        if review is None:
            raise _not_found('التقييم غير موجود')
        updated = Review.objects.filter(
            pk=review.pk, technician_reply__isnull=True
        ).update(technician_reply=reply.strip(), replied_at=utc_now())
        if not updated:
            raise bad_request('تم الرد على هذا التقييم بالفعل')
        return Response({'message': 'تم إضافة الرد', 'review': _review_json(review)})


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


class CustomerReportPaymentView(APIView):
    """POST /api/customer/requests/<id>/report-payment — cash paid."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def post(self, request, pk):
        customer = _customer_for(request.user)
        try:
            result = customer_report(pk, customer.pk, _body(request).get('amount'))
        except PaymentError as exc:
            raise _payment_error(exc)
        return Response({'ok': True, **result, **settlement_view(pk)})


class CustomerPayView(APIView):
    """POST /api/customer/requests/<id>/pay — declare a cash payment."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def post(self, request, pk):
        customer = _customer_for(request.user)
        job = Request.objects.filter(pk=pk, customer_id=customer.pk).first()
        if job is None:
            raise _not_found()
        if job.status != 'completed':
            raise bad_request('الدفع متاح بعد إكمال الطلب فقط')
        if job.payment_status == 'confirmed':
            raise bad_request('تم تأكيد دفع هذا الطلب بالفعل')
        if job.payment_status == 'refunded':
            raise bad_request('تم استرداد هذا الطلب')
        # Cash is the only method: there is no payment gateway.
        if _body(request).get('method') != 'cash':
            raise bad_request('طريقة الدفع غير متاحة')
        Request.objects.filter(pk=job.pk).update(
            payment_status='pending_verification',
            payment_method='cash',
            updated_at=utc_now(),
        )
        return Response({'message': 'تم تسجيل الدفع — بنأكده خلال وقت قصير'})


class CustomerReviewView(APIView):
    """POST /api/customer/requests/<id>/review — {rating, comment?}."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def post(self, request, pk):
        customer = _customer_for(request.user)
        data = _body(request)
        try:
            rating = int(data.get('rating'))
        except (TypeError, ValueError):
            rating = 0
        if str(data.get('rating')).strip() != str(rating) or not 1 <= rating <= 5:
            raise bad_request('التقييم غير صالح')
        comment = data.get('comment')
        if comment not in (None, ''):
            if not is_non_empty_string(comment, MAX_COMMENT_LENGTH):
                raise bad_request('التعليق غير صالح')
            comment = comment.strip()
        else:
            comment = None

        job = Request.objects.filter(pk=pk, customer_id=customer.pk).first()
        if job is None:
            raise _not_found()
        if not job.technician_id:
            raise bad_request('لا يوجد فني يمكن تقييمه لهذا الطلب')
        if job.status != 'completed':
            raise bad_request('يمكن التقييم بعد إكمال الطلب فقط')
        try:
            with transaction.atomic():
                review = Review.objects.create(
                    request_id=job.pk,
                    customer_id=customer.pk,
                    technician_id=job.technician_id,
                    rating=rating,
                    comment=comment,
                )
        except IntegrityError:
            raise bad_request('تم تقييم هذا الطلب بالفعل')
        return Response(
            {'message': 'شكراً لتقييمك', 'review': _review_json(review)},
            status=status.HTTP_201_CREATED,
        )


def _unreviewed_completed(customer):
    return (
        Request.objects.filter(
            customer_id=customer.pk,
            status='completed',
            technician_id__isnull=False,
            review__isnull=True,
        )
        .select_related('technician')
        .order_by('-completed_at', '-id')
    )


class CustomerPendingActionsView(APIView):
    """GET /api/customer/requests/pending-actions — jobs awaiting a review."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def get(self, request):
        customer = _customer_for(request.user)
        rows = []
        for job in _unreviewed_completed(customer):
            label = (
                job.custom_specialty_text
                if job.specialty == 'other' and job.custom_specialty_text
                else _label_for(job.specialty)
            )
            rows.append(
                {
                    'id': job.pk,
                    'specialty': job.specialty,
                    'specialty_label': label,
                    'status': job.status,
                    'amount_status': job.amount_status,
                    'technician_reported_amount': job.technician_reported_amount,
                    'customer_reported_amount': job.customer_reported_amount,
                    'final_amount': job.final_amount,
                    'work_notes': job.work_notes,
                    'completed_at': (
                        job.completed_at.strftime('%Y-%m-%d %H:%M:%S')
                        if job.completed_at
                        else None
                    ),
                    'location_text': job.location_text,
                    'technician_name': job.technician.name if job.technician else None,
                    'has_review': 0,
                }
            )
        return Response({'requests': rows, 'count': len(rows)})


class CustomerPendingReviewsView(APIView):
    """GET /api/customer/pending-reviews — count and list of unreviewed jobs."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def get(self, request):
        customer = _customer_for(request.user)
        rows = [
            {
                'id': job.pk,
                'specialty': job.specialty,
                'completed_at': (
                    job.completed_at.strftime('%Y-%m-%d %H:%M:%S')
                    if job.completed_at
                    else None
                ),
                'customer_name': customer.name,
                'technician_name': job.technician.name if job.technician else None,
                'technician_id': job.technician_id,
            }
            for job in _unreviewed_completed(customer)
        ]
        return Response({'count': len(rows), 'requests': rows})


class CustomerMyReviewsView(APIView):
    """GET /api/customer/my-reviews — every review this customer wrote."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def get(self, request):
        customer = _customer_for(request.user)
        reviews = (
            Review.objects.filter(customer_id=customer.pk)
            .select_related('technician')
            .order_by('-created_at', '-id')
        )
        return Response(
            {
                'reviews': [
                    _review_json(r, r.technician.name if r.technician else None)
                    for r in reviews
                ]
            }
        )
