"""The Telegram bot: commands, inline buttons, the step-by-step flows, the
notifications it sends with buttons, and the webhook registration."""

import json
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from io import StringIO
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import (
    Customer,
    Message,
    Request,
    RequestBroadcast,
    Specialty,
    Technician,
    TechnicianWallet,
    TelegramLinkToken,
    User,
)
from .services import telegram, telegram_bot
from .services.jobs import place_order

PASSWORD = 'TesterPass123'
BOT_ENV = {
    'TELEGRAM_BOT_TOKEN': '123456:test-token',
    'TELEGRAM_BOT_USERNAME': 'dashawybot',
    'SITE_URL': 'https://dashawy.vercel.app',
}
CUSTOMER_CHAT = 555
TECH_CHAT = 777
OTHER_TECH_CHAT = 778


class BotBase(APITestCase):
    def setUp(self):
        cache.clear()
        env = patch.dict('os.environ', BOT_ENV)
        env.start()
        self.addCleanup(env.stop)
        # Never call api.telegram.org from tests: capture every call instead.
        sender = patch('core.services.telegram.api_call', return_value={'ok': True})
        self.api_call = sender.start()
        self.addCleanup(sender.stop)
        push = patch('core.services.push.notify', return_value={'sent': 0, 'failed': 0})
        push.start()
        self.addCleanup(push.stop)
        with patch('django.conf.settings.SITE_URL', 'https://dashawy.vercel.app'):
            from .site_config import get_site_config

            get_site_config(force=True)

        Specialty.objects.create(key='dish_install', label='تركيب دش', sort_order=1)
        self.customer_user = User.objects.create(
            email='bot-customer@example.com',
            phone='+201000000701',
            password_hash=hash_password(PASSWORD),
            role='customer',
            telegram_chat_id=str(CUSTOMER_CHAT),
        )
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل البوت', phone='+201000000701'
        )
        self.tech_user = self.make_technician('bot-tech', '+201000000702', 'فني البوت', TECH_CHAT)
        self.technician = self.tech_user.technician
        self.other_tech_user = self.make_technician(
            'bot-tech-2', '+201000000703', 'فني تاني', OTHER_TECH_CHAT
        )

    def make_technician(self, slug, phone, name, chat_id):
        user = User.objects.create(
            email=f'{slug}@example.com',
            phone=phone,
            password_hash=hash_password(PASSWORD),
            role='technician',
            telegram_chat_id=str(chat_id),
        )
        Technician.objects.create(
            user=user,
            name=name,
            phone=phone,
            specialty='dish_install',
            availability='available',
            approval_status='approved',
        )
        return user

    # -- Telegram plumbing -------------------------------------------------

    def webhook(self, update):
        secret = telegram.webhook_secret()
        response = self.client.post(
            f'/api/telegram/webhook/{secret}',
            data=json.dumps(update),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()['result']

    _update_id = 1000

    def _next_id(self):
        BotBase._update_id += 1
        return BotBase._update_id

    def say(self, text, chat_id=CUSTOMER_CHAT):
        return self.webhook(
            {
                'update_id': self._next_id(),
                'message': {'message_id': 1, 'chat': {'id': chat_id, 'type': 'private'}, 'text': text},
            }
        )

    def press(self, data, chat_id=CUSTOMER_CHAT, message_id=10):
        return self.webhook(
            {
                'update_id': self._next_id(),
                'callback_query': {
                    'id': 'cb',
                    'data': data,
                    'message': {'message_id': message_id, 'chat': {'id': chat_id, 'type': 'private'}},
                },
            }
        )

    def outgoing(self, chat_id=None):
        """Every message the bot produced (sent or edited), oldest first."""
        rows = []
        for call in self.api_call.call_args_list:
            method, payload = call.args[0], call.args[1]
            if method not in ('sendMessage', 'editMessageText'):
                continue
            if chat_id is not None and str(payload['chat_id']) != str(chat_id):
                continue
            rows.append(payload)
        return rows

    def last(self, chat_id=None):
        rows = self.outgoing(chat_id)
        self.assertTrue(rows, 'the bot sent nothing')
        return rows[-1]

    def buttons(self, payload):
        markup = payload.get('reply_markup') or {}
        return [
            button.get('callback_data') or button.get('url')
            for row in markup.get('inline_keyboard', [])
            for button in row
        ]

    def reset_outbox(self):
        self.api_call.reset_mock()

    # -- Domain helpers ----------------------------------------------------

    def order(self, description='الدش مش شغال'):
        from .serializers import CreateRequestSerializer

        serializer = CreateRequestSerializer(
            data={'specialty': 'dish_install', 'description': description, 'area_key': 'smouha'}
        )
        serializer.is_valid(raise_exception=True)
        job, _ = place_order(self.customer, serializer.validated_data)
        self.reset_outbox()
        return job

    def offer_for(self, job, tech=None):
        tech = tech or self.technician
        return RequestBroadcast.objects.get(request=job, technician=tech)

    def assigned_job(self):
        job = self.order()
        self.press(f'offer:accept:{self.offer_for(job).pk}', TECH_CHAT)
        job.refresh_from_db()
        self.assertEqual(job.status, 'assigned')
        self.reset_outbox()
        return job


class LinkingAndMenuTests(BotBase):
    def test_unlinked_chat_gets_the_link_instructions(self):
        self.assertEqual(self.say('/help', chat_id=999), 'unlinked')
        last = self.last(999)
        self.assertIn('مش مربوط', last['text'])
        self.assertIn('https://dashawy.vercel.app/telegram-link.html', self.buttons(last))
        self.assertEqual(self.say('/start', chat_id=999), 'welcome')
        self.assertEqual(self.press('menu:requests', chat_id=999), 'unlinked')

    def test_pasting_the_link_code_links_the_account(self):
        fresh = User.objects.create(
            email='fresh@example.com', phone='+201000000709', password_hash='x', role='customer'
        )
        Customer.objects.create(user=fresh, name='جديد', phone='+201000000709')
        token = telegram.create_link_token(fresh)
        self.assertEqual(self.say(token, chat_id=999), 'linked')
        fresh.refresh_from_db()
        self.assertEqual(fresh.telegram_chat_id, '999')
        self.assertFalse(TelegramLinkToken.objects.exists())
        self.assertIn('menu:requests', self.buttons(self.last(999)))
        # /start <token> still works for the deep link and shows the menu.
        token = telegram.create_link_token(self.customer_user)
        self.assertEqual(self.say(f'/start {token}', chat_id=556), 'linked')
        self.customer_user.refresh_from_db()
        self.assertEqual(self.customer_user.telegram_chat_id, '556')

    def test_start_help_and_unknown_command(self):
        self.assertEqual(self.say('/start'), 'start')
        self.assertEqual(self.buttons(self.last()), ['menu:requests', 'new:start', 'menu:support', 'menu:help'])
        self.assertEqual(self.say('/help'), 'help')
        self.assertIn('/track', self.last()['text'])
        self.assertEqual(self.say('/foo'), 'unknown')
        self.assertIn('مش فاهم', self.last()['text'])
        self.assertEqual(self.say('كلام سايب'), 'unknown')
        self.assertEqual(self.say('/help', TECH_CHAT), 'help')
        self.assertIn('/incoming', self.last(TECH_CHAT)['text'])

    def test_menu_buttons_edit_the_menu_message_and_respect_roles(self):
        self.assertEqual(self.press('menu:help'), 'menu:help')
        self.assertEqual(self.api_call.call_args_list[-1].args[0], 'editMessageText')
        self.assertEqual(self.last()['message_id'], 10)
        self.assertEqual(self.press('menu:support'), 'menu:support')
        self.assertIn('https://dashawy.vercel.app', self.buttons(self.last()))
        # A customer pressing a technician button just gets the menu back.
        self.assertEqual(self.press('menu:incoming'), 'unknown')
        self.assertIn('menu:requests', self.buttons(self.last()))
        # Every button press is acknowledged so Telegram stops the spinner.
        self.assertTrue(any(c.args[0] == 'answerCallbackQuery' for c in self.api_call.call_args_list))

    def test_blocked_account_and_group_chats(self):
        User.objects.filter(pk=self.customer_user.pk).update(blocked=True)
        self.assertEqual(self.say('/start'), 'blocked')
        self.assertIn('موقوف', self.last()['text'])
        ignored = self.webhook(
            {'update_id': 5, 'message': {'chat': {'id': -100, 'type': 'group'}, 'text': '/start'}}
        )
        self.assertEqual(ignored, 'ignored')

    def test_retried_update_is_handled_once(self):
        update = {
            'update_id': 424242,
            'message': {'message_id': 1, 'chat': {'id': CUSTOMER_CHAT, 'type': 'private'}, 'text': '/start'},
        }
        self.assertEqual(self.webhook(update), 'start')
        self.assertEqual(self.webhook(update), 'duplicate')
        self.assertEqual(len(self.outgoing()), 1)

    def test_admin_summary(self):
        admin = User.objects.create(
            email='bot-admin@example.com', password_hash='x', role='admin', telegram_chat_id='900'
        )
        self.assertEqual(self.say('/start', 900), 'start')
        self.assertIn('menu:admin', self.buttons(self.last(900)))
        self.assertEqual(self.press('menu:admin', 900), 'menu:admin')
        self.assertIn('فنيين مستنيين الاعتماد: 0', self.last(900)['text'])
        self.assertEqual(admin.role, 'admin')


class CustomerTests(BotBase):
    def test_requests_list_and_details(self):
        self.assertEqual(self.say('/requests'), 'requests')
        self.assertIn('مفيش طلبات شغالة', self.last()['text'])
        job = self.order()
        self.assertEqual(self.say('/requests'), 'requests')
        text = self.last()['text']
        self.assertIn(f'#{job.pk}', text)
        self.assertIn('مستني فني', text)
        self.assertIn(f'req:view:{job.pk}', self.buttons(self.last()))

        self.assertEqual(self.press(f'req:view:{job.pk}'), 'req:view')
        detail = self.last()
        self.assertIn('تركيب دش', detail['text'])
        self.assertIn('سموحة', detail['text'])
        self.assertIn(f'req:cancel:{job.pk}', self.buttons(detail))
        self.assertNotIn(f'chat:open:{job.pk}', self.buttons(detail))

        self.assertEqual(self.say(f'/track {job.pk}'), 'track')
        self.assertIn(f'طلب #{job.pk}', self.last()['text'])
        self.assertEqual(self.say('/track abc'), 'track')
        self.assertIn('/track 12', self.last()['text'])
        self.assertEqual(self.press('req:view:999999'), 'req:view')
        self.assertIn('غير موجود', self.last()['text'])

    def test_details_after_a_technician_accepts(self):
        job = self.assigned_job()
        self.press(f'req:view:{job.pk}')
        detail = self.last(CUSTOMER_CHAT)
        self.assertIn('فني البوت — +201000000702', detail['text'])
        self.assertIn(f'كود البداية: {job.start_pin}', detail['text'])
        self.assertIn(f'chat:open:{job.pk}', self.buttons(detail))

    def test_new_order_step_by_step(self):
        self.assertEqual(self.say('/new'), 'new')
        self.assertIn('new:spec:dish_install', self.buttons(self.last()))
        self.assertEqual(self.press('new:spec:dish_install'), 'new:spec')
        self.assertIn('new:area:smouha', self.buttons(self.last()))
        self.assertEqual(self.press('new:area:smouha'), 'new:area')
        self.assertIn('العنوان', self.last()['text'])
        self.assertEqual(self.say('شارع فوزي معاذ، عمارة 5'), 'new')
        self.assertIn('اوصف المشكلة', self.last()['text'])
        self.assertEqual(self.say('الدش مش بيجيب إشارة'), 'new')
        summary = self.last()
        self.assertIn('سموحة', summary['text'])
        self.assertIn('شارع فوزي معاذ', summary['text'])
        self.assertIn('new:confirm', self.buttons(summary))
        self.reset_outbox()

        self.assertEqual(self.press('new:confirm'), 'new:confirm')
        job = Request.objects.get(customer=self.customer)
        self.assertEqual(job.status, 'pending')
        self.assertEqual(job.specialty, 'dish_install')
        self.assertEqual(job.location_text, 'سموحة — شارع فوزي معاذ، عمارة 5')
        self.assertEqual(job.description, 'الدش مش بيجيب إشارة')
        self.assertIn(f'تم استلام طلبك #{job.pk}', self.last(CUSTOMER_CHAT)['text'])
        self.assertIn('اتعرض على 2 فني', self.last(CUSTOMER_CHAT)['text'])
        self.assertIsNone(telegram_bot.get_state(CUSTOMER_CHAT))
        # Both technicians were offered the job on Telegram, with buttons.
        for chat in (TECH_CHAT, OTHER_TECH_CHAT):
            offer = self.last(chat)
            self.assertIn('طلب جديد', offer['text'])
            self.assertIn(f'offer:accept:r{job.pk}', self.buttons(offer))
            self.assertIn(f'offer:decline:r{job.pk}', self.buttons(offer))

    def test_new_order_other_service_skip_address_and_abort(self):
        Specialty.objects.create(key='other', label='خدمة أخرى', sort_order=9)
        self.say('/new')
        self.press('new:spec:other')
        self.assertIn('اسم الخدمة', self.last()['text'])
        self.say('تركيب تلفزيون على الحيطة')
        self.press('new:area:miami')
        self.press('new:skipaddress')
        self.say('محتاج حد النهارده')
        self.assertIn('تركيب تلفزيون على الحيطة', self.last()['text'])
        self.press('new:confirm')
        job = Request.objects.get(customer=self.customer)
        self.assertEqual(job.specialty, 'other')
        self.assertEqual(job.custom_specialty_text, 'تركيب تلفزيون على الحيطة')
        self.assertEqual(job.location_text, 'ميامي')

        self.say('/new')
        self.assertEqual(self.press('new:abort'), 'new:abort')
        self.assertIsNone(telegram_bot.get_state(CUSTOMER_CHAT))
        self.assertEqual(Request.objects.count(), 1)
        # A confirm with no flow open does not create anything.
        self.assertEqual(self.press('new:confirm'), 'new:confirm')
        self.assertIn('انتهت صلاحيته', self.last()['text'])
        self.assertEqual(Request.objects.count(), 1)

    def test_open_order_limit_is_reported(self):
        for _ in range(3):
            self.order()
        self.say('/new')
        self.press('new:spec:dish_install')
        self.press('new:area:smouha')
        self.press('new:skipaddress')
        self.say('رابع طلب')
        self.assertEqual(self.press('new:confirm'), 'error')
        self.assertIn('3 طلبات شغالة', self.last()['text'])
        self.assertEqual(Request.objects.count(), 3)

    def test_cancel_with_confirmation_tells_the_technician(self):
        job = self.assigned_job()
        self.assertEqual(self.press(f'req:cancel:{job.pk}'), 'req:cancel')
        self.assertIn(f'req:cancelyes:{job.pk}', self.buttons(self.last(CUSTOMER_CHAT)))
        job.refresh_from_db()
        self.assertEqual(job.status, 'assigned')
        self.assertEqual(self.press(f'req:cancelyes:{job.pk}'), 'req:cancelyes')
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.assertIn('تم إلغاء', self.last(CUSTOMER_CHAT)['text'])
        self.assertIn('لغى الطلب', self.last(TECH_CHAT)['text'])
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')
        # Not twice.
        self.assertEqual(self.press(f'req:cancelyes:{job.pk}'), 'req:cancelyes')
        self.assertIn('مش ممكن', self.last(CUSTOMER_CHAT)['text'])


class TechnicianTests(BotBase):
    def test_incoming_offers_accept_and_the_customer_is_told(self):
        self.assertEqual(self.say('/incoming', TECH_CHAT), 'incoming')
        self.assertIn('مفيش طلبات معروضة', self.last(TECH_CHAT)['text'])
        job = self.order()
        self.say('/incoming', TECH_CHAT)
        offer = self.offer_for(job)
        listing = self.last(TECH_CHAT)
        self.assertIn(f'#{job.pk}', listing['text'])
        self.assertIn(f'offer:accept:{offer.pk}', self.buttons(listing))
        self.assertIn(f'offer:decline:{offer.pk}', self.buttons(listing))

        self.assertEqual(self.press(f'offer:accept:{offer.pk}', TECH_CHAT), 'offer:accept')
        job.refresh_from_db()
        self.assertEqual(job.status, 'assigned')
        self.assertEqual(job.technician_id, self.technician.pk)
        tech_view = self.last(TECH_CHAT)
        self.assertIn('قبلت الطلب', tech_view['text'])
        self.assertIn('+201000000701', tech_view['text'])
        self.assertIn(f'job:onway:{job.pk}', self.buttons(tech_view))
        self.assertIn(f'job:start:{job.pk}', self.buttons(tech_view))
        customer_note = self.last(CUSTOMER_CHAT)
        self.assertIn('تم قبول طلبك', customer_note['text'])
        self.assertIn(job.start_pin, customer_note['text'])
        self.assertIn(f'chat:open:{job.pk}', self.buttons(customer_note))

        # The second technician lost the race and is told so.
        self.assertEqual(self.press(f'offer:accept:r{job.pk}', OTHER_TECH_CHAT), 'error')
        self.assertIn('فني تاني قبل الطلب', self.last(OTHER_TECH_CHAT)['text'])

    def test_accept_from_the_notification_button_and_decline(self):
        job = self.order()
        self.assertEqual(self.press(f'offer:decline:r{job.pk}', OTHER_TECH_CHAT), 'offer:decline')
        self.assertEqual(self.offer_for(job, self.other_tech_user.technician).status, 'declined')
        self.assertEqual(self.press(f'offer:accept:r{job.pk}', TECH_CHAT), 'offer:accept')
        job.refresh_from_db()
        self.assertEqual(job.technician_id, self.technician.pk)
        self.assertEqual(self.press('offer:accept:r424242', TECH_CHAT), 'offer:accept')
        self.assertIn('مش موجود', self.last(TECH_CHAT)['text'])

    def test_busy_technician_cannot_accept_a_second_job(self):
        self.assigned_job()
        second = self.order()
        self.assertEqual(self.press(f'offer:accept:r{second.pk}', TECH_CHAT), 'offer:accept')
        self.assertIn('مش موجود', self.last(TECH_CHAT)['text'])  # busy: never offered

    def test_on_the_way_start_with_pin_and_complete(self):
        job = self.assigned_job()
        self.assertEqual(self.say('/jobs', TECH_CHAT), 'jobs')
        self.assertIn(f'job:view:{job.pk}', self.buttons(self.last(TECH_CHAT)))

        self.assertEqual(self.press(f'job:onway:{job.pk}', TECH_CHAT), 'job:onway')
        self.assertIn('في الطريق', self.last(CUSTOMER_CHAT)['text'])
        self.assertEqual(Message.objects.filter(request=job, sender_role='technician').count(), 1)
        self.reset_outbox()
        self.assertEqual(self.press(f'job:onway:{job.pk}', TECH_CHAT), 'job:onway')
        self.assertIn('قبل كده', self.last(TECH_CHAT)['text'])
        self.assertEqual(self.outgoing(CUSTOMER_CHAT), [])
        self.assertEqual(Message.objects.filter(request=job).count(), 1)

        self.assertEqual(self.press(f'job:start:{job.pk}', TECH_CHAT), 'job:start')
        self.assertIn('4 أرقام', self.last(TECH_CHAT)['text'])
        self.assertEqual(self.say('0000' if job.start_pin != '0000' else '1111', TECH_CHAT), 'pin')
        self.assertIn('الكود غلط', self.last(TECH_CHAT)['text'])
        job.refresh_from_db()
        self.assertEqual(job.status, 'assigned')
        self.assertEqual(self.say(job.start_pin, TECH_CHAT), 'pin')
        job.refresh_from_db()
        self.assertEqual(job.status, 'in_progress')
        self.assertIn('بدأ الشغل', self.last(TECH_CHAT)['text'])
        self.assertIn(f'job:complete:{job.pk}', self.buttons(self.last(TECH_CHAT)))
        self.assertIn('بدأ الشغل في طلبك', self.last(CUSTOMER_CHAT)['text'])
        self.assertIsNone(telegram_bot.get_state(TECH_CHAT))

        self.assertEqual(self.press(f'job:complete:{job.pk}', TECH_CHAT), 'job:complete')
        self.assertEqual(self.say('ok', TECH_CHAT), 'error')
        self.assertIn('5 أحرف', self.last(TECH_CHAT)['text'])
        self.assertEqual(self.say('غيرت الكابل وظبطت الإشارة', TECH_CHAT), 'notes')
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.work_notes, 'غيرت الكابل وظبطت الإشارة')
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')
        self.assertIn('خلّص الشغل', self.last(CUSTOMER_CHAT)['text'])
        self.assertIn(
            f'https://dashawy.vercel.app/customer/my-requests.html?review={job.pk}',
            self.buttons(self.last(CUSTOMER_CHAT)),
        )

    def test_hand_back_an_assigned_job(self):
        job = self.assigned_job()
        self.assertEqual(self.press(f'job:reject:{job.pk}', TECH_CHAT), 'job:reject')
        self.assertIn(f'job:rejectyes:{job.pk}', self.buttons(self.last(TECH_CHAT)))
        self.assertEqual(self.press(f'job:rejectyes:{job.pk}', TECH_CHAT), 'job:rejectyes')
        job.refresh_from_db()
        self.assertEqual(job.status, 'pending')
        self.assertIsNone(job.technician_id)
        self.assertIn('اتعرض على 1 فني', self.last(TECH_CHAT)['text'])
        self.assertIn('بندوّر على فني تاني', self.last(CUSTOMER_CHAT)['text'])
        self.assertEqual(self.offer_for(job, self.other_tech_user.technician).status, 'sent')
        # Not offered to the one who handed it back.
        self.assertEqual(self.offer_for(job).status, 'declined')

    def test_wallet(self):
        self.assertEqual(self.say('/wallet', TECH_CHAT), 'wallet')
        self.assertIn('الرصيد القابل للسحب: 0 ج.م', self.last(TECH_CHAT)['text'])
        TechnicianWallet.objects.filter(pk=self.technician.pk).update(balance=250, total_debt=120)
        self.reset_outbox()
        self.assertEqual(self.press('menu:wallet', TECH_CHAT), 'menu:wallet')
        text = self.last(TECH_CHAT)['text']
        self.assertIn('250 ج.م', text)
        self.assertIn('المستحق عليك للمنصة: 120 ج.م', text)
        self.assertIn('?tab=wallet', self.buttons(self.last(TECH_CHAT))[0])


