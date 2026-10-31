"""Notifications that reach people when the site is closed.

Web Push (``services/push.py``) to every browser the user subscribed, plus a
Telegram message for accounts that linked the bot. The in-site notices come
from the pages themselves (My requests and the technician dashboard poll).
Nothing here raises, so a failed send never turns the action into an error.
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


def _push(user_id, payload):
    from . import push

    return push.notify(user_id, payload)


def notify_request_accepted(request):
    """Best-effort message to the request's customer after an accept."""
    customer = getattr(request, 'customer', None) if request.customer_id else None
    user = getattr(customer, 'user', None) if customer else None
    technician = getattr(request, 'technician', None)
    if user is None:
        return False
    name = technician.name if technician else None
    _push(
        user.pk,
        {
            'title': '✅ تم قبول طلبك',
            'body': f'الفني: {name or "الفني"} — هيتواصل معاك قريب',
            'url': '/customer/my-requests.html',
            'tag': f'request-accepted-{request.pk}',
        },
    )
    if not user.telegram_chat_id:
        return False
    return send_telegram(user.telegram_chat_id, accepted_message(name))


def notify_new_offer(technician_user_ids, request):
    """Tell technicians a new order is waiting in their incoming tab."""
    from ..guest_views import _label_for

    label = (
        request.custom_specialty_text
        if request.specialty == 'other' and request.custom_specialty_text
        else _label_for(request.specialty)
    )
    area = (request.location_text or '').split('—')[0].strip()
    payload = {
        'title': '🔔 طلب جديد',
        'body': label + (f' — {area}' if area else ''),
        'url': '/technician/dashboard.html',
        'tag': 'new-request',
        'requireInteraction': True,
    }
    for user_id in technician_user_ids:
        _push(user_id, payload)


def notify_job_completed(request):
    """Ask the customer to confirm the amount and rate the technician."""
    customer = getattr(request, 'customer', None) if request.customer_id else None
    if customer is None:
        return
    technician = getattr(request, 'technician', None)
    _push(
        customer.user_id,
        {
            'title': '🎉 الفني خلّص الشغل',
            'body': f'{technician.name if technician else "الفني"} أنهى المهمة. أكّد المبلغ وقيّم الخدمة.',
            'url': f'/customer/my-requests.html?review={request.pk}',
            'tag': f'request-completed-{request.pk}',
            'requireInteraction': True,
        },
    )


def notify_chat_message(recipient_user_id, sender_name, body, request_id, recipient_role):
    _push(
        recipient_user_id,
        {
            'title': '💬 رسالة جديدة',
            'body': f'{sender_name}: {body[:50]}',
            'url': f'/chat.html?id={request_id}',
            'tag': f'chat-{request_id}',
        },
    )
