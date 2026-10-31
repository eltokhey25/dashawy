"""Per-request chat, the technician wallet and the debt ceiling."""

from unittest.mock import patch

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import (
    Customer,
    Message,
    Request,
    Setting,
    Specialty,
    Technician,
    TechnicianWallet,
    User,
    WithdrawalRequest,
)
from .services.payments import add_wallet_transaction

PASSWORD = 'TesterPass123'


class ChatWalletBase(APITestCase):
    def setUp(self):
        cache.clear()
        telegram = patch('core.services.notifications.send_telegram', return_value=True)
        telegram.start()
        self.addCleanup(telegram.stop)
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        self.customer_user = self.make_user('chat-customer', '+201000000601', 'customer')
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل الشات', phone='+201000000601'
        )
        self.tech_user = self.make_user('chat-tech', '+201000000602', 'technician')
        self.technician = Technician.objects.create(
            user=self.tech_user, name='فني الشات', phone='+201000000602',
            specialty='dish_install', availability='available', approval_status='approved',
        )

    def make_user(self, name, phone, role):
        return User.objects.create(
            email=f'{name}@example.com', phone=phone,
            password_hash=hash_password(PASSWORD), role=role,
        )

    def job(self, status_value='assigned'):
        return Request.objects.create(
            customer=self.customer, technician=self.technician,
            specialty='dish_install', description='x', status=status_value,
        )


