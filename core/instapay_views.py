"""InstaPay payment endpoints.

Customer:
* ``GET  /api/payments/instapay`` — where to send the money (or disabled);
* ``POST /api/customer/requests/<id>/instapay-receipt`` — multipart
  ``receipt`` (image) + ``amount``; checked at once (see services.instapay);
* ``GET  /api/customer/requests/<id>/instapay-receipts`` — this order's receipts.

Admin (``stats`` permission, like the other money screens):
* ``GET  /api/admin/instapay/receipts?status=review|verified|rejected``;
* ``GET  /api/admin/instapay/receipts/<id>/image``;
* ``POST /api/admin/instapay/receipts/<id>/(approve|reject)`` — {amount?, note?}.
"""

import json

from django.http import FileResponse, Http404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError, bad_request
from .models import PaymentReceipt, Request
from .permissions import AdminPermission, IsCustomer
from .ratelimit import throttle
from .services.instapay import (
    _receipt_json,
    approve_receipt,
    instapay_config,
    reader_available,
    reject_receipt,
    submit_receipt,
)
from .services.payments import PaymentError
from .upload_validators import (
    ALLOWED_ORDER_IMAGE_MIME_TYPES,
    open_stored_upload,
    resolve_stored_upload,
)
from .views import _body, _customer_for

FINANCE = [IsAuthenticated, AdminPermission('stats')]


def _payment_error(exc):
    return ApiError(exc.message, exc.status_code)


def _public_config():
    config = instapay_config()
    if not config['enabled']:
        return {'enabled': False}
    return {
        'enabled': True,
        'number': config['number'],
        'name': config['name'],
        'address': config['address'],
        'link': config['link'],
    }


class InstapayConfigView(APIView):
    """GET /api/payments/instapay — the account customers transfer to."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(_public_config())


class CustomerInstapayReceiptView(APIView):
    """POST /api/customer/requests/<id>/instapay-receipt."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def post(self, request, pk):
        customer = _customer_for(request.user)
        # Each upload costs a model call; this also slows down guessing.
        throttle(
            request, 'instapay-receipt', limit=12, window_seconds=3600,
            identifier=f'user:{request.user.pk}',
        )
        upload = request.FILES.get('receipt')
        if upload is None:
            raise bad_request('ارفع صورة وصل التحويل')
        try:
            receipt, settlement = submit_receipt(pk, customer, upload, request.data.get('amount'))
        except PaymentError as exc:
            raise _payment_error(exc)
        body = {'receipt': _receipt_json(receipt), 'message': receipt.reason}
        if settlement:
            body['settlement'] = settlement
        return Response(body)


class CustomerInstapayReceiptListView(APIView):
    """GET /api/customer/requests/<id>/instapay-receipts."""

    permission_classes = [IsAuthenticated, IsCustomer]

    def get(self, request, pk):
        customer = _customer_for(request.user)
        if not Request.objects.filter(pk=pk, customer_id=customer.pk).exists():
            raise ApiError('الطلب غير موجود', 404)
        rows = PaymentReceipt.objects.filter(request_id=pk).order_by('-created_at', '-id')
        return Response({'receipts': [_receipt_json(r) for r in rows]})


def _admin_receipt_json(receipt):
    row = _receipt_json(receipt)
    job = receipt.request
    row.update(
        {
            'recipient': receipt.recipient,
            'sender': receipt.sender,
            'transferred_at': (
                receipt.transferred_at.strftime('%Y-%m-%d %H:%M:%S')
                if receipt.transferred_at
                else None
            ),
            'checks': json.loads(receipt.checks or '[]'),
            'has_image': bool(receipt.image_path),
            'image_url': (
                f'/api/admin/instapay/receipts/{receipt.pk}/image' if receipt.image_path else None
            ),
            'review_note': receipt.review_note,
            'reviewed_at': (
                receipt.reviewed_at.strftime('%Y-%m-%d %H:%M:%S') if receipt.reviewed_at else None
            ),
            'automatic': receipt.status == PaymentReceipt.STATUS_VERIFIED
            and receipt.reviewed_by_id is None,
            'customer_name': receipt.customer.name if receipt.customer else None,
            'technician_name': job.technician.name if job.technician else None,
            'service_price': job.service_price,
            'request_status': job.status,
            'payment_status': job.payment_status,
        }
    )
    return row


class AdminInstapayReceiptListView(APIView):
    """GET /api/admin/instapay/receipts — newest first, review queue by default."""

    permission_classes = FINANCE

    def get(self, request):
        state = request.query_params.get('status') or PaymentReceipt.STATUS_REVIEW
        if state not in (
            PaymentReceipt.STATUS_REVIEW,
            PaymentReceipt.STATUS_VERIFIED,
            PaymentReceipt.STATUS_REJECTED,
        ):
            raise bad_request('حالة غير صالحة')
        rows = (
            PaymentReceipt.objects.filter(status=state)
            .select_related('customer', 'request__technician')
            .order_by('-created_at', '-id')[:200]
        )
        return Response(
            {
                'receipts': [_admin_receipt_json(r) for r in rows],
                'review_count': PaymentReceipt.objects.filter(
                    status=PaymentReceipt.STATUS_REVIEW
                ).count(),
                'auto_reader': reader_available(),
                'config': _public_config(),
            }
        )


class AdminInstapayReceiptImageView(APIView):
    """GET /api/admin/instapay/receipts/<id>/image — the uploaded receipt."""

    permission_classes = FINANCE

    def get(self, request, pk):
        receipt = PaymentReceipt.objects.filter(pk=pk).first()
        if receipt is None or not receipt.image_path:
            raise Http404('لا توجد صورة')
        name = resolve_stored_upload(receipt.image_path)
        opened = open_stored_upload(name) if name else None
        if opened is None:
            raise Http404('لا توجد صورة')
        handle, mime = opened
        if mime not in ALLOWED_ORDER_IMAGE_MIME_TYPES:
            handle.close()
            raise Http404('لا توجد صورة')
        response = FileResponse(handle, content_type=mime)
        response['Content-Disposition'] = 'inline'
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class AdminInstapayReceiptActionView(APIView):
    """POST /api/admin/instapay/receipts/<id>/(approve|reject)."""

    permission_classes = FINANCE

    def post(self, request, pk, action):
        data = _body(request)
        note = str(data.get('note') or '').strip()[:500] or None
        try:
            if action == 'approve':
                receipt, settlement = approve_receipt(pk, request.user.pk, data.get('amount'), note)
                message = f"تم تأكيد الدفع ({settlement['final_amount']:g} ج.م) وإنهاء المهمة"
            else:
                receipt = reject_receipt(pk, request.user.pk, note)
                message = 'تم رفض الوصل وإبلاغ العميل'
        except PaymentError as exc:
            raise _payment_error(exc)
        receipt = PaymentReceipt.objects.select_related('customer', 'request__technician').get(
            pk=receipt.pk
        )
        return Response({'message': message, 'receipt': _admin_receipt_json(receipt)})
