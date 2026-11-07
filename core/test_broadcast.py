"""New orders go to the technicians; the first to accept gets the job."""

from unittest.mock import patch

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import Customer, Request, RequestBroadcast, Specialty, Technician, User

PASSWORD = 'TesterPass123'


class BroadcastFlowTests(APITestCase):
    def setUp(self):
        cache.clear()
        # Never reach the real Telegram API from tests (.env may hold a token).
        telegram = patch('core.services.notifications.send_telegram', return_value=True)
        self.send_telegram = telegram.start()
        self.addCleanup(telegram.stop)
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        self.customer_user = User.objects.create(
            email='bc-customer@example.com',
            phone='+201000000301',
            password_hash=hash_password(PASSWORD),
            role='customer',
            telegram_chat_id='555001',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل البث', phone='+201000000301'
        )
        self.tech_a = self.make_technician('a', 'فني أ')
        self.tech_b = self.make_technician('b', 'فني ب')

    def make_technician(self, suffix, name, specialty='dish_install'):
        user = User.objects.create(
            email=f'bc-tech-{suffix}@example.com',
            phone=f'+20100000040{len(suffix)}{ord(suffix[0]) % 10}',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        Technician.objects.create(
            user=user,
            name=name,
            phone=user.phone,
            specialty=specialty,
            availability='available',
            approval_status='approved',
        )
        return user

    def create_order(self):
        self.client.force_authenticate(user=self.customer_user)
        response = self.client.post(
            '/api/customer/requests',
            {'specialty': 'dish_install', 'description': 'الدش مش شغال', 'area_key': 'smouha'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.json()

    def incoming(self, tech_user):
        self.client.force_authenticate(user=tech_user)
        response = self.client.get('/api/technician/incoming')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()['requests']

    def offer_id(self, tech_user, request_id):
        return next(
            o['broadcast_id'] for o in self.incoming(tech_user) if o['request_id'] == request_id
        )

    def test_order_reaches_every_eligible_technician(self):
        body = self.create_order()
        self.assertEqual(body['message'], 'تم تلقي طلبك وسيتم التواصل معك')
        self.assertEqual(body['broadcast']['sent_to'], 2)

        offers = self.incoming(self.tech_a)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer['request_id'], body['request']['id'])
        self.assertEqual(offer['customer_name'], 'عميل البث')
        # The customer's phone is shared only after accepting.
        self.assertNotIn('customer_phone', offer)

    def test_first_accept_wins_and_notifies_the_customer(self):
        request_id = self.create_order()['request']['id']
        offer_a = self.offer_id(self.tech_a, request_id)
        offer_b = self.offer_id(self.tech_b, request_id)

        self.client.force_authenticate(user=self.tech_a)
        won = self.client.post(f'/api/technician/broadcasts/{offer_a}/accept')
        self.assertEqual(won.status_code, status.HTTP_200_OK)
        self.send_telegram.assert_called_once()
        chat_id, text = self.send_telegram.call_args.args[:2]
        self.assertEqual(chat_id, '555001')
        self.assertIn('فني أ', text)

        self.client.force_authenticate(user=self.tech_b)
        lost = self.client.post(f'/api/technician/broadcasts/{offer_b}/accept')
        self.assertEqual(lost.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.incoming(self.tech_b), [])

        job = Request.objects.get(pk=request_id)
        self.assertEqual(job.status, 'assigned')
        self.assertEqual(job.technician.user, self.tech_a)
        self.assertEqual(RequestBroadcast.objects.get(pk=offer_b).status, 'expired')

        # The customer sees the technician's name on the request.
        self.client.force_authenticate(user=self.customer_user)
        detail = self.client.get(f'/api/customer/requests/{request_id}').json()['request']
        self.assertEqual(detail['technician_name'], 'فني أ')

    def test_decline_hides_the_offer_and_is_not_re_offered(self):
        request_id = self.create_order()['request']['id']
        offer_a = self.offer_id(self.tech_a, request_id)

        self.client.force_authenticate(user=self.tech_a)
        declined = self.client.post(f'/api/technician/broadcasts/{offer_a}/decline')
        self.assertEqual(declined.status_code, status.HTTP_200_OK)
        self.assertEqual(self.incoming(self.tech_a), [])
        # Still open for the other technician.
        self.assertEqual(len(self.incoming(self.tech_b)), 1)
        self.assertEqual(Request.objects.get(pk=request_id).status, 'pending')

        from .services.broadcast import broadcast_request

        broadcast_request(request_id)
        self.assertEqual(self.incoming(self.tech_a), [])

    def test_busy_technician_cannot_take_another_job(self):
        first = self.create_order()['request']['id']
        second = self.create_order()['request']['id']
        self.client.force_authenticate(user=self.tech_a)
        self.client.post(f'/api/technician/broadcasts/{self.offer_id(self.tech_a, first)}/accept')

        # Offers made before the first accept are still listed, but taking one
        # while busy is refused.
        Technician.objects.filter(user=self.tech_a).update(availability='busy')
        offer = RequestBroadcast.objects.get(request_id=second, technician__user=self.tech_a)
        self.client.force_authenticate(user=self.tech_a)
        response = self.client.post(f'/api/technician/broadcasts/{offer.pk}/accept')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_cancel_closes_offers_and_frees_the_technician(self):
        request_id = self.create_order()['request']['id']
        offer_b = self.offer_id(self.tech_b, request_id)
        self.client.force_authenticate(user=self.tech_a)
        self.client.post(
            f'/api/technician/broadcasts/{self.offer_id(self.tech_a, request_id)}/accept'
        )

        self.client.force_authenticate(user=self.customer_user)
        cancelled = self.client.post(f'/api/customer/requests/{request_id}/cancel')
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK)
        self.assertEqual(cancelled.json()['request']['status'], 'cancelled')
        self.assertEqual(Technician.objects.get(user=self.tech_a).availability, 'available')

        self.client.force_authenticate(user=self.tech_b)
        late = self.client.post(f'/api/technician/broadcasts/{offer_b}/accept')
        self.assertEqual(late.status_code, status.HTTP_409_CONFLICT)

    def test_customer_cannot_cancel_someone_elses_order(self):
        request_id = self.create_order()['request']['id']
        other = User.objects.create(
            email='bc-other@example.com', phone='+201000000399', password_hash='x', role='customer'
        )
        Customer.objects.create(user=other, name='Other', phone='+201000000399')
        self.client.force_authenticate(user=other)
        response = self.client.post(f'/api/customer/requests/{request_id}/cancel')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_order_form_also_goes_to_technicians(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(
            '/api/guest/requests',
            {
                'name': 'عميل جديد',
                'phone': '01000000398',
                'area_key': 'smouha',
                'specialty': 'dish_install',
                'description': 'محتاج تركيب دش',
                'accept_terms': True,
                'password': 'OrderPass123',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body['message'], 'تم تلقي طلبك وسيتم التواصل معك')
        self.assertEqual(body['broadcast']['sent_to'], 2)
        self.assertEqual(body['request']['status'], 'pending')


class OfferPrivacyTests(BroadcastFlowTests):
    def test_offer_shows_the_area_but_not_the_street(self):
        self.client.force_authenticate(user=self.customer_user)
        request_id = self.client.post(
            '/api/customer/requests',
            {
                'specialty': 'dish_install',
                'description': 'الدش مش شغال',
                'area_key': 'smouha',
                'location_text': 'شارع فوزي معاذ، عمارة 12',
            },
            format='json',
        ).json()['request']['id']
        offer = next(o for o in self.incoming(self.tech_a) if o['request_id'] == request_id)
        self.assertEqual(offer['location_text'], 'سموحة')
        self.assertEqual(offer['customer_area'], 'سموحة')
        self.assertNotIn('فوزي', str(offer))

        # The technician who accepts gets the full address.
        self.client.force_authenticate(user=self.tech_a)
        accepted = self.client.post(f"/api/technician/broadcasts/{offer['broadcast_id']}/accept").json()
        self.assertIn('فوزي', accepted['request']['location_text'])


class AtomicAcceptTests(BroadcastFlowTests):
    def test_accept_claims_the_technician_row_atomically(self):
        first = self.create_order()['request']['id']
        second = self.create_order()['request']['id']
        offer_a1 = self.offer_id(self.tech_a, first)
        offer_a2 = self.offer_id(self.tech_a, second)
        self.client.force_authenticate(user=self.tech_a)
        self.assertEqual(self.client.post(f'/api/technician/broadcasts/{offer_a1}/accept').status_code, 200)
        # The second accept is refused even when the cached availability was
        # read before the first one landed (the UPDATE is the arbiter).
        with patch('core.services.broadcast.over_debt_limit_ids', return_value=set()):
            denied = self.client.post(f'/api/technician/broadcasts/{offer_a2}/accept')
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(Request.objects.get(pk=second).status, 'pending')
        self.assertEqual(Technician.objects.get(user=self.tech_a).availability, 'busy')