class ChatTests(ChatWalletBase):
    def test_both_sides_chat_and_unread_counts(self):
        job = self.job()
        self.client.force_authenticate(user=self.customer_user)
        sent = self.client.post(f'/api/messages/{job.pk}', {'body': ' السلام عليكم '}, format='json')
        self.assertEqual(sent.status_code, status.HTTP_201_CREATED)
        self.assertEqual(sent.json()['message']['body'], 'السلام عليكم')
        self.assertEqual(sent.json()['message']['sender_role'], 'customer')

        self.client.force_authenticate(user=self.tech_user)
        unread = self.client.get('/api/messages/unread-count').json()
        self.assertEqual(unread['total'], 1)
        self.assertEqual(unread['byRequest'], [{'request_id': job.pk, 'n': 1}])

        thread = self.client.get(f'/api/messages/{job.pk}').json()
        self.assertEqual(thread['role'], 'technician')
        self.assertTrue(thread['canSend'])
        self.assertEqual([m['body'] for m in thread['messages']], ['السلام عليكم'])
        # Reading the thread marks the customer's message read.
        self.assertEqual(self.client.get('/api/messages/unread-count').json()['total'], 0)

        self.client.post(f'/api/messages/{job.pk}', {'body': 'وعليكم السلام'}, format='json')
        self.client.force_authenticate(user=self.customer_user)
        self.assertEqual(self.client.get('/api/messages/unread-count').json()['total'], 1)
        self.client.post(f'/api/messages/{job.pk}/read')
        self.assertEqual(self.client.get('/api/messages/unread-count').json()['total'], 0)

    def test_strangers_cannot_read_or_write(self):
        job = self.job()
        stranger = self.make_user('chat-stranger', '+201000000603', 'customer')
        Customer.objects.create(user=stranger, name='غريب', phone='+201000000603')
        self.client.force_authenticate(user=stranger)
        self.assertEqual(self.client.get(f'/api/messages/{job.pk}').status_code, 403)
        response = self.client.post(f'/api/messages/{job.pk}', {'body': 'hi'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/api/messages/999999').status_code, 404)

    def test_sending_is_limited_to_active_jobs_and_valid_text(self):
        job = self.job('completed')
        self.client.force_authenticate(user=self.customer_user)
        closed = self.client.post(f'/api/messages/{job.pk}', {'body': 'لسه في حاجة'}, format='json')
        self.assertEqual(closed.status_code, 400)
        self.assertFalse(self.client.get(f'/api/messages/{job.pk}').json()['canSend'])

        active = self.job()
        self.assertEqual(
            self.client.post(f'/api/messages/{active.pk}', {'body': '   '}, format='json').status_code,
            400,
        )
        self.assertEqual(
            self.client.post(f'/api/messages/{active.pk}', {'body': 'x' * 2001}, format='json').status_code,
            400,
        )
        self.assertEqual(Message.objects.count(), 0)

    def test_unassigned_request_has_no_chat(self):
        job = Request.objects.create(
            customer=self.customer, specialty='dish_install', description='x', status='pending'
        )
        self.client.force_authenticate(user=self.customer_user)
        self.assertEqual(self.client.get(f'/api/messages/{job.pk}').status_code, 400)


class WalletTests(ChatWalletBase):
    def test_wallet_shows_debt_and_ledger(self):
        add_wallet_transaction(self.technician.pk, type='debt', amount=60, note='عمولة')
        self.client.force_authenticate(user=self.tech_user)
        body = self.client.get('/api/technician/wallet').json()
        self.assertEqual(body['wallet']['total_debt'], 60)
        self.assertEqual(body['debtLimit'], 300)
        self.assertTrue(body['canReceiveRequests'])
        self.assertEqual(len(body['transactions']), 1)
        self.assertEqual(body['transactions'][0]['type'], 'debt')

    def test_debt_ceiling_stops_new_offers_and_accepts(self):
        Setting.objects.create(key='wallet_debt_limit', value='100')
        self.client.force_authenticate(user=self.customer_user)
        first = self.client.post(
            '/api/customer/requests',
            {'specialty': 'dish_install', 'description': 'طلب أول', 'area_key': 'smouha'},
            format='json',
        ).json()
        self.assertEqual(first['broadcast']['sent_to'], 1)

        add_wallet_transaction(self.technician.pk, type='debt', amount=100)
        self.client.force_authenticate(user=self.tech_user)
        self.assertFalse(self.client.get('/api/technician/wallet').json()['canReceiveRequests'])
        offer = self.client.get('/api/technician/incoming').json()['requests'][0]
        refused = self.client.post(f"/api/technician/broadcasts/{offer['broadcast_id']}/accept")
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.customer_user)
        second = self.client.post(
            '/api/customer/requests',
            {'specialty': 'dish_install', 'description': 'طلب تاني', 'area_key': 'smouha'},
            format='json',
        ).json()
        self.assertEqual(second['broadcast']['sent_to'], 0)

    def test_withdrawal_rules(self):
        self.client.force_authenticate(user=self.tech_user)
        too_much = self.client.post('/api/technician/withdrawals', {'amount': 100}, format='json')
        self.assertEqual(too_much.status_code, 400)

        add_wallet_transaction(self.technician.pk, type='payment', amount=200)
        too_small = self.client.post('/api/technician/withdrawals', {'amount': 20}, format='json')
        self.assertEqual(too_small.status_code, 400)
        made = self.client.post('/api/technician/withdrawals', {'amount': 100}, format='json')
        self.assertEqual(made.status_code, status.HTTP_201_CREATED)
        # The balance only moves when an admin pays it out.
        self.assertEqual(TechnicianWallet.objects.get(pk=self.technician.pk).balance, 200)
        twice = self.client.post('/api/technician/withdrawals', {'amount': 50}, format='json')
        self.assertEqual(twice.status_code, status.HTTP_409_CONFLICT)

        listed = self.client.get('/api/technician/withdrawals').json()
        self.assertEqual(len(listed['withdrawals']), 1)
        self.assertEqual(
            self.client.get('/api/technician/wallet').json()['pendingWithdrawal']['amount'], 100
        )
        self.assertEqual(WithdrawalRequest.objects.count(), 1)

    def test_customers_cannot_open_the_wallet(self):
        self.client.force_authenticate(user=self.customer_user)
        self.assertEqual(self.client.get('/api/technician/wallet').status_code, 403)