class ChatTests(BotBase):
    def test_both_sides_chat_through_the_bot(self):
        job = self.assigned_job()
        self.assertEqual(self.press(f'chat:open:{job.pk}'), 'chat:open')
        self.assertIn('فني البوت', self.last(CUSTOMER_CHAT)['text'])
        self.assertEqual(self.say('هتوصل إمتى؟'), 'chat')
        message = Message.objects.get(request=job, sender_role='customer')
        self.assertEqual(message.body, 'هتوصل إمتى؟')
        note = self.last(TECH_CHAT)
        self.assertIn('رسالة جديدة', note['text'])
        self.assertIn('العميل: هتوصل إمتى؟', note['text'])
        self.assertIn(f'chat:open:{job.pk}', self.buttons(note))
        self.assertIn(f'https://dashawy.vercel.app/chat.html?id={job.pk}', self.buttons(note))

        # The technician replies from the notification's button.
        self.assertEqual(self.press(f'chat:open:{job.pk}', TECH_CHAT), 'chat:open')
        self.assertEqual(self.say('ربع ساعة وأكون عندك', TECH_CHAT), 'chat')
        self.assertEqual(Message.objects.filter(request=job, sender_role='technician').count(), 1)
        self.assertIn('فني البوت: ربع ساعة', self.last(CUSTOMER_CHAT)['text'])

        # The chat stays open until closed; the same words twice are two
        # messages and two notifications. Then free text is a stray message.
        self.reset_outbox()
        self.assertEqual(self.say('تمام', TECH_CHAT), 'chat')
        self.assertEqual(self.say('تمام', TECH_CHAT), 'chat')
        self.assertEqual(len(self.outgoing(CUSTOMER_CHAT)), 2)
        self.assertEqual(self.press('chat:close:0', TECH_CHAT), 'chat:close')
        self.assertEqual(self.say('تمام', TECH_CHAT), 'unknown')
        self.assertEqual(Message.objects.filter(request=job).count(), 4)

    def test_chat_needs_an_assigned_technician(self):
        job = self.order()
        self.assertEqual(self.press(f'chat:open:{job.pk}'), 'error')
        self.assertIn('لا يوجد فني', self.last(CUSTOMER_CHAT)['text'])
        other = User.objects.create(
            email='stranger@example.com', phone='+201000000799', password_hash='x',
            role='customer', telegram_chat_id='888',
        )
        Customer.objects.create(user=other, name='غريب', phone='+201000000799')
        self.assigned_job()
        self.assertEqual(self.press(f'chat:open:{job.pk}', 888), 'error')


