"""Admin portal sign-in by username instead of email."""

import importlib
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.cache import cache
from django.core.management import call_command
from rest_framework.test import APITestCase

from .auth_backend import BcryptAuthBackend, hash_password
from .models import User

PASSWORD = 'TesterPass123'


class AdminUsernameLoginTests(APITestCase):
    def setUp(self):
        cache.clear()
        telegram = patch('core.services.notifications.send_telegram', return_value=True)
        telegram.start()
        self.addCleanup(telegram.stop)
        self.admin = User.objects.create(
            email='boss@example.com', username='boss',
            password_hash=hash_password(PASSWORD), role='admin', is_super_admin=True,
        )
        self.customer = User.objects.create(
            email='cust@example.com', username='cust',
            password_hash=hash_password(PASSWORD), role='customer',
        )

    def login(self, **body):
        return self.client.post('/api/auth/login', body, format='json')

    def test_admin_signs_in_with_username(self):
        response = self.login(username='Boss', password=PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['id'], self.admin.pk)
        self.assertEqual(response.json()['user']['username'], 'boss')

    def test_username_path_rejects_wrong_password_and_email(self):
        self.assertEqual(self.login(username='boss', password='WrongPass123').status_code, 401)
        # The username field never falls back to email lookup.
        self.assertEqual(self.login(username='boss@example.com', password=PASSWORD).status_code, 401)

    def test_username_path_only_matches_admins(self):
        self.assertEqual(self.login(username='cust', password=PASSWORD).status_code, 401)

    def test_customer_email_login_still_works(self):
        self.assertEqual(self.login(identifier='cust@example.com', password=PASSWORD).status_code, 200)

    def test_super_admin_creates_admin_without_email_and_renames(self):
        self.client.force_authenticate(user=self.admin)
        made = self.client.post(
            '/api/admin/admins', {'username': 'Sara_1', 'password': 'AdminPass123'}, format='json'
        )
        self.assertEqual(made.status_code, 201)
        new_id = made.json()['admin']['id']
        listed = {a['id']: a for a in self.client.get('/api/admin/admins').json()['admins']}
        self.assertEqual(listed[new_id]['username'], 'sara_1')
        self.assertEqual(listed[new_id]['email'], '')

        self.assertEqual(
            self.client.post('/api/admin/admins', {'username': 'sara_1', 'password': 'AdminPass123'}, format='json').status_code,
            400,
        )
        for bad in ('1abc', 'ab', 'a@b', 'with space'):
            self.assertEqual(
                self.client.post('/api/admin/admins', {'username': bad, 'password': 'AdminPass123'}, format='json').status_code,
                400, bad,
            )

        self.assertEqual(
            self.client.post(f'/api/admin/admins/{new_id}/username', {'username': 'boss'}, format='json').status_code,
            400,
        )
        renamed = self.client.post(f'/api/admin/admins/{new_id}/username', {'username': 'sara'}, format='json')
        self.assertEqual(renamed.status_code, 200)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.login(username='sara', password='AdminPass123').status_code, 200)

    def test_reset_command_by_username_and_set_username(self):
        call_command('reset_admin_password', username='boss', password='NewPass1234', set_username='chief')
        user = User.objects.get(pk=self.admin.pk)
        self.assertEqual(user.username, 'chief')
        self.assertTrue(BcryptAuthBackend.verify_password(user, 'NewPass1234'))


class BackfillTests(APITestCase):
    def test_existing_admins_get_usernames(self):
        migration = importlib.import_module('core.migrations.0011_admin_username')
        a = User.objects.create(email='ali@x.com', password_hash='x', role='admin')
        b = User.objects.create(email='ali@y.com', password_hash='x', role='admin')
        c = User.objects.create(email='9@z.com', password_hash='x', role='admin')
        cust = User.objects.create(email='cust@z.com', password_hash='x', role='customer')
        migration.backfill_admin_usernames(django_apps, None)
        names = dict(User.objects.values_list('pk', 'username'))
        self.assertEqual(names[a.pk], 'ali')
        self.assertEqual(names[b.pk], 'ali2')
        self.assertEqual(names[c.pk], 'admin9')
        self.assertIsNone(names[cust.pk])
