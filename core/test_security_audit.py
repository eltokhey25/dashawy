"""Regression tests for the October 2026 security audit fixes."""

import tempfile
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import Consent, Customer, Request, Technician, User

PASSWORD = 'TesterPass123'


def make_user(email, role, **extra):
    return User.objects.create(
        email=email, password_hash=hash_password(PASSWORD), role=role, **extra
    )


class AdminPasswordResetScopeTests(APITestCase):
    """A regular admin must not be able to take over admin accounts."""

    def setUp(self):
        cache.clear()
        self.super_admin = make_user('super@example.com', 'admin', is_super_admin=True)
        self.admin = make_user('admin@example.com', 'admin')
        self.other_admin = make_user('admin2@example.com', 'admin')
        self.customer = make_user('cust@example.com', 'customer', phone='+201000000301')
        Customer.objects.create(user=self.customer, name='عميل', phone='+201000000301')

    def reset(self, actor, target):
        self.client.force_authenticate(user=actor)
        return self.client.post(f'/api/admin/users/{target.pk}/reset-password')

    def test_regular_admin_cannot_reset_super_admin(self):
        before = self.super_admin.password_hash
        response = self.reset(self.admin, self.super_admin)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.super_admin.refresh_from_db()
        self.assertEqual(self.super_admin.password_hash, before)

    def test_regular_admin_cannot_reset_another_admin(self):
        response = self.reset(self.admin, self.other_admin)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_admin_can_still_reset_customer(self):
        response = self.reset(self.admin, self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('temporary_password', response.json())

    def test_super_admin_can_reset_admin(self):
        response = self.reset(self.super_admin, self.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_nobody_resets_their_own_password_here(self):
        response = self.reset(self.super_admin, self.super_admin)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TechnicianApprovalGateTests(APITestCase):
    """A rejected technician's still-valid token must stop working."""

    def setUp(self):
        self.tech_user = make_user('tech@example.com', 'technician', phone='+201000000302')
        self.technician = Technician.objects.create(
            user=self.tech_user,
            name='فني',
            phone='+201000000302',
            specialty='dish_install',
            availability='available',
            approval_status='approved',
        )
        self.client.force_authenticate(user=self.tech_user)

    def test_approved_technician_lists_jobs(self):
        response = self.client.get('/api/technician/requests')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_rejected_technician_is_refused(self):
        Technician.objects.filter(pk=self.technician.pk).update(
            approval_status='rejected'
        )
        response = self.client.get('/api/technician/requests')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.post('/api/technician/requests/1/start', {'pin': '1234'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class LoginIpThrottleTests(APITestCase):
    """Password spraying across many accounts from one IP is capped."""

    def setUp(self):
        cache.clear()
        self.user = make_user('spray@example.com', 'customer', phone='+201000000303')
        Customer.objects.create(user=self.user, name='عميل', phone='+201000000303')

    def attempt(self, identifier, password=PASSWORD):
        return self.client.post(
            '/api/auth/login',
            {'identifier': identifier, 'password': password},
            format='json',
        )

    @mock.patch('core.views.LOGIN_IP_FAILURE_LIMIT', 3)
    def test_failures_across_accounts_lock_the_ip(self):
        for i in range(3):
            response = self.attempt(f'nobody{i}@example.com', 'wrong-pass-1')
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        # Even the correct password is refused once the IP budget is spent.
        response = self.attempt(self.user.email)
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @mock.patch('core.views.LOGIN_IP_FAILURE_LIMIT', 3)
    def test_successful_logins_do_not_consume_the_budget(self):
        for _ in range(5):
            response = self.attempt(self.user.email)
            self.assertEqual(response.status_code, status.HTTP_200_OK)


class GuestPhotoServingTests(APITestCase):
    """Only raster images are ever streamed back, never an SVG."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_svg_on_disk_is_not_served(self):
        from .guest_views import hash_token

        orders = Path(self.media_root) / 'orders'
        orders.mkdir(parents=True)
        (orders / 'evil.svg').write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        )
        token = 'a' * 43
        Request.objects.create(
            guest_name='ضيف',
            guest_phone='01000000304',
            guest_token_hash=hash_token(token),
            specialty='dish_install',
            description='x',
            status='pending',
            order_photo_path='/uploads/orders/evil.svg',
        )
        response = self.client.get(f'/api/guest/requests/{token}/photo')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ConsentClientIpTests(APITestCase):
    """The consent audit row records the real client, not the proxy."""

    def test_registration_consent_uses_forwarded_ip(self):
        cache.clear()
        response = self.client.post(
            '/api/auth/register',
            {
                'name': 'عميل جديد',
                'phone': '01000000305',
                'email': 'consent-ip@example.com',
                'password': PASSWORD,
                'accept_terms': True,
            },
            format='json',
            HTTP_X_FORWARDED_FOR='203.0.113.9',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        consent = Consent.objects.get(user__email='consent-ip@example.com')
        self.assertEqual(consent.ip_address, '203.0.113.9')
