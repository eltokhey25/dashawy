"""Telegram linking through the webhook, the password-reset code, and the
notifications that also reach Telegram (login included)."""

import hashlib
import json
from unittest.mock import patch

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import Customer, Otp, TelegramLinkToken, User
from .services import telegram

PASSWORD = 'TesterPass123'
BOT_ENV = {'TELEGRAM_BOT_TOKEN': '123456:test-token', 'TELEGRAM_BOT_USERNAME': 'dashawybot'}


class TelegramBase(APITestCase):
    def setUp(self):
        cache.clear()
        env = patch.dict('os.environ', BOT_ENV)
        env.start()
        self.addCleanup(env.stop)
        # Never call api.telegram.org from tests: capture every send instead.
        sender = patch('core.services.telegram.api_call', return_value={'ok': True})
        self.api_call = sender.start()
        self.addCleanup(sender.stop)
        push = patch('core.services.push.notify', return_value={'sent': 0, 'failed': 0})
        push.start()
        self.addCleanup(push.stop)
        self.user = User.objects.create(
            email='tg-customer@example.com',
            phone='+201000000901',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        Customer.objects.create(user=self.user, name='عميل تليجرام', phone='+201000000901')

    def sent_texts(self):
        return [
            call.args[1]['text']
            for call in self.api_call.call_args_list
            if call.args[0] == 'sendMessage'
        ]

    def webhook(self, update, secret=None, header=None):
        secret = telegram.webhook_secret() if secret is None else secret
        header = secret if header is None else header
        return self.client.post(
            f'/api/telegram/webhook/{secret}',
            data=json.dumps(update),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=header,
        )

    _update_id = 0

    def start_update(self, text, chat_id=555):
        # Telegram numbers its updates; a repeated id is a retry and is skipped.
        TelegramBase._update_id += 1
        return {'update_id': TelegramBase._update_id, 'message': {'chat': {'id': chat_id}, 'text': text}}


class LinkingTests(TelegramBase):
    def test_link_is_for_the_signed_in_account_only(self):
        # Naming an account by phone or email from outside must not work:
        # that was an account takeover (link, then reset the password).
        anonymous = self.client.post('/api/auth/telegram-link', {'identifier': '01000000901'}, format='json')
        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(TelegramLinkToken.objects.exists())

        other = User.objects.create(
            email='tg-other@example.com', phone='+201000000902', password_hash='x', role='customer'
        )
        self.client.force_authenticate(user=other)
        made = self.client.post('/api/auth/telegram-link', {'identifier': '01000000901'}, format='json')
        self.assertEqual(made.status_code, 200)
        # The identifier is ignored: the token belongs to the caller.
        self.assertTrue(TelegramLinkToken.objects.filter(token=made.json()['token'], user=other).exists())
        TelegramLinkToken.objects.all().delete()

        self.client.force_authenticate(user=self.user)
        made = self.client.post('/api/auth/telegram-link', {}, format='json')
        self.assertEqual(made.status_code, 200)
        body = made.json()
        self.assertEqual(body['deep_link'], f"https://t.me/dashawybot?start={body['token']}")
        self.assertTrue(TelegramLinkToken.objects.filter(token=body['token'], user=self.user).exists())

        hit = self.webhook(self.start_update(f"/start {body['token']}"))
        self.assertEqual(hit.status_code, 200)
        self.assertEqual(hit.json()['result'], 'linked')
        self.user.refresh_from_db()
        self.assertEqual(self.user.telegram_chat_id, '555')
        self.assertFalse(TelegramLinkToken.objects.exists())
        self.assertIn('تم ربط حسابك', self.sent_texts()[-1])

        self.client.force_authenticate(user=self.user)
        self.assertTrue(self.client.get('/api/auth/telegram-link').json()['linked'])
        again = self.client.post('/api/auth/telegram-link', {}, format='json')
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)

    def test_bad_token_and_bare_start(self):
        self.assertEqual(self.webhook(self.start_update('/start')).json()['result'], 'welcome')
        self.assertEqual(self.webhook(self.start_update('/start nope')).json()['result'], 'invalid')
        self.assertEqual(self.webhook({'message': {'chat': {'id': 1}, 'text': 'hello'}}).json()['result'], 'unlinked')

    def test_webhook_refuses_wrong_secret_or_header(self):
        update = self.start_update('/start x')
        self.assertEqual(self.webhook(update, secret='0' * 40).status_code, 404)
        self.assertEqual(self.webhook(update, header='wrong').status_code, 404)
        self.assertEqual(self.client.get(f'/api/telegram/webhook/{telegram.webhook_secret()}').status_code, 404)

    def test_unconfigured_bot(self):
        self.client.force_authenticate(user=self.user)
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': ''}):
            off = self.client.post('/api/auth/telegram-link', {}, format='json')
        self.assertEqual(off.status_code, 503)


