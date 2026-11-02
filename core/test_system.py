"""A deploy that runs ahead of ``migrate`` keeps working, and the super admin
can apply the migrations from the admin panel."""

from unittest.mock import patch

from django.core.cache import cache
from django.db import DatabaseError
from django.test import RequestFactory
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .exceptions import ApiError
from .models import Customer, User

PASSWORD = 'TesterPass123'


class MissingTablesTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_rate_limit_falls_back_to_the_cache(self):
        from .ratelimit import throttle

        request = RequestFactory().get('/', REMOTE_ADDR='8.8.4.4')
        with patch('core.ratelimit._count_in_table', side_effect=DatabaseError('relation "rate_counters" does not exist')):
            for _ in range(2):
                throttle(request, 'fallback', limit=2, window_seconds=60)
            with self.assertRaises(ApiError):
                throttle(request, 'fallback', limit=2, window_seconds=60)

    def test_login_and_signed_in_requests_survive(self):
        user = User.objects.create(
            email='sys-customer@example.com', phone='+201000000801',
            password_hash=hash_password(PASSWORD), role='customer',
        )
        Customer.objects.create(user=user, name='عميل', phone='+201000000801')
        missing = DatabaseError('relation does not exist')
        with patch('core.ratelimit._count_in_table', side_effect=missing), \
                patch('core.authentication.RevokedAccessToken.objects.filter', side_effect=missing):
            login = self.client.post(
                '/api/auth/login', {'identifier': user.email, 'password': PASSWORD}, format='json'
            )
            self.assertEqual(login.status_code, 200)
            self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + login.json()['token'])
            self.assertEqual(self.client.get('/api/auth/me').status_code, 200)
            wrong = self.client.post(
                '/api/auth/login', {'identifier': user.email, 'password': 'nope-nope-1'}, format='json'
            )
            self.assertEqual(wrong.status_code, 401)


class AdminMigrateTests(APITestCase):
    def setUp(self):
        self.super_admin = User.objects.create(
            email='sys-super@example.com', password_hash='x', role='admin', is_super_admin=True
        )
        self.section_admin = User.objects.create(
            email='sys-section@example.com', password_hash='x', role='admin', permissions='["settings"]'
        )

    def test_only_the_super_admin(self):
        self.client.force_authenticate(user=self.section_admin)
        self.assertEqual(self.client.get('/api/admin/system/migrations').status_code, 403)
        self.assertEqual(self.client.post('/api/admin/system/migrate').status_code, 403)

    def test_nothing_pending_in_an_up_to_date_database(self):
        self.client.force_authenticate(user=self.super_admin)
        self.assertEqual(self.client.get('/api/admin/system/migrations').json(), {'pending': [], 'count': 0})
        done = self.client.post('/api/admin/system/migrate')
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()['applied'], [])

    def test_pending_migrations_are_applied_and_reported(self):
        self.client.force_authenticate(user=self.super_admin)
        states = iter([['core.0007_rate_counters'], []])
        with patch('core.system_views.pending_migrations', side_effect=lambda: next(states)), \
                patch('core.system_views.call_command') as migrate:
            done = self.client.post('/api/admin/system/migrate')
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()['applied'], ['core.0007_rate_counters'])
        self.assertEqual(migrate.call_args.args, ('migrate',))

    def test_duplicates_are_explained(self):
        self.client.force_authenticate(user=self.super_admin)
        failure = RuntimeError("Duplicate account contacts must be fixed before this migration: phone='+2010' on users [3, 9]")
        with patch('core.system_views.pending_migrations', return_value=['core.0006_revoked_tokens_and_unique_contacts']), \
                patch('core.system_views.call_command', side_effect=failure):
            response = self.client.post('/api/admin/system/migrate')
        self.assertEqual(response.status_code, 409)
        self.assertIn('حسابات متكررة', response.json()['error'])
        self.assertIn('[3, 9]', response.json()['detail'])
