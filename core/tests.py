"""Regression tests for the Phase 2 authentication API."""

from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import Customer, User

PASSWORD = 'TesterPass123'


class AuthApiTests(APITestCase):
    def create_customer(self, email='legacy@example.com', phone='+201099998888'):
        user = User.objects.create(
            email=email,
            phone=phone,
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        Customer.objects.create(user=user, name='Legacy Customer', phone=phone)
        return user

    def test_login_with_legacy_bcrypt_hash_returns_token(self):
        user = self.create_customer()
        response = self.client.post(
            '/api/auth/login',
            {'identifier': user.email, 'password': PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertIn('token', body)
        self.assertNotIn('access', body)
        self.assertEqual(body['user']['id'], user.id)
        self.assertEqual(body['user']['role'], 'customer')
        self.assertTrue(body['profile']['user_id'] == user.id)

    def test_phone_login_normalises_local_number(self):
        self.create_customer(phone='+201099998888')
        response = self.client.post(
            '/api/auth/login',
            {'identifier': '01099998888', 'password': PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('token', response.json())

    def test_wrong_and_unknown_credentials_share_one_message(self):
        user = self.create_customer()
        wrong = self.client.post(
            '/api/auth/login',
            {'email': user.email, 'password': 'wrong-password'},
            format='json',
        )
        unknown = self.client.post(
            '/api/auth/login',
            {'email': 'nobody@example.com', 'password': PASSWORD},
            format='json',
        )
        self.assertEqual(wrong.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(unknown.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(wrong.json()['error'], unknown.json()['error'])
        self.assertNotIn('detail', wrong.json())

    def test_register_creates_customer_and_token(self):
        response = self.client.post(
            '/api/auth/register',
            {
                'name': 'New Customer',
                'phone': '01099997777',
                'email': 'New.Customer@Example.com',
                'password': PASSWORD,
                'area_key': 'smouha',
                'location_text': '12 Test Street',
                'latitude': 31.2156,
                'longitude': 29.9553,
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()

        user = User.objects.get(email='new.customer@example.com')
        self.assertEqual(user.phone, '+201099997777')
        self.assertTrue(Customer.objects.filter(user=user).exists())
        self.assertEqual(body['profile']['location_text'], 'سموحة — 12 Test Street')

    def test_register_rejects_duplicate_email_and_weak_password(self):
        self.create_customer(email='taken@example.com')
        duplicate = self.client.post(
            '/api/auth/register',
            {
                'name': 'Another Customer',
                'phone': '01099996666',
                'email': 'taken@example.com',
                'password': PASSWORD,
            },
            format='json',
        )
        weak = self.client.post(
            '/api/auth/register',
            {
                'name': 'Another Customer',
                'phone': '01099995555',
                'email': 'weak@example.com',
                'password': 'short',
            },
            format='json',
        )
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(weak.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', weak.json())

    def test_me_requires_token_and_returns_fresh_user(self):
        unauth = self.client.get('/api/auth/me')
        self.assertEqual(unauth.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn('error', unauth.json())

        user = self.create_customer()
        login = self.client.post(
            '/api/auth/login',
            {'email': user.email, 'password': PASSWORD},
            format='json',
        )
        token = login.json()['token']
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + token)
        me = self.client.get('/api/auth/me')
        self.assertEqual(me.status_code, status.HTTP_200_OK)
        body = me.json()
        self.assertEqual(body['user']['email'], user.email)
        self.assertIn('telegram_linked', body['user'])
        self.assertEqual(body['profile']['name'], 'Legacy Customer')
