"""Google sign-in: which accounts an ID token may open.

The token itself is verified in ``test_google_auth``; here ``verify_id_token``
is replaced so the account rules can be exercised.
"""

from unittest.mock import patch

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .google_views import GOOGLE_NO_PASSWORD
from .models import Customer, User

CLAIMS = {'sub': 'google-sub-1', 'email': 'victim@example.com', 'name': 'Victim'}


class GoogleLoginAccountTests(APITestCase):
    def setUp(self):
        cache.clear()
        verify = patch('core.google_views.verify_id_token', return_value=dict(CLAIMS))
        verify.start()
        self.addCleanup(verify.stop)
        push = patch('core.services.notifications.send_telegram', return_value=True)
        push.start()
        self.addCleanup(push.stop)

    def google(self):
        return self.client.post('/api/auth/google', {'id_token': 'x'}, format='json')

    def test_password_account_is_never_linked_by_email(self):
        # An attacker registers the victim's address first (registration does
        # not verify email); the victim's Google sign-in must not open it.
        planted = User.objects.create(
            email='victim@example.com', password_hash=hash_password('AttackerPass1'), role='customer'
        )
        Customer.objects.create(user=planted, name='x')
        response = self.google()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertNotIn('token', response.json())
        planted.refresh_from_db()
        self.assertIsNone(planted.google_sub)
        self.assertEqual(User.objects.count(), 1)

    def test_new_and_returning_google_accounts(self):
        first = self.google()
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertIn('token', first.json())
        user = User.objects.get(email='victim@example.com')
        self.assertEqual(user.google_sub, 'google-sub-1')
        self.assertEqual(user.password_hash, GOOGLE_NO_PASSWORD)
        # A password login against the Google sentinel fails closed.
        login = self.client.post(
            '/api/auth/login', {'identifier': 'victim@example.com', 'password': GOOGLE_NO_PASSWORD},
            format='json',
        )
        self.assertEqual(login.status_code, status.HTTP_401_UNAUTHORIZED)

        again = self.google()
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.count(), 1)

    def test_other_roles_and_other_google_accounts_are_refused(self):
        tech = User.objects.create(email='victim@example.com', password_hash='x', role='technician')
        self.assertEqual(self.google().status_code, status.HTTP_403_FORBIDDEN)
        User.objects.filter(pk=tech.pk).update(role='customer', google_sub='someone-else', password_hash=GOOGLE_NO_PASSWORD)
        self.assertEqual(self.google().status_code, status.HTTP_403_FORBIDDEN)
