"""Admin money screens: technician wallets, payouts, withdrawals, payments.

Ported from ``backend/routes/admin.routes.js``. All of them need the
``stats`` admin permission, as in the Node backend.
"""

from django.db.models import Exists, OuterRef, Q, Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError, bad_request
from .models import (
    Request,
    Technician,
    TechnicianWallet,
    WalletTransaction,
    WithdrawalRequest,
    utc_now,
)
from .permissions import AdminPermission
from .serializers import TechnicianRequestSerializer
from .services.payments import (
    PaymentError,
    add_wallet_transaction,
    approve_withdrawal,
    debt_limit,
    ensure_wallet,
    mark_withdrawal_paid,
    parse_amount,
    reject_withdrawal,
    resolve_dispute,
    settlement_view,
)
from .views import _body, _json_ready

FINANCE = [IsAuthenticated, AdminPermission('stats')]


def _payment_error(exc):
    return ApiError(exc.message, exc.status_code)


def _note(data, limit=300):
    note = data.get('note')
    return str(note).strip()[:limit] if note not in (None, '') else None


def _withdrawal_json(row):
    data = _json_ready(
        WithdrawalRequest.objects.filter(pk=row.pk).values().first()
    )
    data['technician_id'] = data.pop('technician', data.get('technician_id'))
    data['processed_by'] = data.pop('processed_by_id', data.get('processed_by'))
    return data


class AdminWalletListView(APIView):
    """GET /api/admin/wallets — every technician wallet, biggest debt first."""

    permission_classes = FINANCE

    def get(self, request):
        limit = debt_limit()
        for technician_id in Technician.objects.values_list('pk', flat=True):
            ensure_wallet(technician_id)
        rows = (
            TechnicianWallet.objects.select_related('technician')
            .order_by('-total_debt', '-balance', 'technician__name')
        )
        wallets = []
        for wallet in rows:
            tech = wallet.technician
            wallets.append(
                {
                    'technician_id': tech.pk,
                    'balance': float(wallet.balance or 0),
                    'total_debt': float(wallet.total_debt or 0),
                    'total_earned': float(wallet.total_earned or 0),
                    'total_owed': float(wallet.total_owed or 0),
                    'total_paid_to_platform': float(wallet.total_paid_to_platform or 0),
                    'total_withdrawn': float(wallet.total_withdrawn or 0),
                    'last_updated': (
                        wallet.last_updated.strftime('%Y-%m-%d %H:%M:%S')
                        if wallet.last_updated
                        else None
                    ),
                    'name': tech.name,
                    'phone': tech.phone,
                    'specialty': tech.specialty,
                    'availability': tech.availability,
                    'blocked': 1 if float(wallet.total_debt or 0) >= limit else 0,
                }
            )
        return Response({'wallets': wallets, 'debtLimit': limit})


class AdminWalletTransactionsView(APIView):
    """GET /api/admin/wallets/<technician_id>/transactions — latest 100 rows."""

    permission_classes = FINANCE

    def get(self, request, technician_id):
        rows = (
            WalletTransaction.objects.filter(technician_id=technician_id)
            .order_by('-created_at', '-id')
            .values()[:100]
        )
        return Response({'transactions': [_json_ready(r) for r in rows]})


class AdminWalletPayoutView(APIView):
    """POST /api/admin/wallets/<technician_id>/payout — technician paid the platform."""

    permission_classes = FINANCE

    def post(self, request, technician_id):
        data = _body(request)
        try:
            amount = parse_amount(data.get('amount'))
        except PaymentError as exc:
            raise bad_request(exc.message)
        if not Technician.objects.filter(pk=technician_id).exists():
            raise ApiError('الفني غير موجود', status.HTTP_404_NOT_FOUND)
        before = float(ensure_wallet(technician_id).total_debt or 0)
        if amount > before:
            raise bad_request(f'المبلغ أكبر من المستحق على الفني ({before:g} ج.م)')
        after = add_wallet_transaction(
            technician_id,
            type=WalletTransaction.TYPE_PAYOUT,
            amount=amount,
            note=_note(data) or 'تحويل من الفني للمنصة',
        )
        return Response(
            {
                'message': 'تم تسجيل التحويل',
                'total_debt': after['total_debt'],
                'balance': after['balance'],
                'remaining_debt': round(before - amount, 2),
            }
        )


