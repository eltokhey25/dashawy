"""Behaviour added after the November 2026 QA review.

* "اطلب منه": a request with ``technician_id`` is offered to that technician
  alone first, to everyone once they decline or go quiet.
* ``/api/requests/<id>/photo`` shows the order photo to the parties only.
* Approving a technician offers them the orders still waiting for one.
"""

import base64
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import Customer, Request, RequestBroadcast, Specialty, Technician, User, utc_now
from .services.broadcast import PREFERRED_OFFER_MINUTES

PASSWORD = 'TesterPass123'
PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)


class QaFixesBase(APITestCase):
    def setUp(self):
        cache.clear()
        telegram = patch('core.services.notifications.send_telegram', return_value=True)
        telegram.start()
        self.addCleanup(telegram.stop)
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        self.customer_user = User.objects.create(
            email='qa-customer@example.com',
            phone='+201000000501',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل الاختبار', phone='+201000000501'
        )
        self.tech_a = self.make_technician('a', 'فني أ')
        self.tech_b = self.make_technician('b', 'فني ب')
        self.admin = User.objects.create(
            email='qa-admin@example.com',
            password_hash=hash_password(PASSWORD),
            role='admin',
            is_super_admin=True,
        )

    def make_technician(self, suffix, name, **fields):
        user = User.objects.create(
            email=f'qa-tech-{suffix}@example.com',
            phone=f'+2010000006{ord(suffix) % 100:02d}',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        fields.setdefault('availability', 'available')
        fields.setdefault('approval_status', 'approved')
        Technician.objects.create(
            user=user, name=name, phone=user.phone, specialty='dish_general', **fields
        )
        return user

    def tech_id(self, user):
        return Technician.objects.get(user=user).pk

    def create_order(self, **extra):
        self.client.force_authenticate(user=self.customer_user)
        body = {'specialty': 'dish_install', 'description': 'الدش مش شغال', 'area_key': 'smouha'}
        body.update(extra)
        return self.client.post('/api/customer/requests', body, format='json')

    def incoming(self, tech_user):
        self.client.force_authenticate(user=tech_user)
        response = self.client.get('/api/technician/incoming')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()['requests']


class PreferredTechnicianTests(QaFixesBase):
    def test_chosen_technician_is_offered_first(self):
        response = self.create_order(technician_id=self.tech_id(self.tech_a))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertTrue(body['preferred_technician']['offered'])
        self.assertEqual(body['broadcast']['sent_to'], 1)
        self.assertEqual(body['request']['preferred_technician_name'], 'فني أ')

        offers = self.incoming(self.tech_a)
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0]['preferred'])
        self.assertEqual(self.incoming(self.tech_b), [])

    def test_decline_by_the_chosen_technician_opens_it_to_everyone(self):
        request_id = self.create_order(technician_id=self.tech_id(self.tech_a)).json()['request']['id']
        offer = self.incoming(self.tech_a)[0]['broadcast_id']
        declined = self.client.post(f'/api/technician/broadcasts/{offer}/decline')
        self.assertEqual(declined.status_code, status.HTTP_200_OK)

        self.assertEqual(self.incoming(self.tech_a), [])
        self.assertEqual([o['request_id'] for o in self.incoming(self.tech_b)], [request_id])
        self.assertIsNone(Request.objects.get(pk=request_id).preferred_technician_id)

    def test_a_quiet_chosen_technician_is_bypassed_after_the_grace_period(self):
        request_id = self.create_order(technician_id=self.tech_id(self.tech_a)).json()['request']['id']
        self.assertEqual(self.incoming(self.tech_b), [])

        RequestBroadcast.objects.filter(request_id=request_id).update(
            sent_at=utc_now() - timedelta(minutes=PREFERRED_OFFER_MINUTES + 1)
        )
        # The dashboard poll of any technician widens the stale offer.
        self.assertEqual([o['request_id'] for o in self.incoming(self.tech_b)], [request_id])
        self.assertIsNone(Request.objects.get(pk=request_id).preferred_technician_id)
        # The chosen technician keeps their offer too.
        self.assertEqual(len(self.incoming(self.tech_a)), 1)

    def test_an_unavailable_choice_goes_to_everyone_at_once(self):
        Technician.objects.filter(user=self.tech_a).update(availability='busy')
        body = self.create_order(technician_id=self.tech_id(self.tech_a)).json()
        self.assertFalse(body['preferred_technician']['offered'])
        self.assertEqual(body['broadcast']['sent_to'], 1)
        self.assertEqual(len(self.incoming(self.tech_b)), 1)

    def test_an_unapproved_choice_is_refused(self):
        pending = self.make_technician('p', 'فني معلق', approval_status='pending')
        response = self.create_order(technician_id=self.tech_id(pending))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Request.objects.count(), 0)


class OrderPhotoTests(QaFixesBase):
    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix='dashawy-qa-media-')
        self.addCleanup(shutil.rmtree, self.media_root, True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_authenticate(user=self.customer_user)
        photo = SimpleUploadedFile('problem.png', PNG_BYTES, content_type='image/png')
        response = self.client.post(
            '/api/guest/requests',
            {
                'name': 'عميل الاختبار',
                'phone': '01000000501',
                'area_key': 'smouha',
                'specialty': 'dish_install',
                'description': 'الطبق وقع',
                'accept_terms': True,
                'photo': photo,
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.request_id = response.json()['request']['id']
        self.url = f'/api/requests/{self.request_id}/photo'

    def test_customer_sees_their_own_order_photo(self):
        self.client.force_authenticate(user=self.customer_user)
        listed = self.client.get('/api/customer/requests').json()['requests'][0]
        self.assertEqual(listed['photo_url'], self.url)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'image/png')

    def test_offered_technician_and_admin_see_it_but_a_stranger_does_not(self):
        offers = self.incoming(self.tech_a)
        self.assertEqual(offers[0]['photo_url'], self.url)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

        self.client.force_authenticate(user=self.admin)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

        other = User.objects.create(
            email='other@example.com', phone='+201000000777',
            password_hash=hash_password(PASSWORD), role='customer',
        )
        Customer.objects.create(user=other, name='عميل تاني', phone='+201000000777')
        self.client.force_authenticate(user=other)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)


class ApprovalOffersOpenOrdersTests(QaFixesBase):
    def test_newly_approved_technician_sees_the_waiting_orders(self):
        request_id = self.create_order().json()['request']['id']
        newcomer = self.make_technician('n', 'فني جديد', approval_status='pending',
                                        national_id_path='/uploads/technicians/x/id.png',
                                        criminal_record_path='/uploads/technicians/x/cr.png')
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(f'/api/admin/technicians/{self.tech_id(newcomer)}/approve')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()['offered_requests'], [request_id])
        self.assertEqual([o['request_id'] for o in self.incoming(newcomer)], [request_id])
