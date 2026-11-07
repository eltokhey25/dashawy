"""Telegram transport: sending, account linking and the webhook entry point.

The Node backend long-polled ``getUpdates``, which needs a process that never
stops; on Vercel there is none, so Telegram POSTs each update to the webhook
(``core.telegram_views.telegram_webhook``). ``setup_webhook`` registers that
URL with Telegram once, with a secret Telegram echoes in a header on every
call, and publishes the bot's command menu.

Linking: the site creates a short-lived token and a ``t.me/<bot>?start=<token>``
link; when the person opens it, Telegram sends ``/start <token>`` to the
webhook and the chat is attached to the account. Every other command and
button press is handled by ``services.telegram_bot``. Nothing here raises on
a failed send: a notification must never break the action that triggered it.
"""

import hashlib
import json
import logging
import os
import secrets
import urllib.request
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction

from ..models import TelegramLinkToken, User, utc_now

logger = logging.getLogger(__name__)

API = 'https://api.telegram.org/bot{token}/{method}'
LINK_TTL_MINUTES = 15
USERNAME_CACHE_KEY = 'dashawy:telegram:bot-username'
# Telegram re-sends an update until the webhook answers 200, and a slow cold
# start can make it retry one that was already handled: remember each
# update_id for a day. The same notification text is never sent twice to a
# chat within this window either (see ``already_notified``).
UPDATE_DEDUP_SECONDS = 24 * 3600
NOTIFY_DEDUP_SECONDS = 12 * 3600

# The command menu Telegram shows under the "/" button, in both roles' words.
BOT_COMMANDS = (
    ('start', 'القائمة الرئيسية'),
    ('menu', 'القائمة الرئيسية'),
    ('requests', 'طلباتي وحالتها'),
    ('new', 'طلب فني جديد'),
    ('incoming', 'الطلبات الواردة (للفني)'),
    ('jobs', 'شغلي الحالي (للفني)'),
    ('wallet', 'رصيد المحفظة (للفني)'),
    ('support', 'التواصل مع الدعم'),
    ('help', 'شرح الأوامر'),
)

LINK_WELCOME = (
    'أهلاً بيك في بوت دشاوي 👋\n'
    'عشان تربط حسابك، افتح صفحة «ربط تليجرام» من الموقع واضغط على رابط الربط.'
)
LINK_INVALID = 'رابط الربط غير صالح أو انتهت صلاحيته. اطلب رابطاً جديداً من الموقع.'
LINK_SUCCESS = (
    'تم ربط حسابك بدشاوي بنجاح ✅\n'
    'هتوصلك هنا إشعارات طلباتك، وكود استعادة كلمة السر لو نسيتها.'
)


class TelegramError(Exception):
    def __init__(self, message, status=None, description=''):
        super().__init__(message)
        self.status = status
        self.description = description


def bot_token():
    return os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()


def is_configured():
    return bool(bot_token())


def webhook_secret():
    """Derived from the bot token, so no extra secret has to be configured."""
    token = bot_token()
    if not token:
        return ''
    return hashlib.sha256(f'dashawy-webhook:{token}'.encode('utf-8')).hexdigest()[:40]


