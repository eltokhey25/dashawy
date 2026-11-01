"""Telegram bot: account linking, the password-reset code and messages.

The Node backend long-polled ``getUpdates``, which needs a process that never
stops; on Vercel there is none, so Telegram now POSTs each update to the
webhook (``core.telegram_views.TelegramWebhookView``). ``setup_webhook``
registers that URL with Telegram once, with a secret Telegram echoes in a
header on every call.

Linking: the site creates a short-lived token and a ``t.me/<bot>?start=<token>``
link; when the person opens it, Telegram sends ``/start <token>`` to the
webhook and the chat is attached to the account. Nothing here raises on a
failed send: a notification must never break the action that triggered it.
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


def send_message(chat_id, text):
    """Plain-text message to a chat. Returns True on success, False otherwise."""
    if not chat_id or not is_configured():
        return False
    try:
        api_call(
            'sendMessage',
            {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True},
        )
        return True
    except TelegramError as exc:
        if _chat_unreachable(exc):
            # The person blocked the bot or deleted the chat: unlink so the
            # site stops relying on a channel that cannot deliver.
            User.objects.filter(telegram_chat_id=str(chat_id)).update(telegram_chat_id=None)
        logger.warning('Telegram send failed: %s', exc)
        return False


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


def handle_update(update):
    """Process one Telegram update (only ``/start`` matters)."""
    message = (update or {}).get('message') or (update or {}).get('edited_message')
    chat = (message or {}).get('chat') or {}
    chat_id = chat.get('id')
    if chat_id is None:
        return 'ignored'
    text = str(message.get('text') or '').strip()
    if not text.lower().startswith('/start'):
        return 'ignored'
    parts = text.split(maxsplit=1)
    token = parts[1].strip() if len(parts) > 1 else ''
    if not token:
        send_message(chat_id, LINK_WELCOME)
        return 'welcome'

    now = utc_now()
    with transaction.atomic():
        row = (
            TelegramLinkToken.objects.select_for_update()
            .filter(token=token, expires_at__gt=now)
            .first()
        )
        if row is None:
            TelegramLinkToken.objects.filter(token=token).delete()
        else:
            link_chat(row.user_id, chat_id)
            row.delete()
    if row is None:
        send_message(chat_id, LINK_INVALID)
        return 'invalid'
    send_message(chat_id, LINK_SUCCESS)
    return 'linked'


def setup_webhook(site_url):
    """Register ``<site_url>/api/telegram/webhook/<secret>`` with Telegram."""
    secret = webhook_secret()
    url = f'{site_url.rstrip("/")}/api/telegram/webhook/{secret}'
    api_call(
        'setWebhook',
        {
            'url': url,
            'secret_token': secret,
            'allowed_updates': ['message', 'edited_message'],
            'drop_pending_updates': True,
        },
    )
    return url
