"""Technician wallet: balance, unpaid commission and payout requests.

Port of the wallet routes in ``backend/routes/technician.routes.js``. The
commission booked by the cash confirmation (see ``services/payments.py``)
shows up here as debt; once it reaches ``wallet_debt_limit`` the technician
stops receiving new orders until they pay the platform.
"""

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import ApiError
from .models import WalletTransaction, WithdrawalRequest
from .permissions import IsTechnician
from .services.payments import (
    PaymentError,
    can_receive_requests,
    debt_limit,
    ensure_wallet,
    pending_withdrawal,
    request_withdrawal,
    withdrawal_settings,
)
from .views import _body, _json_ready, _technician_for


class TechnicianWalletView(APIView):
    """GET /api/technician/wallet — both pots, totals and recent ledger rows."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def get(self, request):
        technician = _technician_for(request.user)
        wallet = ensure_wallet(technician.pk)
        transactions = [
            _json_ready(row)
            for row in WalletTransaction.objects.filter(technician_id=technician.pk)
            .order_by('-created_at', '-id')
            .values()[:50]
        ]
        settings = withdrawal_settings()
        return Response(
            {
                'wallet': {
                    'technician_id': technician.pk,
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
                },
                'transactions': transactions,
                'debtLimit': debt_limit(),
                'minWithdrawal': settings['min'],
                'withdrawalEtaHours': settings['eta_hours'],
                'canReceiveRequests': can_receive_requests(technician.pk),
                'pendingWithdrawal': pending_withdrawal(technician.pk),
            }
        )


class TechnicianWithdrawalsView(APIView):
    """GET/POST /api/technician/withdrawals — history and new payout request."""

    permission_classes = [IsAuthenticated, IsTechnician]

    def get(self, request):
        technician = _technician_for(request.user)
        rows = [
            _json_ready(row)
            for row in WithdrawalRequest.objects.filter(technician_id=technician.pk)
            .order_by('-requested_at', '-id')
            .values()[:30]
        ]
        return Response(
            {'withdrawals': rows, 'minWithdrawal': withdrawal_settings()['min']}
        )

    def post(self, request):
        technician = _technician_for(request.user)
        try:
            row = request_withdrawal(technician.pk, _body(request).get('amount'))
        except PaymentError as exc:
            raise ApiError(exc.message, exc.status_code)
        hours = withdrawal_settings()['eta_hours']
        return Response(
            {
                'message': f'تم استلام طلب السحب. سيتم التحويل خلال {hours:g} ساعة.',
                'request': row,
            },
            status=status.HTTP_201_CREATED,
        )