def api_call(method, payload=None, timeout=10):
    token = bot_token()
    if not token:
        raise TelegramError('TELEGRAM_BOT_TOKEN is not set')
    body = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(
        API.format(token=token, method=method),
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            data = json.load(exc)
        except Exception:  # noqa: BLE001
            data = {}
        raise TelegramError(
            data.get('description') or f'Telegram API HTTP {exc.code}',
            status=exc.code,
            description=data.get('description', ''),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - network errors
        raise TelegramError(str(exc)) from exc
    if not data.get('ok'):
        raise TelegramError(data.get('description') or 'Telegram API error')
    return data.get('result')


SENT, UNREACHABLE, FAILED = 'sent', 'unreachable', 'failed'


def deliver_message(chat_id, text, reply_markup=None):
    """Send without touching the database (safe from a worker thread).

    ``reply_markup`` is an inline keyboard (see ``inline_keyboard``). Returns
    ``SENT``, ``UNREACHABLE`` (blocked bot or deleted chat: the caller should
    unlink it) or ``FAILED``.
    """
    if not chat_id or not is_configured():
        return FAILED
    payload = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        api_call('sendMessage', payload)
        return SENT
    except TelegramError as exc:
        logger.warning('Telegram send failed: %s', exc)
        return UNREACHABLE if _chat_unreachable(exc) else FAILED


def inline_keyboard(rows):
    """``[[(label, data), ...], ...]`` -> Telegram ``InlineKeyboardMarkup``.

    ``data`` starting with ``http`` opens that link; anything else comes back
    as ``callback_data`` (Telegram allows 64 bytes).
    """
    keyboard = []
    for row in rows:
        buttons = []
        for label, data in row:
            if str(data).startswith(('http://', 'https://')):
                buttons.append({'text': label, 'url': data})
            else:
                buttons.append({'text': label, 'callback_data': str(data)[:64]})
        if buttons:
            keyboard.append(buttons)
    return {'inline_keyboard': keyboard}


def edit_message(chat_id, message_id, text, reply_markup=None):
    """Replace a bot message in place (menus); False when Telegram refuses."""
    if not chat_id or not message_id or not is_configured():
        return False
    payload = {
        'chat_id': chat_id,
        'message_id': message_id,
        'text': text,
        'disable_web_page_preview': True,
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        api_call('editMessageText', payload)
        return True
    except TelegramError as exc:
        logger.info('Telegram edit failed: %s', exc)
        return False


def answer_callback(callback_id, text=None, alert=False):
    """Stop the button's spinner, optionally with a toast or an alert box."""
    if not callback_id or not is_configured():
        return False
    payload = {'callback_query_id': callback_id}
    if text:
        payload['text'] = text[:200]
        payload['show_alert'] = bool(alert)
    try:
        api_call('answerCallbackQuery', payload)
        return True
    except TelegramError as exc:
        logger.info('Telegram answerCallbackQuery failed: %s', exc)
        return False


def already_notified(chat_id, tag, text):
    """True when this exact notification reached the chat recently.

    Called on the request thread (the cache may be the database). The first
    call for a (chat, tag, text) claims it; later ones within
    ``NOTIFY_DEDUP_SECONDS`` are duplicates.
    """
    digest = hashlib.sha256(f'{chat_id}|{tag or ""}|{text}'.encode('utf-8')).hexdigest()[:32]
    return not cache.add(f'dashawy:telegram:notified:{digest}', 1, NOTIFY_DEDUP_SECONDS)


def unlink_chats(chat_ids):
    """The person blocked the bot or deleted the chat: stop relying on it."""
    chat_ids = [str(c) for c in chat_ids if c]
    if chat_ids:
        User.objects.filter(telegram_chat_id__in=chat_ids).update(telegram_chat_id=None)


def send_message(chat_id, text, reply_markup=None):
    """Plain-text message to a chat. Returns True on success, False otherwise."""
    outcome = deliver_message(chat_id, text, reply_markup)
    if outcome == UNREACHABLE:
        unlink_chats([chat_id])
    return outcome == SENT


def send_document(chat_id, filename, data, caption=''):
    """Upload a file to a chat (multipart ``sendDocument``). Returns a bool."""
    token = bot_token()
    if not chat_id or not token:
        return False
    boundary = '----dashawy' + secrets.token_hex(12)
    parts = []
    for name, value in (('chat_id', str(chat_id)), ('caption', caption or '')):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode('utf-8')
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
            f'filename="{filename}"\r\nContent-Type: application/gzip\r\n\r\n'
        ).encode('utf-8')
        + data
        + b'\r\n'
    )
    parts.append(f'--{boundary}--\r\n'.encode('utf-8'))
    body = b''.join(parts)
    req = urllib.request.Request(
        API.format(token=token, method='sendDocument'),
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return bool(json.load(response).get('ok'))
    except Exception as exc:  # noqa: BLE001
        logger.warning('Telegram sendDocument failed: %s', exc)
        return False


def _chat_unreachable(exc):
    text = (exc.description or str(exc)).lower()
    return exc.status == 403 or any(
        marker in text for marker in ('chat not found', 'blocked by the user', 'user is deactivated')
    )


def bot_username():
    configured = os.environ.get('TELEGRAM_BOT_USERNAME', '').strip().lstrip('@')
    if configured:
        return configured
    cached = cache.get(USERNAME_CACHE_KEY)
    if cached:
        return cached
    if not is_configured():
        return None
    try:
        username = (api_call('getMe') or {}).get('username')
    except TelegramError:
        return None
    if username:
        cache.set(USERNAME_CACHE_KEY, username, 24 * 3600)
    return username


def deep_link(token):
    username = bot_username()
    return f'https://t.me/{username}?start={token}' if username else None


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------


def create_link_token(user):
    """A fresh 15-minute link token for ``user``; older ones are discarded."""
    token = secrets.token_hex(24)
    now = utc_now()
    with transaction.atomic():
        TelegramLinkToken.objects.filter(user_id=user.pk).delete()
        TelegramLinkToken.objects.filter(expires_at__lte=now).delete()
        TelegramLinkToken.objects.create(
            token=token, user_id=user.pk, expires_at=now + timedelta(minutes=LINK_TTL_MINUTES)
        )
    return token


def link_chat(user_id, chat_id):
    chat = str(chat_id)
    with transaction.atomic():
        # One chat belongs to one account.
        User.objects.filter(telegram_chat_id=chat).exclude(pk=user_id).update(telegram_chat_id=None)
        User.objects.filter(pk=user_id).update(telegram_chat_id=chat)


def consume_link_token(token, chat_id):
    """Attach ``chat_id`` to the account behind ``token``; returns the user id or None."""
    now = utc_now()
    with transaction.atomic():
        row = (
            TelegramLinkToken.objects.select_for_update()
            .filter(token=token, expires_at__gt=now)
            .first()
        )
        if row is None:
            TelegramLinkToken.objects.filter(token=token).delete()
            return None
        link_chat(row.user_id, chat_id)
        row.delete()
        return row.user_id


def handle_update(update):
    """Process one Telegram update: a message or a button press.

    A retried update (same ``update_id``) is acknowledged without being
    handled again, so a slow first attempt never accepts an offer twice.
    """
    update = update or {}
    update_id = update.get('update_id')
    if update_id is not None and not cache.add(
        f'dashawy:telegram:update:{update_id}', 1, UPDATE_DEDUP_SECONDS
    ):
        return 'duplicate'
    from . import telegram_bot

    return telegram_bot.handle_update(update)


def webhook_url(site_url):
    return f'{site_url.rstrip("/")}/api/telegram/webhook/{webhook_secret()}'


def setup_webhook(site_url):
    """Register ``<site_url>/api/telegram/webhook/<secret>`` with Telegram.

    Also publishes the command menu. Returns the registered URL.
    """
    url = webhook_url(site_url)
    api_call(
        'setWebhook',
        {
            'url': url,
            'secret_token': webhook_secret(),
            'allowed_updates': ['message', 'edited_message', 'callback_query'],
            'drop_pending_updates': True,
        },
    )
    try:
        api_call(
            'setMyCommands',
            {'commands': [{'command': c, 'description': d} for c, d in BOT_COMMANDS]},
        )
    except TelegramError as exc:
        logger.warning('setMyCommands failed: %s', exc)
    return url


def webhook_info():
    """What Telegram currently has registered (``getWebhookInfo``)."""
    return api_call('getWebhookInfo') or {}
