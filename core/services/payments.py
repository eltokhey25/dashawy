"""Two-sided cash confirmation and the technician wallet ledger.

Python port of ``backend/services/payments.js`` and the wallet half of
``backend/services/pricing.js``. The customer pays the technician in cash, so
the platform never sees the money; both sides report the amount:

* match    -> the platform commission is booked as wallet debt and the
              request is settled (``amount_status = 'matched'``);
* mismatch -> frozen as ``'disputed'`` for an admin to resolve;
* one side only -> ``'pending'`` while waiting for the other.
"""

from django.db import transaction

from ..models import Request, TechnicianWallet, WalletTransaction, utc_now
from .pricing import commission_rate

# Anything larger than a rounding artefact is a real difference in cash.
MATCH_TOLERANCE = 0.01
SETTLED = ('matched', 'resolved')
MAX_AMOUNT = 100000


class PaymentError(Exception):
    """A report that cannot be accepted; carries an HTTP status."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _round2(value):
    return round(float(value) + 0.0, 2)


def parse_amount(raw):
    """A positive amount up to ``MAX_AMOUNT``, or :class:`PaymentError`."""
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        raise PaymentError('المبلغ غير صالح')
    if not (0 < amount <= MAX_AMOUNT):
        raise PaymentError('المبلغ غير صالح')
    return _round2(amount)


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------


def ensure_wallet(technician_id):
    wallet, _ = TechnicianWallet.objects.get_or_create(
        technician_id=technician_id,
        defaults={
            'balance': 0,
            'total_debt': 0,
            'total_earned': 0,
            'total_owed': 0,
            'total_paid_to_platform': 0,
            'total_withdrawn': 0,
        },
    )
    return wallet


def add_wallet_transaction(technician_id, *, type, amount, request_id=None, note=None):
    """Append one ledger entry and move the matching wallet pot.

    ``amount`` is a magnitude; ``type`` says which pot moves (a ``debt`` raises
    what the technician owes the platform, a ``payout`` pays it down).
    """
    magnitude = abs(float(amount or 0))
    with transaction.atomic():
        wallet = ensure_wallet(technician_id)
        wallet = TechnicianWallet.objects.select_for_update().get(pk=wallet.pk)
        balance = _round2(wallet.balance or 0)
        debt = _round2(wallet.total_debt or 0)
        earned = _round2(wallet.total_earned or 0)
        owed = _round2(wallet.total_owed or 0)
        paid = _round2(wallet.total_paid_to_platform or 0)
        withdrawn = _round2(wallet.total_withdrawn or 0)

        if type == WalletTransaction.TYPE_PAYMENT:
            balance, earned = _round2(balance + magnitude), _round2(earned + magnitude)
        elif type == WalletTransaction.TYPE_DEBT:
            debt, owed = _round2(debt + magnitude), _round2(owed + magnitude)
        elif type == WalletTransaction.TYPE_PAYOUT:
            debt, paid = max(0.0, _round2(debt - magnitude)), _round2(paid + magnitude)
        elif type == WalletTransaction.TYPE_WITHDRAWAL:
            balance = max(0.0, _round2(balance - magnitude))
            withdrawn = _round2(withdrawn + magnitude)

        WalletTransaction.objects.create(
            technician_id=technician_id,
            request_id=request_id,
            type=type,
            amount=magnitude,
            balance_after=balance,
            debt_after=debt,
            note=note,
        )
        TechnicianWallet.objects.filter(pk=wallet.pk).update(
            balance=balance,
            total_debt=debt,
            total_earned=earned,
            total_owed=owed,
            total_paid_to_platform=paid,
            total_withdrawn=withdrawn,
            last_updated=utc_now(),
        )
    return {'balance': balance, 'total_debt': debt}


# ---------------------------------------------------------------------------
# Cash confirmation
# ---------------------------------------------------------------------------


def _commission_percent(request):
    # The rate frozen on the request when it was created; legacy rows stored 0.
    if request.commission_percent:
        return float(request.commission_percent)
    return commission_rate()


def _settle(request_id, final_amount, status):
    """Book the commission once and mark the request settled."""
    with transaction.atomic():
        request = Request.objects.select_for_update().get(pk=request_id)
        if request.technician_id and WalletTransaction.objects.filter(
            request_id=request_id,
            technician_id=request.technician_id,
            type=WalletTransaction.TYPE_DEBT,
        ).exists():
            return {'status': 'already_finalized', 'final_amount': request.final_amount}

        amount = _round2(final_amount)
        commission = _round2(amount * _commission_percent(request) / 100)
        now = utc_now()
        Request.objects.filter(pk=request_id).update(
            final_amount=amount,
            amount_status=status,
            payment_status='confirmed',
            payment_method='cash',
            payment_reference=request.payment_reference or f'cash-{amount}',
            payment_confirmed_at=now,
            commission_amount=commission,
            technician_earning=_round2(amount - commission),
            updated_at=now,
        )
        if request.technician_id:
            add_wallet_transaction(
                request.technician_id,
                type=WalletTransaction.TYPE_DEBT,
                amount=commission,
                request_id=request_id,
                note=f'عمولة المنصة على طلب #{request_id} (كاش {amount} ج.م)',
            )
    return {'status': status, 'final_amount': amount, 'commission': commission}


def try_match(request_id):
    """Compare whatever has been reported so far."""
    request = Request.objects.get(pk=request_id)
    customer = request.customer_reported_amount
    technician = request.technician_reported_amount
    if customer is not None and technician is not None:
        if abs(float(customer) - float(technician)) < MATCH_TOLERANCE:
            result = _settle(request_id, customer, 'matched')
            result['message'] = 'تم تأكيد المبلغ من الطرفين'
            return result
        Request.objects.filter(pk=request_id).update(
            amount_status='disputed', updated_at=utc_now()
        )
        return {
            'status': 'disputed',
            'message': 'يوجد اختلاف في المبلغ — تم إحالة الطلب للمراجعة',
            'customer_amount': customer,
            'technician_amount': technician,
        }
    return {
        'status': 'pending',
        'message': 'في انتظار تأكيد الطرف الآخر',
        'waiting_on': 'customer' if technician is not None else 'technician',
    }


def _report(request, field, amount):
    if request.status != 'completed':
        raise PaymentError('الطلب لم يُكتمل بعد')
    if request.amount_status in SETTLED or getattr(request, field) is not None:
        raise PaymentError('تم تسجيل المبلغ مسبقاً', 409)
    now = utc_now()
    updates = {field: amount, 'amount_status': 'pending', 'updated_at': now}
    if field == 'technician_reported_amount':
        updates['technician_reported_at'] = now
        updates['payment_method'] = request.payment_method or 'cash'
    else:
        updates['customer_reported_at'] = now
    Request.objects.filter(pk=request.pk).update(**updates)
    return try_match(request.pk)


def technician_report(request_id, technician_id, raw_amount):
    request = Request.objects.filter(pk=request_id, technician_id=technician_id).first()
    if request is None:
        raise PaymentError('الطلب غير موجود', 404)
    return _report(request, 'technician_reported_amount', parse_amount(raw_amount))


def customer_report(request_id, customer_id, raw_amount):
    request = Request.objects.filter(pk=request_id, customer_id=customer_id).first()
    if request is None:
        raise PaymentError('الطلب غير موجود', 404)
    if request.payment_method and request.payment_method != 'cash':
        raise PaymentError('هذا الطلب لم يكن دفعاً نقدياً')
    return _report(request, 'customer_reported_amount', parse_amount(raw_amount))


def settlement_view(request_id):
    """The figures both dashboards show after a report."""
    request = Request.objects.get(pk=request_id)
    final = request.final_amount
    return {
        'status': request.amount_status,
        'final_amount': final,
        'commission': (
            _round2(final * _commission_percent(request) / 100) if final is not None else None
        ),
        'customer_amount': request.customer_reported_amount,
        'technician_amount': request.technician_reported_amount,
    }
