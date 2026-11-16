"""InstaPay payments: receipt upload, automatic checks, settlement and review."""

import io
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from .models import PaymentReceipt, Request, Setting, TechnicianWallet, WalletTransaction, utc_now
from .services import instapay
from .test_completion import CompletionBase

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


def png(seed=b'a'):
    return SimpleUploadedFile('r.png', PNG + seed * 32, content_type='image/png')


def receipt_fields(**overrides):
    fields = {
        'is_transfer_receipt': True,
        'legibility': 'clear',
        'transaction_status': 'successful',
        'amount': '300.00',
        'currency': 'EGP',
        'reference': '123456789012',
        'transferred_at': '',
        'recipient_name': 'Dashawy Platform',
        'recipient_account': 'dashawy@instapay',
        'sender_name': 'Customer',
        'sender_account': '01000000501',
        'tampering_suspected': False,
        'tampering_notes': '',
    }
    fields.update(overrides)
    return fields


def cairo_now_text(delta=timedelta()):
    from zoneinfo import ZoneInfo
    from datetime import datetime

    return (datetime.now(ZoneInfo('Africa/Cairo')) + delta).strftime('%Y-%m-%d %H:%M')


class InstapayBase(CompletionBase):
    def setUp(self):
        super().setUp()
        for key, value in {
            'instapay_enabled': '1',
            'instapay_number': '01012345678',
            'instapay_name': 'Dashawy Platform',
            'instapay_address': 'dashawy@instapay',
            'commission_percent': '20',
        }.items():
            Setting.objects.update_or_create(key=key, defaults={'value': value})
        self.job = Request.objects.create(
            customer=self.customer,
            technician=self.technician,
            specialty='dish_install',
            description='تركيب دش جديد',
            status='in_progress',
            service_price=300,
            commission_percent=20,
            created_at=utc_now() - timedelta(hours=1),
        )
        self.technician.availability = 'busy'
        self.technician.save()

    def upload(self, fields, amount='300', image=None, job=None):
        self.as_customer()
        with patch.object(instapay, 'read_receipt', return_value=fields):
            return self.client.post(
                f'/api/customer/requests/{(job or self.job).pk}/instapay-receipt',
                {'receipt': image or png(), 'amount': amount},
                format='multipart',
            )


class AutomaticVerificationTests(InstapayBase):
    def test_clean_receipt_confirms_payment_and_finishes_the_job(self):
        res = self.upload(receipt_fields(transferred_at=cairo_now_text()))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['receipt']['status'], 'verified')
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'completed')
        self.assertEqual(self.job.payment_status, 'confirmed')
        self.assertEqual(self.job.payment_method, 'instapay')
        self.assertEqual(self.job.payment_reference, '123456789012')
        self.assertEqual(self.job.final_amount, 300)
        self.assertEqual(self.job.commission_amount, 60)
        self.assertEqual(self.job.technician_earning, 240)
        wallet = TechnicianWallet.objects.get(technician=self.technician)
        self.assertEqual(wallet.balance, 240)
        self.assertEqual(wallet.total_debt, 0)
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')

    def test_a_completed_job_is_settled_without_reopening_it(self):
        Request.objects.filter(pk=self.job.pk).update(status='completed', work_notes='تم التركيب')
        res = self.upload(receipt_fields(transferred_at=cairo_now_text()))
        self.assertEqual(res.data['receipt']['status'], 'verified')
        self.job.refresh_from_db()
        self.assertEqual(self.job.work_notes, 'تم التركيب')
        self.assertEqual(
            WalletTransaction.objects.filter(request=self.job, type='payment').count(), 1
        )

    def test_masked_mobile_recipient_waits_for_an_admin(self):
        res = self.upload(
            receipt_fields(recipient_account='010****5678', transferred_at=cairo_now_text())
        )
        self.assertEqual(res.data['receipt']['status'], 'review')
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'in_progress')
        self.assertEqual(self.job.payment_status, 'pending_verification')

    def test_unreadable_by_the_reader_waits_for_an_admin(self):
        res = self.upload(None)
        self.assertEqual(res.data['receipt']['status'], 'review')

    def test_amount_above_the_auto_ceiling_waits_for_an_admin(self):
        Setting.objects.update_or_create(key='instapay_auto_max', defaults={'value': '200'})
        res = self.upload(receipt_fields(transferred_at=cairo_now_text()))
        self.assertEqual(res.data['receipt']['status'], 'review')

    def test_suspected_editing_waits_for_an_admin(self):
        res = self.upload(
            receipt_fields(
                transferred_at=cairo_now_text(),
                tampering_suspected=True,
                tampering_notes='font differs',
            )
        )
        self.assertEqual(res.data['receipt']['status'], 'review')