class NotificationTests(BotBase):
    def test_same_notification_is_not_sent_twice(self):
        from .services.notifications import notify_request_accepted

        job = self.assigned_job()
        job = Request.objects.select_related('customer__user', 'technician').get(pk=job.pk)
        self.reset_outbox()
        notify_request_accepted(job)
        notify_request_accepted(job)
        self.assertEqual(len(self.outgoing(CUSTOMER_CHAT)), 0)  # already sent on accept
        Request.objects.filter(pk=job.pk).update(start_pin='9876')
        job.refresh_from_db()
        notify_request_accepted(job)
        self.assertEqual(len(self.outgoing(CUSTOMER_CHAT)), 1)  # a different text goes out

    def test_fan_out_skips_chats_that_already_got_the_offer(self):
        from .services.broadcast import broadcast_request

        job = self.order()
        broadcast_request(job.pk)  # re-broadcast: same text to the same technicians
        self.assertEqual(self.outgoing(TECH_CHAT), [])
        self.assertEqual(self.outgoing(OTHER_TECH_CHAT), [])

    def test_chat_notification_links_the_thread(self):
        from .services.notifications import notify_chat_message

        notify_chat_message(self.customer_user.pk, 'الفني', 'السلام عليكم', 42, 'customer')
        note = self.last(CUSTOMER_CHAT)
        self.assertIn('رسالة جديدة', note['text'])
        self.assertIn('https://dashawy.vercel.app/chat.html?id=42', self.buttons(note))


