"""Tell a customer that a technician accepted their request.

The in-site notice comes from the request itself: My requests polls every few
seconds and announces the change to ``assigned`` with the technician's name.
This module adds the message that reaches the customer when the site is closed:
a Telegram message for accounts that linked the bot. It never raises, so a
failed send cannot turn a successful accept into an error.
"""

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

TELEGRAM_API = 'https://api.telegram.org/bot{token}/sendMessage'


def accepted_message(technician_name):
    name = technician_name or 'الفني'
    return (
        '✅ تم قبول طلبك في دشاوي\n'
        f'الفني: {name}\n'
        'هيتواصل معاك قريب. تابع طلبك من «طلباتي».'
    )


def send_telegram(chat_id, text):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token or not chat_id:
        return False
    body = json.dumps({'chat_id': chat_id, 'text': text}).encode('utf-8')
    req = urllib.request.Request(
        TELEGRAM_API.format(token=token),
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001 - a notification must never break the caller
        logger.warning('Telegram notification failed', exc_info=True)
        return False


def notify_request_accepted(request):
    """Best-effort message to the request's customer after an accept."""
    customer = getattr(request, 'customer', None) if request.customer_id else None
    user = getattr(customer, 'user', None) if customer else None
    technician = getattr(request, 'technician', None)
    if user is None or not user.telegram_chat_id:
        return False
    return send_telegram(
        user.telegram_chat_id,
        accepted_message(technician.name if technician else None),
    )