class RejectionTests(InstapayBase):
    def assertRejected(self, res):
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['receipt']['status'], 'rejected')
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'in_progress')
        self.assertIsNone(self.job.payment_status)
        self.assertFalse(WalletTransaction.objects.filter(request=self.job).exists())

    def test_another_recipient(self):
        self.assertRejected(
            self.upload(
                receipt_fields(recipient_account='someone@instapay', transferred_at=cairo_now_text())
            )
        )

    def test_another_full_mobile_number(self):
        self.assertRejected(
            self.upload(
                receipt_fields(recipient_account='01099999999', transferred_at=cairo_now_text())
            )
        )

    def test_amount_differs_from_what_the_customer_typed(self):
        self.assertRejected(self.upload(receipt_fields(transferred_at=cairo_now_text()), amount='350'))

    def test_failed_transfer(self):
        self.assertRejected(
            self.upload(receipt_fields(transaction_status='failed', transferred_at=cairo_now_text()))
        )

    def test_transfer_older_than_the_order(self):
        self.assertRejected(
            self.upload(receipt_fields(transferred_at=cairo_now_text(-timedelta(days=3))))
        )

    def test_not_a_receipt(self):
        self.assertRejected(self.upload(receipt_fields(is_transfer_receipt=False)))

    def test_refused_image_is_not_kept(self):
        self.upload(receipt_fields(recipient_account='x@instapay', transferred_at=cairo_now_text()))
        self.assertIsNone(PaymentReceipt.objects.get().image_path)

    def test_reference_cannot_pay_two_orders(self):
        self.upload(receipt_fields(transferred_at=cairo_now_text()))
        other = Request.objects.create(
            customer=self.customer,
            technician=self.technician,
            specialty='dish_install',
            description='طلب تاني',
            status='in_progress',
            service_price=300,
            commission_percent=20,
            created_at=utc_now() - timedelta(hours=1),
        )
        res = self.upload(receipt_fields(transferred_at=cairo_now_text()), image=png(b'b'), job=other)
        self.assertEqual(res.data['receipt']['status'], 'rejected')
        self.assertIn('اتستخدم', res.data['receipt']['reason'])

    def test_same_image_twice(self):
        self.upload(None)  # under review
        Request.objects.filter(pk=self.job.pk).update(payment_status=None, payment_method=None)
        PaymentReceipt.objects.update(status='verified', reference='999')
        other = Request.objects.create(
            customer=self.customer,
            technician=self.technician,
            specialty='dish_install',
            description='طلب تالت',
            status='in_progress',
            created_at=utc_now() - timedelta(hours=1),
        )
        res = self.upload(receipt_fields(transferred_at=cairo_now_text()), job=other)
        self.assertEqual(res.data['receipt']['status'], 'rejected')


class GuardTests(InstapayBase):
    def test_disabled_until_the_admin_turns_it_on(self):
        Setting.objects.filter(key='instapay_enabled').update(value='0')
        res = self.upload(receipt_fields())
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.client.get('/api/payments/instapay').data['enabled'])

    def test_not_before_the_job_starts(self):
        Request.objects.filter(pk=self.job.pk).update(status='assigned')
        self.assertEqual(self.upload(receipt_fields()).status_code, 400)

    def test_other_customers_order(self):
        Request.objects.filter(pk=self.job.pk).update(customer=None)
        self.assertEqual(self.upload(receipt_fields()).status_code, 404)

    def test_cash_report_is_blocked_once_paid_by_instapay(self):
        self.upload(receipt_fields(transferred_at=cairo_now_text()))
        self.as_technician()
        res = self.client.post(
            f'/api/technician/requests/{self.job.pk}/report-payment', {'amount': 300}, format='json'
        )
        self.assertIn(res.status_code, (400, 409))
        self.assertEqual(TechnicianWallet.objects.get(technician=self.technician).total_debt, 0)

    def test_receipt_under_review_blocks_a_second_upload(self):
        self.upload(None)
        self.assertEqual(self.upload(receipt_fields(), image=png(b'c')).status_code, 409)


