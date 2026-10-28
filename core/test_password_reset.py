"""Regression tests for admin-assisted password resets and forced rotation."""

import logging

from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .auth_backend import BcryptAuthBackend, hash_password
from .models import AdminPasswordResetAudit, Customer, User
from .site_config import clear_site_config_cache

PASSWORD = 'TesterPass123'
NEW_PASSWORD = 'FreshPassword456'


class PasswordResetApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create(
            email='reset-admin@example.com',
            password_hash=hash_password(PASSWORD),
            role='admin',
            is_super_admin=True,
        )
        self.customer = User.objects.create(
            email='reset-customer@example.com',
            phone='+201000000201',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        Customer.objects.create(
            user=self.customer,
            name='Reset Customer',
            phone='+201000000201',
        )

    def login(self, user, password=PASSWORD):
        return self.client.post(
            '/api/auth/login',
            {'identifier': user.email, 'password': password},
            format='json',
        )

    def admin_reset(self, user):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            f'/api/admin/users/{user.pk}/reset-password'
        )
        self.client.force_authenticate(user=None)
        return response

    def test_admin_reset_returns_a_working_temporary_password_once(self):
        response = self.admin_reset(self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()

        temporary = body['temporary_password']
        self.assertTrue(temporary)
        self.assertEqual(body['temp_password'], temporary)
        self.assertTrue(body['must_change_password'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertNotIn('password_hash', body)

        self.customer.refresh_from_db()
        self.assertTrue(self.customer.must_change_password)
        self.assertFalse(
            BcryptAuthBackend.verify_password(self.customer, PASSWORD)
        )
        self.assertTrue(
            BcryptAuthBackend.verify_password(self.customer, temporary)
        )

        login = self.login(self.customer, temporary)
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.assertTrue(login.json()['must_change_password'])

    def test_non_admin_cannot_reset_a_password(self):
        original_hash = self.customer.password_hash
        self.client.force_authenticate(user=self.customer)
        response = self.client.post(
            f'/api/admin/users/{self.customer.pk}/reset-password'
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.password_hash, original_hash)
        self.assertFalse(self.customer.must_change_password)
        self.assertEqual(AdminPasswordResetAudit.objects.count(), 0)

    def test_forced_change_on_next_login_clears_the_flag(self):
        temporary = self.admin_reset(self.customer).json()['temporary_password']

        login = self.login(self.customer, temporary).json()
        self.assertTrue(login['must_change_password'])

        token_client = APIClient()
        token_client.credentials(HTTP_AUTHORIZATION='Bearer ' + login['token'])
        changed = token_client.post(
            '/api/auth/change-password',
            {
                'current_password': temporary,
                'new_password': NEW_PASSWORD,
            },
            format='json',
        )
        self.assertEqual(changed.status_code, status.HTTP_200_OK)
        body = changed.json()
        self.assertFalse(body['must_change_password'])
        self.assertIn('token', body)
        self.assertIn('refresh', body)

        self.customer.refresh_from_db()
        self.assertFalse(self.customer.must_change_password)
        self.assertTrue(
            BcryptAuthBackend.verify_password(self.customer, NEW_PASSWORD)
        )
        self.assertFalse(
            BcryptAuthBackend.verify_password(self.customer, temporary)
        )

        # The rotated token works; the temporary password does not sign in.
        rotated = APIClient()
        rotated.credentials(HTTP_AUTHORIZATION='Bearer ' + body['token'])
        self.assertEqual(
            rotated.get('/api/auth/me').status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.login(self.customer, temporary).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        fresh = self.login(self.customer, NEW_PASSWORD)
        self.assertEqual(fresh.status_code, status.HTTP_200_OK)
        self.assertFalse(fresh.json()['must_change_password'])

    def test_old_jwt_is_rejected_after_reset(self):
        old_token = self.login(self.customer).json()['token']
        old_client = APIClient()
        old_client.credentials(HTTP_AUTHORIZATION='Bearer ' + old_token)
        self.assertEqual(
            old_client.get('/api/auth/me').status_code,
            status.HTTP_200_OK,
        )

        temporary = self.admin_reset(self.customer).json()['temporary_password']
        self.assertTrue(temporary)

        rejected = old_client.get('/api/auth/me')
        self.assertEqual(rejected.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_audit_row_records_admin_target_time_and_ip(self):
        response = self.admin_reset(self.customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        audit = AdminPasswordResetAudit.objects.get()
        self.assertEqual(audit.admin_id, self.admin.pk)
        self.assertEqual(audit.target_id, self.customer.pk)
        self.assertEqual(audit.admin_user_id, self.admin.pk)
        self.assertEqual(audit.target_user_id, self.customer.pk)
        self.assertIsNotNone(audit.created_at)
        self.assertTrue(audit.ip_address)
        self.assertNotIn(
            response.json()['temporary_password'],
            str(audit.__dict__),
        )

    def test_temporary_password_never_reaches_logs(self):
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = Capture(level=logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            response = self.admin_reset(self.customer)
        finally:
            root.removeHandler(handler)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        temporary = response.json()['temporary_password']
        self.assertNotIn(temporary, '\n'.join(captured))

    def test_reset_endpoint_is_rate_limited(self):
        self.client.force_authenticate(user=self.admin)
        last = None
        for _ in range(11):
            last = self.client.post(
                f'/api/admin/users/{self.customer.pk}/reset-password'
            )
        self.assertEqual(last.status_code, 429)


class PasswordRecoveryUiTests(APITestCase):
    """The retired recovery flows no longer show dead forms or widgets."""

    def tearDown(self):
        clear_site_config_cache()

    @override_settings(BUSINESS_WHATSAPP='201000000000')
    def test_login_forgot_password_points_at_whatsapp(self):
        clear_site_config_cache()
        response = self.client.get('/login.html')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, 'كلمنا على واتساب وهنساعدك ترجّع حسابك')
        self.assertContains(response, 'wa.me/201000000000')
        self.assertNotContains(response, '/verify-otp.html')

    def test_unavailable_pages_render_a_friendly_notice(self):
        for path in ('/verify-otp.html', '/telegram-link.html'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertContains(response, 'الخدمة غير متاحة حالياً')

    def test_dashboards_no_longer_load_telegram_widgets(self):
        for path in (
            '/customer/home.html',
            '/customer/dashboard.html',
            '/technician/dashboard.html',
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertNotContains(response, 'telegram-settings.js')
            self.assertNotContains(response, 'id="telegram-settings"')
