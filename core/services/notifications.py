"""Notifications that reach people when the site is closed.

Web Push (``services/push.py``) to every browser the user subscribed, plus a
Telegram message for accounts that linked the bot. The in-site notices come
from the pages themselves (My requests and the technician dashboard poll).
Nothing here raises, so a failed send never turns the action into an error.
"""

import logging

logger = logging.getLogger(__name__)

def accepted_message(technician_name):
    name = technician_name or 'الفني'
    return (
        '✅ تم قبول طلبك في دشاوي\n'
        f'الفني: {name}\n'
        'هيتواصل معاك قريب. تابع طلبك من «طلباتي».'
    )


def send_telegram(chat_id, text):
    """Kept as the seam the tests patch; delivery lives in services.telegram."""
    from .telegram import send_message

    return send_message(chat_id, text)


def _push(user_id, payload):
    """Deliver one notification to every channel the user has.

    Browser push for subscribed devices, and the same text on Telegram when
    the account is linked. ``url`` becomes a link only for site pages.
    """
    from ..models import User
    from . import push

    result = push.notify(user_id, payload)
    chat_id = (
        User.objects.filter(pk=user_id).values_list('telegram_chat_id', flat=True).first()
    )
    if chat_id:
        text = f"{payload.get('title', '')}\n{payload.get('body', '')}".strip()
        url = payload.get('url')
        if url and url.startswith('/'):
            from ..site_config import get_site_config

            text += f"\n{get_site_config()['site_url']}{url}"
        send_telegram(chat_id, text)
    return result


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
    return True


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


def _money(value):
    return f'{value:g}'


def notify_debt_change(technician_id, before, after):
    """Warn a technician as their unpaid commission nears the debt limit.

    Fires once when the debt crosses the warning line, once when it reaches
    the limit (new orders stop), and once when a payment brings it back
    under the limit. Push plus Telegram for linked accounts.
    """
    from ..models import Technician
    from .payments import debt_limit, debt_warning_threshold

    limit = debt_limit()
    warning = debt_warning_threshold()
    if before < limit <= after:
        title = '⛔ حسابك اتوقف عن استقبال الطلبات'
        body = (
            f'المستحق عليك للمنصة {_money(after)} ج.م ووصل للحد ({_money(limit)} ج.م). '
            'سدد المستحقات عشان الطلبات ترجع توصلك.'
        )
    elif before < warning <= after < limit:
        title = '⚠️ قربت توصل لحد المديونية'
        body = (
            f'المستحق عليك للمنصة {_money(after)} ج.م. لو وصل {_money(limit)} ج.م '
            f'حسابك هيتوقف عن استقبال الطلبات — باقي {_money(round(limit - after, 2))} ج.م. '
            'سدد دلوقتي عشان شغلك ميقفش.'
        )
    elif after < limit <= before:
        title = '✅ حسابك رجع يستقبل طلبات'
        body = f'تم تسجيل السداد. المستحق عليك دلوقتي {_money(after)} ج.م.'
    else:
        return False

    tech = Technician.objects.filter(pk=technician_id).select_related('user').first()
    if tech is None or tech.user is None:
        return False
    _push(
        tech.user_id,
        {
            'title': title,
            'body': body,
            'url': '/technician/dashboard.html?tab=wallet',
            'tag': 'wallet-debt',
            'requireInteraction': True,
        },
    )
    return True


def notify_request_cancelled(technician_user_id, request_id):
    """Tell the assigned technician the customer cancelled the job."""
    _push(
        technician_user_id,
        {
            'title': '❌ العميل لغى الطلب',
            'body': f'طلب #{request_id} اتلغى وإنت متاح دلوقتي لطلبات تانية.',
            'url': '/technician/dashboard.html',
            'tag': f'request-cancelled-{request_id}',
        },
    )


def _cairo_time():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo('Africa/Cairo')).strftime('%Y-%m-%d %H:%M')


def notify_login(user, ip=None):
    """Tell the account owner someone signed in (password, Google or reset)."""
    _push(
        user.pk,
        {
            'title': '🔔 تم تسجيل الدخول لحسابك',
            'body': f'الوقت: {_cairo_time()}' + (f' — من IP {ip}' if ip else '')
            + '\nلو مش إنت، غيّر كلمة السر فوراً.',
            'url': '/change-password',
            'tag': 'login',
        },
    )


def notify_password_changed(user, ip=None):
    _push(
        user.pk,
        {
            'title': '🔑 تم تغيير كلمة السر',
            'body': f'اتغيرت كلمة سر حسابك في {_cairo_time()}.' + '\nلو مش إنت، كلّمنا فوراً.',
            'url': '/login.html',
            'tag': 'password-changed',
        },
    )


def notify_account_created(user):
    _push(
        user.pk,
        {
            'title': '🎉 أهلاً بيك في دشاوي',
            'body': 'تم إنشاء حسابك بنجاح. هتوصلك هنا إشعارات طلباتك.',
            'url': '/customer/my-requests.html',
            'tag': 'welcome',
        },
    )
