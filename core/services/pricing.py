"""Service prices, commission and technician earnings.

The admin-editable ``service_pricing`` table is the single source of truth. A
price of zero (or a missing/disabled row) means "not priced yet", which the UI
renders as ``السعر بعد المعاينة`` rather than inventing a number.
"""

from __future__ import annotations

from ..models import ServicePricing
from ..site_config import get_site_config


def price_for(service_key: str):
    """Return the active positive price for a service, or ``None``."""
    row = (
        ServicePricing.objects.filter(service_key=service_key)
        .values('price', 'is_active')
        .first()
    )
    if not row or row['is_active'] is False:
        return None
    try:
        value = float(row['price'] or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def commission_rate() -> float:
    try:
        value = float(get_site_config()['commission_rate'])
    except (TypeError, ValueError, KeyError):
        value = 20.0
    return min(max(value, 0.0), 100.0)


def quote_for(service_key: str) -> dict:
    """Price/commission fields for a new request.

    Nothing is charged at creation time; these fields are the frozen quote that
    later payment and wallet code reads.
    """
    price = price_for(service_key)
    rate = commission_rate()
    if price is None:
        return {
            'service_price': 0.0,
            'commission_percent': rate,
            'commission_amount': 0.0,
            'technician_earning': 0.0,
        }
    commission = round(price * rate / 100.0, 2)
    return {
        'service_price': price,
        'commission_percent': rate,
        'commission_amount': commission,
        'technician_earning': round(price - commission, 2),
    }


def price_status(request) -> dict:
    """How the customer-facing pages should describe the price.

    * ``confirmed``  - the tariff is known and the technician has not changed it
    * ``estimated``  - a tariff exists but the final amount is agreed on site
    * ``inspection`` - no tariff configured; the technician quotes after seeing
      the job
    """
    try:
        amount = float(request.service_price or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount <= 0:
        return {
            'status': 'inspection',
            'label': 'السعر بعد المعاينة',
            'amount': None,
        }
    if request.final_amount is not None:
        return {
            'status': 'confirmed',
            'label': 'السعر النهائي',
            'amount': float(request.final_amount),
        }
    return {
        'status': 'estimated',
        'label': 'سعر تقديري — بيتأكد معاك قبل بدء الشغل',
        'amount': amount,
    }
