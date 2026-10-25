"""Tests for the October 2026 improvement phases.

Coverage highlights:

* account-free ordering, token secrecy, honeypot and rate limits;
* guest-order linking only with both the token and a matching phone;
* required terms/privacy consent on registration and guest ordering;
* truthful public stats/reviews (no invented numbers);
* private technician documents (admin-only, never publicly served);
* security headers, SEO/legal pages, manifest/robots/sitemap and SITE_URL.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import (
    Consent,
    Customer,
    GuestSubmissionLog,
    Request,
    Review,
    ServicePricing,
    Specialty,
    Technician,
    User,
    utc_now,
)
from .site_config import clear_site_config_cache

PASSWORD = 'TesterPass123'

PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


def guest_payload(**overrides):
    body = {
        'name': 'عميل من غير حساب',
        'phone': '01000000001',
        'area_key': 'smouha',
        'specialty': 'dish_install',
        'description': 'الطبق وقع ومحتاج تركيب جديد',
        'accept_terms': True,
        'policy_version': 'v1.0',
    }
    body.update(overrides)
    return body


class GuestOrderTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp(prefix='dashawy-guest-media-')
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def create_guest(self, **overrides):
        return self.client.post(
            '/api/guest/requests', guest_payload(**overrides), format='json'
        )

    def test_consent_is_required(self):
        body = guest_payload()
        body.pop('accept_terms')
        response = self.client.post('/api/guest/requests', body, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('الشروط', response.json()['error'])
        self.assertEqual(Request.objects.count(), 0)

    def test_guest_order_is_created_with_hashed_token_only(self):
        response = self.create_guest()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        token = body['tracking_token']
        self.assertGreaterEqual(len(token), 40)
        self.assertTrue(body['tracking_url'].endswith('/track.html?token=' + token))

        job = Request.objects.get(pk=body['request']['id'])
        self.assertIsNone(job.customer_id)
        self.assertEqual(job.guest_name, 'عميل من غير حساب')
        self.assertEqual(job.guest_phone, '01000000001')
        self.assertNotEqual(job.guest_token_hash, token)
        self.assertEqual(len(job.guest_token_hash), 64)
        self.assertNotIn(token, job.guest_token_hash)
        self.assertTrue(GuestSubmissionLog.objects.exists())

    def test_tracking_requires_the_token_and_never_leaks_it(self):
        created = self.create_guest().json()
        token = created['tracking_token']

        detail = self.client.get('/api/guest/requests/' + token)
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        payload = detail.json()['request']
        self.assertEqual(payload['reference'], '#' + str(created['request']['id']))
        self.assertNotIn('guest_token_hash', payload)
        self.assertNotIn('customer_phone', payload)
        self.assertEqual(payload['is_guest'], True)

        missing = self.client.get('/api/guest/requests/not-a-real-token-1234567890')
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)
        short = self.client.get('/api/guest/requests/short')
        self.assertEqual(short.status_code, status.HTTP_404_NOT_FOUND)

    def test_honeypot_submission_is_rejected(self):
        response = self.create_guest(website='https://spam.example')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Request.objects.count(), 0)

    def test_invalid_phone_is_rejected(self):
        response = self.create_guest(phone='12345')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_per_ip_rate_limit(self):
        for index in range(5):
            response = self.create_guest(phone='010000000%02d' % index)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        blocked = self.create_guest(phone='01000000099')
        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(GuestSubmissionLog.objects.count(), 5)

    def test_per_phone_rate_limit_across_ips(self):
        for index in range(5):
            response = self.client.post(
                '/api/guest/requests',
                guest_payload(phone='01000000111'),
                format='json',
                REMOTE_ADDR='10.0.0.%d' % index,
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        blocked = self.client.post(
            '/api/guest/requests',
            guest_payload(phone='01000000111'),
            format='json',
            REMOTE_ADDR='10.0.0.99',
        )
        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_guest_can_cancel_and_technician_is_released(self):
        tech_user = User.objects.create(
            email='guest-tech@example.com',
            phone='01000000222',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        technician = Technician.objects.create(
            user=tech_user,
            name='فني متاح',
            phone='01000000222',
            specialty='dish_install',
            availability='available',
            approval_status='approved',
            latitude=31.2156,
            longitude=29.9553,
        )

        created = self.create_guest()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        body = created.json()
        self.assertTrue(body['assigned'])
        technician.refresh_from_db()
        self.assertEqual(technician.availability, 'busy')

        cancelled = self.client.post(
            '/api/guest/requests/' + body['tracking_token'] + '/cancel'
        )
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK)
        self.assertEqual(cancelled.json()['request']['status'], 'cancelled')
        technician.refresh_from_db()
        self.assertEqual(technician.availability, 'available')

    def test_guest_order_photo_is_private(self):
        photo = SimpleUploadedFile('problem.png', PNG_BYTES, content_type='image/png')
        response = self.client.post(
            '/api/guest/requests',
            guest_payload(photo=photo),
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        token = body['tracking_token']
        self.assertTrue(body['request']['has_photo'])

        detail = self.client.get('/api/guest/requests/' + token)
        photo_url = detail.json()['request']['photo_url']
        self.assertTrue(photo_url.startswith('/api/guest/requests/'))
        image = self.client.get(photo_url)
        self.assertEqual(image.status_code, status.HTTP_200_OK)
        self.assertTrue(image['Content-Type'].startswith('image/'))
        self.assertEqual(image['Cache-Control'], 'private, max-age=300')


class GuestLinkingTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create(
            email='link-customer@example.com',
            phone='01000000001',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.user,
            name='عميل مرتبط',
            phone='01000000001',
        )

    def create_guest(self, phone='01000000001'):
        return self.client.post(
            '/api/guest/requests', guest_payload(phone=phone), format='json'
        ).json()

    def test_link_requires_auth(self):
        created = self.create_guest()
        response = self.client.post(
            '/api/customer/guest-orders/link',
            {'token': created['tracking_token']},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_link_rejects_a_different_phone(self):
        created = self.create_guest(phone='01000000009')
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            '/api/customer/guest-orders/link',
            {'token': created['tracking_token']},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_link_with_token_and_matching_phone(self):
        created = self.create_guest()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            '/api/customer/guest-orders/link',
            {'token': created['tracking_token']},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = Request.objects.get(pk=created['request']['id'])
        self.assertEqual(job.customer_id, self.customer.pk)
        self.assertIsNone(job.guest_token_hash)
        self.assertIsNotNone(job.guest_linked_at)

        again = self.client.post(
            '/api/customer/guest-orders/link',
            {'token': created['tracking_token']},
            format='json',
        )
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)

    def test_completing_a_profile_does_not_auto_link_guest_orders(self):
        created = self.create_guest()
        google_user = User.objects.create(
            email='google-customer@example.com',
            phone=None,
            password_hash='!google-oauth-no-password!',
            role='customer',
            google_sub='google-test-sub',
        )
        Customer.objects.create(user=google_user, name='Google Customer')
        self.client.force_authenticate(user=google_user)

        missing_consent = self.client.post(
            '/api/auth/complete-profile',
            {'name': 'Google Customer', 'phone': '01000000001', 'area_key': 'smouha'},
            format='json',
        )
        self.assertEqual(missing_consent.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            '/api/auth/complete-profile',
            {
                'name': 'Google Customer',
                'phone': '01000000001',
                'area_key': 'smouha',
                'accept_terms': True,
                'policy_version': 'v1.0',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['guest_orders_count'], 1)
        job = Request.objects.get(pk=created['request']['id'])
        # Only the token endpoint may link the order.
        self.assertIsNone(job.customer_id)
        self.assertTrue(
            Consent.objects.filter(
                user=google_user, consent_type='terms_and_privacy'
            ).exists()
        )


class ConsentTests(APITestCase):
    def setUp(self):
        cache.clear()

    def registration_body(self, **overrides):
        body = {
            'name': 'عميل جديد',
            'phone': '01000000333',
            'email': 'new-customer@example.com',
            'password': 'StrongPass123',
            'area_key': 'smouha',
            'accept_terms': True,
            'policy_version': 'v1.0',
        }
        body.update(overrides)
        return body

    def test_registration_requires_terms(self):
        body = self.registration_body()
        body.pop('accept_terms')
        response = self.client.post('/api/auth/register', body, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email='new-customer@example.com').exists())

    def test_registration_records_consent_with_version(self):
        response = self.client.post(
            '/api/auth/register', self.registration_body(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email='new-customer@example.com')
        consent = Consent.objects.get(user=user)
        self.assertEqual(consent.consent_type, 'terms_and_privacy')
        self.assertEqual(consent.policy_version, 'v1.0')
        self.assertIsNotNone(consent.ip_address)


class PublicDataTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_stats_are_hidden_below_threshold_and_real_above_it(self):
        small = self.client.get('/api/public/stats').json()
        self.assertFalse(small['visible'])
        self.assertTrue(all(not item['visible'] for item in small['items']))

        for index in range(12):
            Request.objects.create(
                specialty='dish_install',
                description='طلب مكتمل %d' % index,
                status='completed',
                completed_at=utc_now(),
            )
        for index in range(3):
            user = User.objects.create(
                email='approved-tech-%d@example.com' % index,
                phone='0100000100%d' % index,
                password_hash=hash_password(PASSWORD),
                role='technician',
            )
            Technician.objects.create(
                user=user,
                name='فني %d' % index,
                phone='0100000100%d' % index,
                specialty='dish_install',
                approval_status='approved',
                availability='available',
            )

        stats = self.client.get('/api/public/stats').json()
        self.assertTrue(stats['visible'])
        by_key = {item['key']: item for item in stats['items']}
        self.assertTrue(by_key['completed']['visible'])
        self.assertEqual(by_key['completed']['value'], 12)
        self.assertTrue(by_key['technicians']['visible'])
        self.assertEqual(by_key['technicians']['value'], 3)

    def test_reviews_require_a_completed_request_and_a_comment(self):
        customer_user = User.objects.create(
            email='review-customer@example.com',
            phone='01000002001',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        customer = Customer.objects.create(
            user=customer_user, name='أحمد محمد', phone='01000002001'
        )
        tech_user = User.objects.create(
            email='review-tech@example.com',
            phone='01000002002',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        technician = Technician.objects.create(
            user=tech_user,
            name='فني المراجعة',
            phone='01000002002',
            specialty='dish_install',
            approval_status='approved',
        )

        completed = Request.objects.create(
            customer=customer,
            technician=technician,
            specialty='dish_install',
            description='تم',
            status='completed',
            location_text='سموحة — شارع الاختبار',
            completed_at=utc_now(),
        )
        pending = Request.objects.create(
            customer=customer,
            specialty='dish_install',
            description='لسه',
            status='pending',
        )
        Review.objects.create(
            request=completed,
            customer=customer,
            technician=technician,
            rating=5,
            comment='شغل ممتاز',
        )
        Review.objects.create(
            request=pending,
            customer=customer,
            technician=technician,
            rating=5,
            comment='مش المفروض يظهر',
        )

        data = self.client.get('/api/public/reviews').json()
        self.assertEqual(data['count'], 1)
        review = data['reviews'][0]
        self.assertEqual(review['comment'], 'شغل ممتاز')
        self.assertEqual(review['reviewer_name'], 'أحمد م.')
        self.assertEqual(review['area'], 'سموحة')

    def test_service_prices_come_from_the_database(self):
        ServicePricing.objects.create(
            service_key='dish_install', price=150, is_active=True
        )
        data = self.client.get('/api/specialties').json()
        rows = {row['key']: row for row in data['specialties']}
        self.assertEqual(rows['dish_install']['price'], 150)
        self.assertIsNone(rows['dish_repair']['price'])


class PricingTests(APITestCase):
    def setUp(self):
        cache.clear()
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        ServicePricing.objects.create(
            service_key='dish_install', price=200, is_active=True
        )
        user = User.objects.create(
            email='pricing@example.com',
            phone='01000003001',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        Customer.objects.create(user=user, name='عميل السعر', phone='01000003001')
        self.client.force_authenticate(user=user)

    def test_new_request_freezes_price_and_commission(self):
        response = self.client.post(
            '/api/customer/requests',
            {
                'specialty': 'dish_install',
                'description': 'تركيب طبق',
                'area_key': 'smouha',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        job = Request.objects.get(pk=response.json()['request']['id'])
        self.assertEqual(job.service_price, 200)
        commission = float(settings.COMMISSION_RATE or 20)
        self.assertAlmostEqual(job.commission_percent, commission)
        self.assertAlmostEqual(job.commission_amount, round(200 * commission / 100, 2))
        self.assertAlmostEqual(
            job.technician_earning, round(200 - (200 * commission / 100), 2)
        )
        self.assertEqual(
            response.json()['request']['price_status']['status'], 'estimated'
        )


class PrivateDocumentTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp(prefix='dashawy-media-')
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.admin = User.objects.create(
            email='doc-admin@example.com',
            password_hash=hash_password(PASSWORD),
            role='admin',
            is_super_admin=True,
        )
        self.customer_user = User.objects.create(
            email='doc-customer@example.com',
            phone='01000004001',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        Customer.objects.create(
            user=self.customer_user, name='عميل', phone='01000004001'
        )
        tech_user = User.objects.create(
            email='doc-tech@example.com',
            phone='01000004002',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        self.technician = Technician.objects.create(
            user=tech_user,
            name='فني',
            phone='01000004002',
            specialty='dish_install',
            approval_status='pending',
        )

        private_dir = Path(self.media_root) / 'technicians' / 'user-1'
        private_dir.mkdir(parents=True, exist_ok=True)
        (private_dir / 'national_id.png').write_bytes(PNG_BYTES)
        public_dir = Path(self.media_root) / 'technicians' / 'profiles'
        public_dir.mkdir(parents=True, exist_ok=True)
        (public_dir / 'profile.png').write_bytes(PNG_BYTES)

        Technician.objects.filter(pk=self.technician.pk).update(
            national_id_path='/uploads/technicians/user-1/national_id.png',
            criminal_record_path='/uploads/technicians/user-1/national_id.png',
        )

    def test_document_is_admin_only(self):
        url = f'/api/admin/technicians/{self.technician.pk}/document/national_id'
        anonymous = self.client.get(url)
        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.force_authenticate(user=self.customer_user)
        denied = self.client.get(url)
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.admin)
        allowed = self.client.get(url)
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)
        self.assertTrue(allowed['Content-Type'].startswith('image/'))
        self.assertEqual(allowed['Cache-Control'], 'private, no-store')

    def test_document_type_is_whitelisted(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(
            f'/api/admin/technicians/{self.technician.pk}/document/passport'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_upload_route_serves_only_profile_pictures(self):
        profile = self.client.get('/uploads/technicians/profiles/profile.png')
        self.assertEqual(profile.status_code, status.HTTP_200_OK)

        secret = self.client.get('/uploads/technicians/user-1/national_id.png')
        self.assertEqual(secret.status_code, status.HTTP_404_NOT_FOUND)

        traversal = self.client.get(
            '/uploads/technicians/profiles/../../technicians/user-1/national_id.png'
        )
        self.assertEqual(traversal.status_code, status.HTTP_404_NOT_FOUND)

    def test_tampered_database_path_cannot_escape_media_root(self):
        outside = Path(tempfile.mkdtemp(prefix='dashawy-outside-'))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / 'secret.png').write_bytes(PNG_BYTES)
        Technician.objects.filter(pk=self.technician.pk).update(
            national_id_path='../' + outside.name + '/secret.png'
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(
            f'/api/admin/technicians/{self.technician.pk}/document/national_id'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PublicSiteTests(APITestCase):
    def setUp(self):
        cache.clear()
        clear_site_config_cache()

    def tearDown(self):
        clear_site_config_cache()

    def test_legal_and_order_pages_are_served(self):
        for path, marker in (
            ('/order.html', 'بيانات الطلب'),
            ('/track.html', 'متابعة الطلب'),
            ('/terms.html', 'شروط الاستخدام'),
            ('/refund-cancellation.html', 'سياسة الإلغاء والاسترداد'),
            ('/privacy.html', 'سياسة الخصوصية'),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, status.HTTP_200_OK, path)
            self.assertContains(response, marker, status_code=200)

    def test_tracking_page_is_noindex(self):
        response = self.client.get('/track.html')
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')

    def test_rendered_pages_do_not_hardcode_contact_details(self):
        for path in ('/privacy.html', '/terms.html', '/refund-cancellation.html', '/'):
            body = self.client.get(path).content.decode('utf-8')
            self.assertNotIn('01025330520', body)
            self.assertNotIn('meltokhey39@gmail.com', body)

    def test_manifest_is_rendered_with_site_url(self):
        response = self.client.get('/manifest.webmanifest')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('application/manifest+json', response['Content-Type'])
        self.assertContains(response, '"start_url": "/"')

    def test_robots_and_sitemap_use_site_url(self):
        robots = self.client.get('/robots.txt')
        self.assertEqual(robots.status_code, status.HTTP_200_OK)
        self.assertContains(robots, 'Disallow: /api/')
        self.assertContains(robots, settings.SITE_URL + '/sitemap.xml')

        sitemap = self.client.get('/sitemap.xml')
        self.assertEqual(sitemap.status_code, status.HTTP_200_OK)
        self.assertIn('application/xml', sitemap['Content-Type'])
        self.assertContains(sitemap, settings.SITE_URL + '/order.html')
        self.assertContains(sitemap, settings.SITE_URL + '/terms.html')

    def test_security_headers_on_html(self):
        response = self.client.get('/')
        self.assertIn('Content-Security-Policy', response)
        self.assertIn("object-src 'none'", response['Content-Security-Policy'])
        self.assertIn('frame-ancestors', response['Content-Security-Policy'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertIn('geolocation=(self)', response['Permissions-Policy'])
        self.assertEqual(
            response['Referrer-Policy'], 'strict-origin-when-cross-origin'
        )

    @override_settings(
        SITE_URL='https://example.test',
        BUSINESS_PHONE='01000000000',
        BUSINESS_WHATSAPP='201000000000',
        BUSINESS_EMAIL='hello@example.test',
        SOCIAL_FACEBOOK='',
        SOCIAL_INSTAGRAM='',
        SOCIAL_TIKTOK='',
    )
    def test_public_config_comes_from_settings(self):
        clear_site_config_cache()
        data = self.client.get('/api/public/config').json()
        self.assertEqual(data['site_url'], 'https://example.test')
        self.assertEqual(data['business_phone'], '01000000000')
        self.assertEqual(data['business_whatsapp'], '201000000000')
        self.assertEqual(data['business_email'], 'hello@example.test')
        self.assertIn('wa.me/201000000000', data['whatsapp_url'])

    def test_error_pages_exist(self):
        self.assertEqual(
            self.client.get('/404.html').status_code, status.HTTP_200_OK
        )
        self.assertContains(self.client.get('/404.html'), '404')

    def test_template_partials_are_not_standalone_pages(self):
        self.assertEqual(
            self.client.get('/partials/contact_dock.html').status_code,
            status.HTTP_404_NOT_FOUND,
        )


class TechnicianRegistrationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp(prefix='dashawy-tech-media-')
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def body(self, **overrides):
        body = {
            'name': 'فني جديد',
            'phone': '01000005001',
            'email': 'new-tech@example.com',
            'password': 'StrongPass123',
            'area_key': 'smouha',
            'accept_terms': 'true',
            'sensitive_consent': 'true',
            'policy_version': 'v1.0',
            'national_id': SimpleUploadedFile(
                'id.png', PNG_BYTES, content_type='image/png'
            ),
            'criminal_record': SimpleUploadedFile(
                'record.png', PNG_BYTES, content_type='image/png'
            ),
        }
        body.update(overrides)
        return body

    def test_sensitive_consent_is_required(self):
        body = self.body()
        body.pop('sensitive_consent')
        response = self.client.post(
            '/api/auth/register-technician', body, format='multipart'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email='new-tech@example.com').exists())

    def test_documents_are_saved_outside_the_public_profile_folder(self):
        response = self.client.post(
            '/api/auth/register-technician', self.body(), format='multipart'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        technician = Technician.objects.get(user__email='new-tech@example.com')
        self.assertTrue(
            technician.national_id_path.startswith('/uploads/technicians/user-')
        )
        self.assertNotIn('/profiles/', technician.national_id_path)
        self.assertTrue(
            (Path(self.media_root) / technician.national_id_path.lstrip('/uploads/')).is_file()
        )
        self.assertTrue(
            Consent.objects.filter(
                user=technician.user,
                consent_type='terms_privacy_sensitive_documents',
                policy_version='v1.0',
            ).exists()
        )


class ConfigAndProxyTests(APITestCase):
    """Every public value must come from configuration, never the source."""

    @override_settings(SITE_URL='https://example.test')
    def test_llms_txt_uses_configured_values(self):
        response = self.client.get('/llms.txt')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('text/plain', response['Content-Type'])
        body = response.content.decode('utf-8')
        self.assertIn('https://example.test/order.html', body)
        self.assertNotIn('dashawy.pythonanywhere.com', body)
        self.assertNotIn('{{', body)

    def test_about_page_canonical_follows_site_url(self):
        with override_settings(SITE_URL='https://example.test'):
            response = self.client.get('/about-ai.html')
        self.assertContains(response, 'https://example.test/about-ai.html')
        self.assertNotContains(response, 'dashawy.pythonanywhere.com')

    def test_client_ip_ignores_spoofed_header_from_public_address(self):
        from types import SimpleNamespace

        from .ratelimit import client_ip

        request = SimpleNamespace(
            META={
                'REMOTE_ADDR': '8.8.8.8',
                'HTTP_X_FORWARDED_FOR': '10.0.0.1, 198.51.100.7',
            }
        )
        self.assertEqual(client_ip(request), '8.8.8.8')

    def test_client_ip_uses_rightmost_hop_behind_a_proxy(self):
        from types import SimpleNamespace

        from .ratelimit import client_ip

        request = SimpleNamespace(
            META={
                'REMOTE_ADDR': '127.0.0.1',
                'HTTP_X_FORWARDED_FOR': '1.1.1.1, 198.51.100.7',
            }
        )
        self.assertEqual(client_ip(request), '198.51.100.7')