class WebhookSetupTests(BotBase):
    def test_management_command_registers_url_commands_and_buttons(self):
        out = StringIO()
        call_command('telegram_webhook', url='https://dashawy.vercel.app', stdout=out)
        methods = {c.args[0]: c.args[1] for c in self.api_call.call_args_list}
        self.assertIn('setWebhook', methods)
        self.assertEqual(
            methods['setWebhook']['url'],
            f'https://dashawy.vercel.app/api/telegram/webhook/{telegram.webhook_secret()}',
        )
        self.assertEqual(methods['setWebhook']['secret_token'], telegram.webhook_secret())
        self.assertIn('callback_query', methods['setWebhook']['allowed_updates'])
        commands = {c['command'] for c in methods['setMyCommands']['commands']}
        self.assertTrue({'start', 'help', 'requests', 'new', 'incoming', 'jobs', 'wallet'} <= commands)
        self.assertIn('Webhook registered', out.getvalue())

    def test_command_refuses_http_and_show_prints_state(self):
        from django.core.management import CommandError

        with self.assertRaises(CommandError):
            call_command('telegram_webhook', url='http://localhost:8000')
        self.api_call.return_value = {'url': 'https://old.example/hook', 'pending_update_count': 3}
        out = StringIO()
        call_command('telegram_webhook', show=True, url='https://dashawy.vercel.app', stdout=out)
        self.assertIn('registered: https://old.example/hook', out.getvalue())
        self.assertIn('pending:    3', out.getvalue())
        self.assertNotIn('setWebhook', {c.args[0] for c in self.api_call.call_args_list})

    def test_admin_endpoint_is_for_the_super_admin_only(self):
        expected = telegram.webhook_url('https://dashawy.vercel.app')
        self.client.force_authenticate(user=self.customer_user)
        self.assertEqual(self.client.get('/api/admin/telegram/webhook').status_code, 403)
        boss = User.objects.create(
            email='boss@example.com', password_hash='x', role='admin', is_super_admin=True
        )
        self.client.force_authenticate(user=boss)
        self.api_call.return_value = {'url': '', 'pending_update_count': 0}
        state = self.client.get('/api/admin/telegram/webhook')
        self.assertEqual(state.status_code, 200)
        self.assertFalse(state.json()['registered'])
        self.assertEqual(state.json()['expected_url'], expected)

        self.api_call.return_value = {'ok': True}
        done = self.client.post('/api/admin/telegram/webhook')
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()['url'], expected)
        self.api_call.return_value = {'url': expected, 'pending_update_count': 0}
        self.assertTrue(self.client.get('/api/admin/telegram/webhook').json()['registered'])

        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': ''}):
            self.assertEqual(self.client.post('/api/admin/telegram/webhook').status_code, 503)