class AdminWithdrawalListView(APIView):
    """GET /api/admin/withdrawals?status= — payout requests, oldest first."""

    permission_classes = FINANCE

    def get(self, request):
        rows = WithdrawalRequest.objects.select_related('technician', 'processed_by')
        wanted = request.query_params.get('status')
        if wanted:
            if wanted not in ('pending', 'approved', 'paid', 'rejected'):
                raise bad_request('حالة السحب غير صالحة')
            rows = rows.filter(status=wanted)
        withdrawals = []
        for row in rows.order_by('requested_at', 'id')[:100]:
            data = _withdrawal_json(row)
            data['technician_name'] = row.technician.name
            data['technician_phone'] = row.technician.phone
            data['processed_by_email'] = row.processed_by.email if row.processed_by else None
            withdrawals.append(data)
        awaiting = WithdrawalRequest.objects.filter(status__in=('pending', 'approved'))
        return Response(
            {
                'withdrawals': withdrawals,
                'count': len(withdrawals),
                'awaiting_total': float(awaiting.aggregate(t=Sum('amount'))['t'] or 0),
                'awaiting_count': awaiting.count(),
            }
        )


class AdminWithdrawalActionView(APIView):
    """POST /api/admin/withdrawals/<id>/(approve|paid|reject)."""

    permission_classes = FINANCE

    MESSAGES = {
        'approve': 'تم اعتماد الطلب. قم بتحويل المبلغ ثم اضغط "تم التحويل".',
        'paid': 'تم تسجيل الدفع. تم خصم المبلغ من رصيد الفني.',
        'reject': 'تم رفض الطلب',
    }

    def post(self, request, pk, action):
        note = _note(_body(request))
        try:
            if action == 'approve':
                row = approve_withdrawal(pk, request.user.pk)
            elif action == 'paid':
                row = mark_withdrawal_paid(pk, request.user.pk, note)
            else:
                row = reject_withdrawal(pk, request.user.pk, note)
        except PaymentError as exc:
            raise _payment_error(exc)
        return Response({'message': self.MESSAGES[action], 'request': _withdrawal_json(row)})


def _booked_commission():
    return WalletTransaction.objects.filter(
        request_id=OuterRef('pk'), type=WalletTransaction.TYPE_DEBT
    )


class AdminPaymentsPendingView(APIView):
    """GET /api/admin/payments/pending — completed jobs an admin may settle."""

    permission_classes = FINANCE

    def get(self, request):
        rows = (
            Request.objects.filter(status='completed', customer__isnull=False)
            .annotate(booked=Exists(_booked_commission()))
            .filter(
                Q(amount_status__in=('pending', 'disputed'), booked=False)
                | Q(
                    amount_status__isnull=True,
                    payment_status__isnull=True,
                )
                | Q(
                    amount_status__isnull=True,
                    payment_status__in=('unpaid', 'pending_verification'),
                )
            )
            .select_related('customer', 'technician')
        )
        order = {'disputed': 0, 'pending': 1}
        rows = sorted(
            rows,
            key=lambda r: (
                order.get(r.amount_status, 2),
                -(r.completed_at.timestamp() if r.completed_at else 0),
            ),
        )[:100]
        payments = TechnicianRequestSerializer(rows, many=True).data
        return Response({'payments': payments, 'count': len(payments)})


class AdminPaymentsDisputedView(APIView):
    """GET /api/admin/payments/disputed — the two amounts disagree."""

    permission_classes = FINANCE

    def get(self, request):
        rows = (
            Request.objects.filter(amount_status='disputed')
            .select_related('customer', 'technician')
            .order_by('-technician_reported_at')
        )
        disputes = TechnicianRequestSerializer(rows, many=True).data
        return Response({'disputes': disputes, 'count': len(disputes)})