class PasswordResetTests(TelegramBase):
    def link(self):
        User.objects.filter(pk=self.user.pk).update(telegram_chat_id='555')

    def code_from_telegram(self):
        text = self.sent_texts()[-1]
        return ''.join(ch for ch in text if ch.isdigit())[:6]

    def test_code_is_sent_and_resets_the_password(self):
        self.link()
        asked = self.client.post('/api/auth/request-otp', {'identifier': '01000000901'}, format='json')
        self.assertEqual(asked.status_code, 200)
        code = self.code_from_telegram()
        self.assertRegex(code, r'^\d{6}$')
        row = Otp.objects.get(email='+201000000901')
        self.assertEqual(row.otp_hash, hashlib.sha256(code.encode()).hexdigest())

        wrong = self.client.post(
            '/api/auth/reset-password',
            {'identifier': '01000000901', 'otp': '000000', 'new_password': 'NewPass123'},
            format='json',
        )
        self.assertEqual(wrong.status_code, 400)

        reset = self.client.post(
            '/api/auth/reset-password',
            {'identifier': '01000000901', 'otp': code, 'new_password': 'NewPass123'},
            format='json',
        )
        self.assertEqual(reset.status_code, 200)
        self.assertIn('token', reset.json())
        login = self.client.post(
            '/api/auth/login', {'identifier': '01000000901', 'password': 'NewPass123'}, format='json'
        )
        self.assertEqual(login.status_code, 200)
        old = self.client.post(
            '/api/auth/login', {'identifier': '01000000901', 'password': PASSWORD}, format='json'
        )
        self.assertEqual(old.status_code, 401)
        # The code is single-use.
        twice = self.client.post(
            '/api/auth/reset-password',
            {'identifier': '01000000901', 'otp': code, 'new_password': 'Other123x'},
            format='json',
        )
        self.assertEqual(twice.status_code, 400)
        # The owner hears about the change and the sign-in on Telegram.
        texts = self.sent_texts()
        self.assertTrue(any('تغيير كلمة السر' in t for t in texts))
        self.assertTrue(any('تسجيل الدخول' in t for t in texts))

    def test_unlinked_and_unknown_accounts_get_the_same_answer(self):
        not_linked = self.client.post('/api/auth/request-otp', {'identifier': '01000000901'}, format='json')
        unknown = self.client.post('/api/auth/request-otp', {'identifier': '01099999999'}, format='json')
        self.assertEqual(not_linked.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(not_linked.json(), unknown.json())
        self.assertEqual(Otp.objects.count(), 0)
        self.assertEqual(self.sent_texts(), [])

    def test_cooldown_and_weak_password(self):
        self.link()
        self.client.post('/api/auth/request-otp', {'identifier': '01000000901'}, format='json')
        again = self.client.post('/api/auth/request-otp', {'identifier': '01000000901'}, format='json')
        self.assertEqual(again.status_code, 429)
        code = self.code_from_telegram()
        weak = self.client.post(
            '/api/auth/reset-password',
            {'identifier': '01000000901', 'otp': code, 'new_password': '12345678'},
            format='json',
        )
        self.assertEqual(weak.status_code, 400)
        # A rejected password does not burn the code.
        self.assertTrue(Otp.objects.filter(used=False).exists())

    def test_failed_send_leaves_no_code_behind(self):
        self.link()
        self.api_call.side_effect = telegram.TelegramError('boom', status=500)
        response = self.client.post('/api/auth/request-otp', {'identifier': '01000000901'}, format='json')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Otp.objects.count(), 0)

    def test_blocked_bot_unlinks_the_account(self):
        self.link()
        self.api_call.side_effect = telegram.TelegramError(
            'Forbidden: bot was blocked by the user', status=403, description='bot was blocked by the user'
        )
        response = self.client.post('/api/auth/request-otp', {'identifier': '01000000901'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Otp.objects.count(), 0)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_chat_id)


class NotificationChannelTests(TelegramBase):
    def test_login_is_announced_on_telegram(self):
        User.objects.filter(pk=self.user.pk).update(telegram_chat_id='555')
        response = self.client.post(
            '/api/auth/login', {'identifier': '01000000901', 'password': PASSWORD}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        texts = self.sent_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn('تم تسجيل الدخول لحسابك', texts[0])
        self.assertIn('/change-password', texts[0])

    def test_every_notification_reaches_a_linked_account(self):
        from .services.notifications import notify_chat_message

        User.objects.filter(pk=self.user.pk).update(telegram_chat_id='555')
        notify_chat_message(self.user.pk, 'الفني', 'السلام عليكم', 42, 'customer')
        self.assertIn('رسالة جديدة', self.sent_texts()[-1])
        # The link to the thread is a button under the message.
        last = [c.args[1] for c in self.api_call.call_args_list if c.args[0] == 'sendMessage'][-1]
        urls = [b.get('url') for row in last['reply_markup']['inline_keyboard'] for b in row]
        self.assertTrue(any(u and u.endswith('/chat.html?id=42') for u in urls))

    def test_unlinked_account_gets_nothing_on_telegram(self):
        self.client.post('/api/auth/login', {'identifier': '01000000901', 'password': PASSWORD}, format='json')
        self.assertEqual(self.sent_texts(), [])
