"""Technician profile and gallery, public technician pages, Web Push,
privacy requests and the Telegram status endpoint."""

import shutil
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import (
    Customer,
    DataSubjectRequest,
    PushSubscription,
    Request,
    Review,
    Specialty,
    Technician,
    TechnicianPortfolio,
    User,
)
from .tests import PNG_BYTES

PASSWORD = 'TesterPass123'
SUBSCRIPTION = {
    'endpoint': 'https://fcm.googleapis.com/fcm/send/abc',
    'keys': {'p256dh': 'BPUBKEY', 'auth': 'AUTHKEY'},
}


def png(name='work.png'):
    return SimpleUploadedFile(name, PNG_BYTES, content_type='image/png')


class ProfileBase(APITestCase):
    def setUp(self):
        cache.clear()
        media = tempfile.mkdtemp(prefix='dashawy-profile-media-')
        self.addCleanup(shutil.rmtree, media, True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        telegram = patch('core.services.notifications.send_telegram', return_value=True)
        telegram.start()
        self.addCleanup(telegram.stop)
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        self.tech_user = User.objects.create(
            email='profile-tech@example.com', phone='+201000000801',
            password_hash=hash_password(PASSWORD), role='technician',
        )
        self.technician = Technician.objects.create(
            user=self.tech_user, name='فني البروفايل', phone='+201000000801',
            specialty='dish_install', availability='available', approval_status='approved',
            location_text='سموحة',
        )
        self.customer_user = User.objects.create(
            email='profile-customer@example.com', phone='+201000000802',
            password_hash=hash_password(PASSWORD), role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل البروفايل', phone='+201000000802'
        )


class TechnicianProfileTests(ProfileBase):
    def test_bio_and_experience(self):
        self.client.force_authenticate(user=self.tech_user)
        saved = self.client.put(
            '/api/technician/profile-details',
            {'bio': 'فني دش من 10 سنين', 'years_experience': 10},
            format='json',
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()['profile']['years_experience'], 10)
        details = self.client.get('/api/technician/profile-details').json()
        self.assertEqual(details['profile']['bio'], 'فني دش من 10 سنين')
        self.assertEqual(details['technician']['name'], 'فني البروفايل')
        bad = self.client.put('/api/technician/profile-details', {'years_experience': 99}, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_profile_image_is_public_and_replaced(self):
        self.client.force_authenticate(user=self.tech_user)
        first = self.client.post('/api/technician/profile-image', {'profile_image': png()}, format='multipart')
        self.assertEqual(first.status_code, 200)
        path = first.json()['profile']['profile_image_path']
        self.assertTrue(path.startswith(f'/uploads/technicians/profiles/tech-{self.technician.pk}/'))
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(path).status_code, 200)

        self.client.force_authenticate(user=self.tech_user)
        second = self.client.post('/api/technician/profile-image', {'profile_image': png()}, format='multipart')
        self.assertNotEqual(second.json()['profile']['profile_image_path'], path)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_non_images_are_rejected(self):
        self.client.force_authenticate(user=self.tech_user)
        fake = SimpleUploadedFile('x.png', b'<svg xmlns="http://www.w3.org/2000/svg"/>', content_type='image/png')
        response = self.client.post('/api/technician/profile-image', {'profile_image': fake}, format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_portfolio_keeps_the_newest_five(self):
        self.client.force_authenticate(user=self.tech_user)
        added = self.client.post(
            '/api/technician/portfolio',
            {'portfolio_images': [png('a.png'), png('b.png'), png('c.png')], 'caption': 'تركيب'},
            format='multipart',
        )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(len(added.json()['portfolio']), 3)
        more = self.client.post(
            '/api/technician/portfolio',
            {'portfolio_images': [png('d.png'), png('e.png'), png('f.png')]},
            format='multipart',
        )
        self.assertEqual(len(more.json()['portfolio']), 5)

        item = TechnicianPortfolio.objects.filter(technician=self.technician).first()
        self.assertEqual(self.client.delete(f'/api/technician/portfolio/{item.pk}').status_code, 200)
        self.assertEqual(TechnicianPortfolio.objects.filter(technician=self.technician).count(), 4)

    def test_customers_cannot_edit_a_profile(self):
        self.client.force_authenticate(user=self.customer_user)
        self.assertEqual(self.client.get('/api/technician/profile-details').status_code, 403)


class PublicTechnicianTests(ProfileBase):
    def test_public_pages_show_only_approved_and_visible_reviews(self):
        job = Request.objects.create(
            customer=self.customer, technician=self.technician,
            specialty='dish_install', description='x', status='completed',
        )
        Review.objects.create(request=job, customer=self.customer, technician=self.technician, rating=5, comment='ممتاز')
        hidden_job = Request.objects.create(
            customer=self.customer, technician=self.technician,
            specialty='dish_install', description='y', status='completed',
        )
        Review.objects.create(
            request=hidden_job, customer=self.customer, technician=self.technician,
            rating=1, comment='مخفي', hidden=True,
        )
        Technician.objects.create(
            user=User.objects.create(email='pending-pub@example.com', password_hash='x', role='technician'),
            name='فني مستني', phone='+201000000803', specialty='dish_install', approval_status='pending',
        )

        listed = self.client.get('/api/public/technicians').json()['technicians']
        self.assertEqual([t['name'] for t in listed], ['فني البروفايل'])
        self.assertEqual(listed[0]['avg_rating'], 5)
        self.assertNotIn('phone', listed[0])
        self.assertEqual(
            len(self.client.get('/api/public/technicians?area=smouha').json()['technicians']), 1
        )

        detail = self.client.get(f'/api/public/technicians/{self.technician.pk}').json()
        self.assertEqual([r['comment'] for r in detail['reviews']], ['ممتاز'])
        self.assertEqual(detail['stats']['completed_count'], 2)
        self.assertNotIn('phone', detail['technician'])

        pending = Technician.objects.get(name='فني مستني')
        self.assertEqual(self.client.get(f'/api/public/technicians/{pending.pk}').status_code, 404)


@override_settings()
class PushTests(ProfileBase):
    def setUp(self):
        super().setUp()
        keys = patch.dict(
            'os.environ',
            {'VAPID_PUBLIC_KEY': 'PUBLIC', 'VAPID_PRIVATE_KEY': 'PRIVATE', 'VAPID_SUBJECT': 'mailto:a@b.c'},
        )
        keys.start()
        self.addCleanup(keys.stop)
        self.webpush = patch('pywebpush.webpush')
        self.sent = self.webpush.start()
        self.addCleanup(self.webpush.stop)

    def test_public_key_and_subscription(self):
        self.assertEqual(
            self.client.get('/api/notifications/vapid-public-key').json(),
            {'publicKey': 'PUBLIC', 'enabled': True},
        )
        self.client.force_authenticate(user=self.customer_user)
        self.assertEqual(
            self.client.post('/api/notifications/subscribe', {'subscription': SUBSCRIPTION}, format='json').status_code,
            200,
        )
        self.assertEqual(PushSubscription.objects.get().user_id, self.customer_user.pk)
        bad = self.client.post('/api/notifications/subscribe', {'subscription': {'endpoint': 'x'}}, format='json')
        self.assertEqual(bad.status_code, 400)
        removed = self.client.post(
            '/api/notifications/unsubscribe', {'endpoint': SUBSCRIPTION['endpoint']}, format='json'
        )
        self.assertTrue(removed.json()['removed'])

    def test_new_order_and_accept_send_pushes(self):
        PushSubscription.objects.create(user=self.tech_user, endpoint='https://fcm.googleapis.com/fcm/send/tech', p256dh='k', auth='a')
        PushSubscription.objects.create(user=self.customer_user, endpoint='https://fcm.googleapis.com/fcm/send/cust', p256dh='k', auth='a')

        self.client.force_authenticate(user=self.customer_user)
        request_id = self.client.post(
            '/api/customer/requests',
            {'specialty': 'dish_install', 'description': 'محتاج فني', 'area_key': 'smouha'},
            format='json',
        ).json()['request']['id']
        tech_push = self.sent.call_args_list[-1].kwargs
        self.assertEqual(tech_push['subscription_info']['endpoint'], 'https://fcm.googleapis.com/fcm/send/tech')
        self.assertIn('طلب جديد', tech_push['data'])

        self.client.force_authenticate(user=self.tech_user)
        offer = self.client.get('/api/technician/incoming').json()['requests'][0]
        self.client.post(f"/api/technician/broadcasts/{offer['broadcast_id']}/accept")
        customer_push = self.sent.call_args_list[-1].kwargs
        self.assertEqual(customer_push['subscription_info']['endpoint'], 'https://fcm.googleapis.com/fcm/send/cust')
        self.assertIn('فني البروفايل', customer_push['data'])
        self.assertTrue(Request.objects.filter(pk=request_id, status='assigned').exists())

    def test_gone_subscriptions_are_removed(self):
        from pywebpush import WebPushException

        PushSubscription.objects.create(user=self.customer_user, endpoint='https://fcm.googleapis.com/fcm/send/old', p256dh='k', auth='a')
        gone = type('Resp', (), {'status_code': 410})()
        self.sent.side_effect = WebPushException('gone', response=gone)
        from .services.push import send_to_user

        self.assertEqual(send_to_user(self.customer_user.pk, {'title': 'x'}), {'sent': 0, 'failed': 1})
        self.assertFalse(PushSubscription.objects.exists())


class PrivacyAndTelegramTests(ProfileBase):
    def test_data_subject_request(self):
        self.client.force_authenticate(user=self.customer_user)
        made = self.client.post('/api/data-subject/request', {'request_type': 'erasure'}, format='json')
        self.assertEqual(made.status_code, 201)
        self.assertEqual(DataSubjectRequest.objects.get().request_type, 'erasure')
        bad = self.client.post('/api/data-subject/request', {'request_type': 'nope'}, format='json')
        self.assertEqual(bad.status_code, 400)
        mine = self.client.get('/api/data-subject/my-requests').json()['requests']
        self.assertEqual(len(mine), 1)

    def test_telegram_status(self):
        User.objects.filter(pk=self.customer_user.pk).update(telegram_chat_id='123')
        self.customer_user.refresh_from_db()
        self.client.force_authenticate(user=self.customer_user)
        self.assertTrue(self.client.get('/api/auth/telegram-link').json()['linked'])


class PushEndpointTests(ProfileBase):
    def test_only_browser_push_services_are_accepted(self):
        self.client.force_authenticate(user=self.customer_user)
        for endpoint in (
            'http://fcm.googleapis.com/fcm/send/abc',
            'https://evil.example/collect',
            'https://169.254.169.254/latest/meta-data',
            'https://fcm.googleapis.com.evil.example/x',
        ):
            bad = self.client.post(
                '/api/notifications/subscribe',
                {'subscription': {'endpoint': endpoint, 'keys': {'p256dh': 'k', 'auth': 'a'}}},
                format='json',
            )
            self.assertEqual(bad.status_code, 400, endpoint)
        for endpoint in (
            'https://fcm.googleapis.com/fcm/send/abc',
            'https://updates.push.services.mozilla.com/wpush/v2/x',
            'https://web.push.apple.com/QAbc',
            'https://wns2-par02p.notify.windows.com/w/?token=x',
        ):
            ok = self.client.post(
                '/api/notifications/subscribe',
                {'subscription': {'endpoint': endpoint, 'keys': {'p256dh': 'k', 'auth': 'a'}}},
                format='json',
            )
            self.assertEqual(ok.status_code, 200, endpoint)
        self.assertEqual(PushSubscription.objects.count(), 4)