class AdminPaymentResolveView(APIView):
    """POST /api/admin/payments/<id>/resolve-dispute — {final_amount, note}."""

    permission_classes = FINANCE

    def post(self, request, pk):
        data = _body(request)
        was = Request.objects.filter(pk=pk).values_list('amount_status', flat=True).first()
        try:
            result = resolve_dispute(pk, data.get('final_amount'), request.user.pk, _note(data, 1000))
        except PaymentError as exc:
            raise _payment_error(exc)
        view = settlement_view(pk)
        job = Request.objects.get(pk=pk)
        if result.get('status') == 'already_finalized':
            return Response(
                {
                    'message': 'المبلغ محسوب بالفعل لهذا الطلب — لم يتم تعديل أي رصيد',
                    'already_finalized': True,
                    'status': view['status'],
                    'final_amount': view['final_amount'],
                    'commission': view['commission'],
                }
            )
        return Response(
            {
                'message': (
                    'تم حل النزاع وتسجيل المبلغ' if was == 'disputed' else 'تم تأكيد المبلغ وتسجيله'
                ),
                'status': view['status'],
                'final_amount': view['final_amount'],
                'commission': view['commission'],
                'dispute_note': job.dispute_note,
                'dispute_resolved_at': (
                    job.dispute_resolved_at.strftime('%Y-%m-%d %H:%M:%S')
                    if job.dispute_resolved_at
                    else None
                ),
            }
        )


class AdminPaymentConfirmView(APIView):
    """POST /api/admin/payments/<id>/confirm — settle a legacy cash job."""

    permission_classes = FINANCE

    def post(self, request, pk):
        job = Request.objects.filter(pk=pk).first()
        if job is None:
            raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
        if job.status != 'completed':
            raise bad_request('الطلب ليس مكتملاً')
        if not job.technician_id:
            raise bad_request('لا يوجد فني مرتبط')
        if job.amount_status == 'disputed':
            raise bad_request('الطلب محل نزاع — يُحل من صفحة النزاعات')
        if job.amount_status == 'pending':
            raise bad_request('الطلب في انتظار تأكيد الطرف الآخر')
        if job.payment_status == 'confirmed':
            return Response({'message': 'تم تأكيد الدفع مسبقاً', 'already_confirmed': True})
        if job.payment_status == 'refunded':
            raise bad_request('تم استرداد هذا الطلب — لا يمكن تأكيد دفعه')
        if _body(request).get('method') == 'instapay':
            raise bad_request('إنستاباي غير متاح حالياً — الدفع كاش فقط')

        updated = Request.objects.filter(pk=pk).exclude(payment_status='confirmed').update(
            payment_status='confirmed',
            payment_method='cash',
            payment_reference=_body(request).get('reference') or None,
            payment_confirmed_at=utc_now(),
            payment_confirmed_by_id=request.user.pk,
            updated_at=utc_now(),
        )
        already_booked = WalletTransaction.objects.filter(
            request_id=pk, type=WalletTransaction.TYPE_DEBT
        ).exists()
        if updated and not already_booked and job.commission_amount:
            add_wallet_transaction(
                job.technician_id,
                type=WalletTransaction.TYPE_DEBT,
                amount=job.commission_amount,
                request_id=pk,
                note=f'عمولة المنصة على طلب #{pk} (كاش)',
            )
        return Response({'message': 'تم تأكيد الدفع'})


class AdminPaymentRefundView(APIView):
    """POST /api/admin/payments/<id>/refund — mark the payment refunded."""

    permission_classes = FINANCE

    def post(self, request, pk):
        if not Request.objects.filter(pk=pk).exists():
            raise ApiError('الطلب غير موجود', status.HTTP_404_NOT_FOUND)
        Request.objects.filter(pk=pk).update(payment_status='refunded', updated_at=utc_now())
        return Response({'message': 'تم تسجيل الاسترداد'})