class AdminReviewTests(InstapayBase):
    def setUp(self):
        super().setUp()
        from .auth_backend import hash_password
        from .models import User

        self.admin = User.objects.create(
            email='pay-admin@example.com',
            password_hash=hash_password('AdminPass123'),
            role='admin',
            is_super_admin=True,
        )
        self.upload(None)
        self.receipt = PaymentReceipt.objects.get()

    def test_admin_sees_the_queue_and_the_image(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.get('/api/admin/instapay/receipts')
        self.assertEqual(res.data['review_count'], 1)
        image = self.client.get(res.data['receipts'][0]['image_url'])
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image['Content-Type'], 'image/png')

    def test_approve_settles_and_finishes(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.post(
            f'/api/admin/instapay/receipts/{self.receipt.pk}/approve', {'amount': 300}, format='json'
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'completed')
        self.assertEqual(self.job.payment_status, 'confirmed')
        self.assertEqual(self.job.payment_confirmed_by_id, self.admin.pk)
        self.assertEqual(TechnicianWallet.objects.get(technician=self.technician).balance, 240)
        again = self.client.post(f'/api/admin/instapay/receipts/{self.receipt.pk}/approve')
        self.assertEqual(again.status_code, 409)

    def test_reject_reopens_payment(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.post(
            f'/api/admin/instapay/receipts/{self.receipt.pk}/reject',
            {'note': 'التحويل مش واصل'},
            format='json',
        )
        self.assertEqual(res.status_code, 200)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.payment_status)
        self.assertEqual(self.job.status, 'in_progress')

    def test_customer_cannot_use_admin_routes(self):
        self.as_customer()
        res = self.client.post(f'/api/admin/instapay/receipts/{self.receipt.pk}/approve')
        self.assertEqual(res.status_code, 403)

    def test_refund_takes_the_earning_back(self):
        self.client.force_authenticate(user=self.admin)
        self.client.post(f'/api/admin/instapay/receipts/{self.receipt.pk}/approve')
        res = self.client.post(f'/api/admin/payments/{self.job.pk}/refund')
        self.assertEqual(res.status_code, 200, res.data)
        wallet = TechnicianWallet.objects.get(technician=self.technician)
        self.assertEqual(wallet.balance, 0)
        self.assertEqual(wallet.total_debt, 0)

    def test_stale_settlement_leaves_instapay_review_alone(self):
        from .services.settlement import settle_stale_reports

        Request.objects.filter(pk=self.job.pk).update(
            status='completed', completed_at=utc_now() - timedelta(days=5)
        )
        settle_stale_reports()
        self.job.refresh_from_db()
        self.assertEqual(self.job.payment_status, 'pending_verification')


class MatchingUnitTests(TestCase):
    config = {
        'number': '01012345678',
        'address': 'dashawy@instapay',
        'name': 'محمد أحمد علي',
    }

    def test_recipient_matching(self):
        m = instapay.match_recipient
        self.assertEqual(m('dashawy@instapay', '', self.config), 'match')
        self.assertEqual(m('+20 101 234 5678', '', self.config), 'match')
        self.assertEqual(m('٠١٠١٢٣٤٥٦٧٨', '', self.config), 'match')
        self.assertEqual(m('01099999999', '', self.config), 'mismatch')
        self.assertEqual(m('010*****678', '', self.config), 'partial')
        self.assertEqual(m('011*****678', '', self.config), 'mismatch')
        self.assertEqual(m('', 'محمد ا***', self.config), 'partial')
        self.assertEqual(m('', 'أحمد', self.config), 'partial')
        self.assertEqual(m('', 'سارة', self.config), 'unknown')

    def test_money_and_link_parsing(self):
        self.assertEqual(instapay.parse_money('1,250.50 EGP'), 1250.5)
        self.assertEqual(instapay.parse_money('٣٠٠٫٠٠'), 300)
        self.assertIsNone(instapay.parse_money('—'))
        self.assertEqual(
            instapay.address_from_link('https://ipn.eg/S/dashawy/instapay/7XyZ'),
            'dashawy@instapay',
        )


class TelegramReviewTests(InstapayBase):
    """Receipts left for review reach the admins' Telegram with two buttons."""

    ADMIN_CHAT = 901
    _update_ids = __import__('itertools').count(50000)

    def setUp(self):
        super().setUp()
        import json as _json

        from .auth_backend import hash_password
        from .models import User

        env = patch.dict(
            'os.environ',
            {'TELEGRAM_BOT_TOKEN': '123456:test-token', 'SITE_URL': 'https://dashawy.vercel.app'},
        )
        env.start()
        self.addCleanup(env.stop)
        sender = patch('core.services.telegram.api_call', return_value={'ok': True})
        self.api_call = sender.start()
        self.addCleanup(sender.stop)
        self.admin = User.objects.create(
            email='tg-admin@example.com',
            password_hash=hash_password('AdminPass123'),
            role='admin',
            permissions=_json.dumps(['stats']),
            telegram_chat_id=str(self.ADMIN_CHAT),
        )
        self.no_money_admin = User.objects.create(
            email='tg-admin-2@example.com',
            password_hash=hash_password('AdminPass123'),
            role='admin',
            permissions=_json.dumps(['technicians']),
            telegram_chat_id='902',
        )

    def sent_to(self, chat_id):
        return [
            call.args[1]
            for call in self.api_call.call_args_list
            if call.args[0] in ('sendMessage', 'editMessageText')
            and str(call.args[1]['chat_id']) == str(chat_id)
        ]

    def press(self, data, chat_id=ADMIN_CHAT):
        from .services import telegram

        secret = telegram.webhook_secret()
        res = self.client.post(
            f'/api/telegram/webhook/{secret}',
            data=json.dumps(
                {
                    # Unique per press: the webhook drops a repeated update id.
                    'update_id': next(self._update_ids),
                    'callback_query': {
                        'id': 'cb',
                        'data': data,
                        'message': {'message_id': 5, 'chat': {'id': chat_id, 'type': 'private'}},
                    },
                }
            ),
            content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret,
        )
        self.assertEqual(res.status_code, 200)
        return res.json()['result']

    def review_receipt(self):
        self.upload(
            receipt_fields(
                recipient_account='010****5678',
                sender_name='Ahmed M.',
                transferred_at=cairo_now_text(),
            )
        )
        return PaymentReceipt.objects.get()

    def test_review_message_reaches_money_admins_with_buttons(self):
        receipt = self.review_receipt()
        messages = self.sent_to(self.ADMIN_CHAT)
        self.assertEqual(len(messages), 1)
        text = messages[0]['text']
        self.assertIn(f'طلب #{self.job.pk}', text)
        self.assertIn('300', text)
        self.assertIn('123456789012', text)
        self.assertIn('Ahmed M.', text)
        data = [b.get('callback_data') for row in messages[0]['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn(f'pay:ok:{receipt.pk}', data)
        self.assertIn(f'pay:no:{receipt.pk}', data)
        self.assertEqual(self.sent_to(902), [])

    def test_clean_receipt_does_not_bother_the_admins(self):
        self.upload(receipt_fields(transferred_at=cairo_now_text()))
        self.assertEqual(self.sent_to(self.ADMIN_CHAT), [])

    def test_one_tap_approve_finishes_the_job(self):
        receipt = self.review_receipt()
        self.assertEqual(self.press(f'pay:ok:{receipt.pk}'), 'pay:ok')
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'completed')
        self.assertEqual(self.job.payment_status, 'confirmed')
        self.assertEqual(self.job.payment_confirmed_by_id, self.admin.pk)
        self.assertEqual(TechnicianWallet.objects.get(technician=self.technician).balance, 240)
        self.assertIn('تم تأكيد', self.sent_to(self.ADMIN_CHAT)[-1]['text'])
        # A second tap (or another admin's copy) changes nothing.
        self.assertEqual(self.press(f'pay:ok:{receipt.pk}'), 'error')
        self.assertEqual(
            WalletTransaction.objects.filter(request=self.job, type='payment').count(), 1
        )

    def test_one_tap_reject_reopens_payment(self):
        receipt = self.review_receipt()
        self.assertEqual(self.press(f'pay:no:{receipt.pk}'), 'pay:no')
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, 'rejected')
        self.job.refresh_from_db()
        self.assertIsNone(self.job.payment_status)
        self.assertEqual(self.job.status, 'in_progress')

    def test_admin_without_money_permission_and_customers_cannot_decide(self):
        receipt = self.review_receipt()
        self.assertEqual(self.press(f'pay:ok:{receipt.pk}', chat_id=902), 'forbidden')
        self.customer_user.telegram_chat_id = '555'
        self.customer_user.save()
        self.assertEqual(self.press(f'pay:ok:{receipt.pk}', chat_id=555), 'forbidden')
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, 'review')
