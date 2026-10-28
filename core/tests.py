"""Regression tests for the Phase 2 authentication API."""

import base64
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User as DjangoUser
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from .auth_backend import hash_password
from .models import (
    Customer,
    Request,
    RequestBroadcast,
    Specialty,
    Technician,
    User,
    utc_now,
)
from .serializers import TechnicianDocumentUploadSerializer
from .site_config import clear_site_config_cache
from .upload_validators import validate_document_upload

PASSWORD = 'TesterPass123'

# Real one-pixel samples so libmagic can identify the actual file format.
PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII='
)
JPEG_BYTES = base64.b64decode(
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABD/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/EH//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/EH//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/EH//2Q=='
)
PDF_BYTES = (
    b'%PDF-1.4\n'
    b'1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n'
    b'2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n'
    b'trailer\n<< /Root 1 0 R >>\n%%EOF\n'
)


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
        self.assertIn('refresh', body)

        refresh = RefreshToken(body['refresh'])
        self.assertEqual(refresh.get('userId'), str(user.id))
        outstanding = OutstandingToken.objects.get(jti=refresh['jti'])
        self.assertIsNone(outstanding.user)

    def test_logout_blacklists_refresh_token(self):
        user = self.create_customer()
        login = self.client.post(
            '/api/auth/login',
            {'identifier': user.email, 'password': PASSWORD},
            format='json',
        )
        body = login.json()
        self.client.credentials(
            HTTP_AUTHORIZATION='Bearer ' + body['token']
        )

        response = self.client.post(
            '/api/auth/logout',
            {'refresh': body['refresh']},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        with self.assertRaises(TokenError):
            RefreshToken(body['refresh']).verify()

    def test_logout_rejects_another_users_refresh_token(self):
        owner = self.create_customer(
            email='owner@example.com', phone='+201099991111'
        )
        other = self.create_customer(
            email='other@example.com', phone='+201099992222'
        )
        owner_login = self.client.post(
            '/api/auth/login',
            {'identifier': owner.email, 'password': PASSWORD},
            format='json',
        ).json()
        other_refresh = self.client.post(
            '/api/auth/login',
            {'identifier': other.email, 'password': PASSWORD},
            format='json',
        ).json()['refresh']

        self.client.credentials(
            HTTP_AUTHORIZATION='Bearer ' + owner_login['token']
        )
        response = self.client.post(
            '/api/auth/logout',
            {'refresh': other_refresh},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        RefreshToken(other_refresh).verify()

    def test_axes_locks_out_after_repeated_failures(self):
        locked = self.create_customer(
            email='locked@example.com', phone='+201099993333'
        )
        for _ in range(5):
            response = self.client.post(
                '/api/auth/login',
                {'identifier': locked.email, 'password': 'wrong-password'},
                format='json',
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['code'], 'account_locked')

        # Axes also locks the source IP, so a valid account cannot be used to
        # bypass the lockout from the same client.
        other = self.create_customer(
            email='other-lock@example.com', phone='+201099994444'
        )
        blocked = self.client.post(
            '/api/auth/login',
            {'identifier': other.email, 'password': PASSWORD},
            format='json',
        )
        self.assertEqual(blocked.status_code, 429)

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

    def test_django_admin_credentials_cannot_log_into_legacy_api(self):
        user = User.objects.create(
            email='legacy-admin@example.com',
            phone='+201099995555',
            password_hash=hash_password('CorePass123'),
            role='customer',
        )
        Customer.objects.create(
            user=user, name='Legacy Admin', phone='+201099995555'
        )
        admin = DjangoUser.objects.create(
            id=user.id,
            username=user.email,
            is_staff=True,
            is_superuser=True,
        )
        admin.set_password('DjangoPass123')
        admin.save()

        response = self.client.post(
            '/api/auth/login',
            {'identifier': user.email, 'password': 'DjangoPass123'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn('token', response.json())

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
                'accept_terms': True,
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
                'accept_terms': True,
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
                'accept_terms': True,
            },
            format='json',
        )
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(weak.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', weak.json())

    def test_register_rejects_common_and_similar_passwords(self):
        common = self.client.post(
            '/api/auth/register',
            {
                'name': 'Common Password',
                'phone': '01099994444',
                'email': 'common@example.com',
                'password': 'password1',
                'accept_terms': True,
            },
            format='json',
        )
        similar = self.client.post(
            '/api/auth/register',
            {
                'name': 'NewCustomer',
                'phone': '01099993333',
                'email': 'newcustomer@example.com',
                'password': 'NewCustomer123',
                'accept_terms': True,
            },
            format='json',
        )
        self.assertEqual(common.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(similar.status_code, status.HTTP_400_BAD_REQUEST)

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


class RequestApiTests(APITestCase):
    """Phase 3: customer requests, auto-assignment and technician actions."""

    def setUp(self):
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        Specialty.objects.create(key='dish_repair', label='صيانة دش')

        self.customer_user = User.objects.create(
            email='customer@example.com',
            phone='+201000000001',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user,
            name='Test Customer',
            phone='+201000000001',
            location_text='سموحة',
            latitude=31.2156,
            longitude=29.9553,
        )

        self.tech_user = User.objects.create(
            email='tech@example.com',
            phone='+201000000002',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        self.technician = Technician.objects.create(
            user=self.tech_user,
            name='Test Technician',
            phone='+201000000002',
            specialty='dish_install',
            availability='available',
            approval_status='approved',
            location_text='سموحة',
            latitude=31.2160,
            longitude=29.9560,
        )

    def authenticate(self, user):
        self.client.force_authenticate(user=user)

    def create_request(self, specialty='dish_install', **overrides):
        body = {
            'specialty': specialty,
            'description': 'الدش لا يعمل',
            'location_text': 'شارع الاختبار',
            'latitude': 31.2156,
            'longitude': 29.9553,
        }
        body.update(overrides)
        return self.client.post('/api/customer/requests', body, format='json')

    def test_create_request_assigns_matching_technician(self):
        self.authenticate(self.customer_user)
        response = self.create_request()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertTrue(body['assigned'])
        self.assertEqual(body['message'], 'تم تعيين فني لطلبك')
        self.assertEqual(body['request']['status'], 'assigned')
        self.assertEqual(body['request']['technician_id'], self.technician.id)
        self.assertEqual(body['request']['customer_id'], self.customer.id)
        self.assertEqual(body['request']['customer_name'], 'Test Customer')
        self.assertEqual(
            body['request']['technician_profile_url'],
            '/technician-profile.html?id=' + str(self.technician.id),
        )

        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'busy')
        broadcast = RequestBroadcast.objects.get(
            request_id=body['request']['id'], technician=self.technician
        )
        self.assertEqual(broadcast.status, 'accepted')

    def test_create_request_without_technician_stays_pending(self):
        self.authenticate(self.customer_user)
        response = self.create_request(specialty='dish_repair')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertFalse(body['assigned'])
        self.assertEqual(
            body['message'],
            'تم استلام طلبك بنجاح وسيتم التواصل معك قريباً',
        )
        self.assertEqual(body['request']['status'], 'pending')
        self.assertIsNone(body['request']['technician_id'])
        self.assertEqual(
            body['assignment']['reason'], 'no_technician_available'
        )

    def test_customer_lists_and_reads_only_own_requests(self):
        self.authenticate(self.customer_user)
        created = self.create_request().json()['request']

        listing = self.client.get('/api/customer/requests')
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listing.json()['requests']), 1)
        self.assertEqual(listing.json()['requests'][0]['id'], created['id'])

        detail = self.client.get(f"/api/customer/requests/{created['id']}")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.json()['request']['id'], created['id'])

        other_user = User.objects.create(
            email='other@example.com',
            phone='+201000000009',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        other_customer = Customer.objects.create(
            user=other_user, name='Other', phone='+201000000009'
        )
        foreign = Request.objects.create(
            customer=other_customer,
            specialty='dish_install',
            description='not yours',
        )
        denied = self.client.get(f'/api/customer/requests/{foreign.id}')
        self.assertEqual(denied.status_code, status.HTTP_404_NOT_FOUND)

    def test_technician_lists_jobs_without_start_pin(self):
        self.authenticate(self.customer_user)
        created = self.create_request().json()['request']

        self.authenticate(self.tech_user)
        response = self.client.get('/api/technician/requests')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        jobs = response.json()['requests']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['id'], created['id'])
        self.assertNotIn('start_pin', jobs[0])

    def test_technician_accepts_job_with_arrival_code(self):
        self.authenticate(self.customer_user)
        created = self.create_request().json()['request']
        pin = created['start_pin']
        self.assertRegex(pin, r'^\d{4}$')

        self.authenticate(self.tech_user)
        response = self.client.post(
            f"/api/technician/requests/{created['id']}/accept", {'pin': pin}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['request']['status'], 'in_progress')
        self.assertNotIn('start_pin', response.json()['request'])

        job = Request.objects.get(pk=created['id'])
        self.assertEqual(job.status, 'in_progress')
        self.assertIsNotNone(job.started_at)
        self.assertIsNone(job.start_pin)

    def test_technician_accept_without_arrival_code_is_rejected(self):
        self.authenticate(self.customer_user)
        created = self.create_request().json()['request']

        self.authenticate(self.tech_user)
        response = self.client.post(
            f"/api/technician/requests/{created['id']}/accept"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        job = Request.objects.get(pk=created['id'])
        self.assertEqual(job.status, 'assigned')
        self.assertIsNone(job.started_at)

    def test_technician_cannot_act_on_another_technicians_job(self):
        self.authenticate(self.customer_user)
        created = self.create_request().json()['request']

        second_user = User.objects.create(
            email='tech-owner@example.com',
            phone='+201000000004',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        second = Technician.objects.create(
            user=second_user,
            name='Second Technician',
            phone='+201000000004',
            specialty='dish_install',
            availability='busy',
            approval_status='approved',
        )
        Request.objects.filter(pk=created['id']).update(technician=second)

        self.authenticate(self.tech_user)
        response = self.client.post(
            f"/api/technician/requests/{created['id']}/accept"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rejection_unassigns_and_reassigns_to_next_technician(self):
        second_user = User.objects.create(
            email='tech2@example.com',
            phone='+201000000003',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        second = Technician.objects.create(
            user=second_user,
            name='Second Technician',
            phone='+201000000003',
            specialty='dish_install',
            availability='available',
            approval_status='approved',
        )

        self.authenticate(self.customer_user)
        created = self.create_request().json()['request']
        self.assertEqual(created['technician_id'], self.technician.id)

        self.authenticate(self.tech_user)
        response = self.client.post(
            f"/api/technician/requests/{created['id']}/reject"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body['reassigned'])
        self.assertEqual(body['request']['technician_id'], second.id)

        self.technician.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')
        self.assertEqual(second.availability, 'busy')

        job = Request.objects.get(pk=created['id'])
        self.assertIn(str(self.technician.id), job.declined_technicians)
        decline = RequestBroadcast.objects.get(
            request=job, technician=self.technician
        )
        self.assertEqual(decline.status, 'declined')

    def test_rejection_with_no_other_technician_returns_to_pending(self):
        self.authenticate(self.customer_user)
        created = self.create_request().json()['request']

        self.authenticate(self.tech_user)
        response = self.client.post(
            f"/api/technician/requests/{created['id']}/reject"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertFalse(body['reassigned'])
        self.assertEqual(body['request']['status'], 'pending')
        self.assertIsNone(body['request']['technician_id'])

        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')

    def test_role_guards(self):
        self.authenticate(self.tech_user)
        customer_route = self.client.get('/api/customer/requests')
        self.assertEqual(customer_route.status_code, status.HTTP_403_FORBIDDEN)

        self.authenticate(self.customer_user)
        technician_route = self.client.get('/api/technician/requests')
        self.assertEqual(technician_route.status_code, status.HTTP_403_FORBIDDEN)

    def test_validation_rejects_unknown_specialty_and_other_without_text(self):
        self.authenticate(self.customer_user)

        unknown = self.create_request(specialty='plumbing')
        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)

        other = self.create_request(specialty='other')
        self.assertEqual(other.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', other.json())


class FrontendServingTests(APITestCase):
    """Django serves the legacy frontend without changing its files."""

    def test_root_serves_index(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, 'دشاوي')

    def test_admin_pages_and_extensionless_routes(self):
        login = self.client.get('/admin/login.html')
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.assertContains(login, 'دخول لوحة التحكم')

        alias = self.client.get('/admin')
        self.assertEqual(alias.status_code, status.HTTP_200_OK)
        self.assertContains(alias, 'دخول لوحة التحكم')

        dashboard = self.client.get('/admin/dashboard')
        self.assertEqual(dashboard.status_code, status.HTTP_200_OK)
        self.assertContains(dashboard, 'لوحة التحكم')

    def test_css_js_and_images_are_served(self):
        css = self.client.get('/css/style.css')
        self.assertEqual(css.status_code, status.HTTP_200_OK)
        self.assertIn('text/css', css['Content-Type'])

        js = self.client.get('/js/api.js')
        self.assertEqual(js.status_code, status.HTTP_200_OK)
        self.assertIn('javascript', js['Content-Type'])

        icon = self.client.get('/dashawy-brand/favicon.svg')
        self.assertEqual(icon.status_code, status.HTTP_200_OK)

        static_css = self.client.get('/static/css/style.css')
        self.assertEqual(static_css.status_code, status.HTTP_200_OK)
        self.assertIn('text/css', static_css['Content-Type'])

    def test_api_routes_take_precedence_over_frontend_catch_all(self):
        response = self.client.get('/api/auth/me')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn('application/json', response['Content-Type'])
        self.assertIn('error', response.json())

    def test_every_page_renders_without_raw_template_tags(self):
        root = Path(settings.BASE_DIR) / 'core' / 'templates' / 'frontend'
        pages = [
            path.relative_to(root).as_posix()
            for path in sorted(root.rglob('*.html'))
            if not path.relative_to(root).as_posix().startswith('partials/')
        ]
        self.assertGreater(len(pages), 20)

        for page in pages:
            with self.subTest(page=page):
                response = self.client.get('/' + page)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                body = response.content.decode('utf-8')
                self.assertNotIn('{%', body)
                self.assertNotIn('{{', body)
                self.assertNotIn('{#', body)

    def test_static_html_redirects_to_the_rendered_route(self):
        response = self.client.get('/static/index.html')
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response['Location'], '/index.html')

        rendered = self.client.get('/static/index.html', follow=True)
        self.assertEqual(rendered.status_code, status.HTTP_200_OK)
        self.assertNotIn('{%', rendered.content.decode('utf-8'))

    @override_settings(
        BUSINESS_WHATSAPP='201025330520',
        BUSINESS_PHONE='01025330520',
    )
    def test_contact_context_is_rendered_on_the_homepage(self):
        clear_site_config_cache()
        self.addCleanup(clear_site_config_cache)
        response = self.client.get('/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.content.decode('utf-8')
        self.assertIn('https://wa.me/201025330520', body)
        self.assertIn('01025330520', body)
        self.assertIn('الإسكندرية، مصر', body)


class AdminApiTests(APITestCase):
    """Phase 4 admin dashboard endpoints."""

    def setUp(self):
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        Specialty.objects.create(key='dish_repair', label='صيانة دش')

        self.admin_user = User.objects.create(
            email='admin@example.com',
            password_hash=hash_password(PASSWORD),
            role='admin',
            is_super_admin=True,
        )
        self.customer_user = User.objects.create(
            email='admin-test-customer@example.com',
            phone='+201000000101',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user,
            name='Admin Test Customer',
            phone='+201000000101',
        )
        self.tech_user = User.objects.create(
            email='admin-test-tech@example.com',
            phone='+201000000102',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        self.technician = Technician.objects.create(
            user=self.tech_user,
            name='Admin Test Technician',
            phone='+201000000102',
            specialty='dish_install',
            availability='available',
            approval_status='approved',
            documents_verified=False,
        )
        self.assigned = Request.objects.create(
            customer=self.customer,
            technician=self.technician,
            specialty='dish_install',
            description='Assigned request',
            status='assigned',
            assigned_at=utc_now(),
        )
        self.pending = Request.objects.create(
            customer=self.customer,
            specialty='dish_repair',
            description='Pending request',
            status='pending',
        )
        RequestBroadcast.objects.create(
            request=self.assigned,
            technician=self.technician,
            status='sent',
        )

    def authenticate_admin(self):
        self.client.force_authenticate(user=self.admin_user)

    def test_stats_returns_flat_and_dashboard_shapes(self):
        self.authenticate_admin()
        response = self.client.get('/api/admin/stats')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()

        self.assertEqual(body['totalRequests'], 2)
        self.assertEqual(body['pendingRequests'], 1)
        self.assertEqual(body['activeTechnicians'], 1)
        self.assertEqual(body['totalCustomers'], 1)
        self.assertEqual(body['completedRequests'], 0)
        self.assertEqual(body['cancelledRequests'], 0)

        self.assertEqual(body['byStatus']['pending'], 1)
        self.assertEqual(body['byStatus']['assigned'], 1)
        self.assertEqual(len(body['newestRequests']), 2)
        self.assertEqual(body['newestRequests'][0]['customer_name'], 'Admin Test Customer')

    def test_requests_list_filters_by_status_and_specialty(self):
        self.authenticate_admin()

        pending = self.client.get('/api/admin/requests?status=pending')
        self.assertEqual(pending.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row['id'] for row in pending.json()['requests']], [self.pending.id]
        )

        specialty = self.client.get('/api/admin/requests?specialty=dish_install')
        self.assertEqual(specialty.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row['id'] for row in specialty.json()['requests']], [self.assigned.id]
        )

        completed = self.client.get('/api/admin/requests?status=completed')
        self.assertEqual(completed.status_code, status.HTTP_200_OK)
        self.assertEqual(completed.json()['requests'], [])

    def test_technicians_list_and_verify_toggle(self):
        self.authenticate_admin()
        response = self.client.get('/api/admin/technicians')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        technician = response.json()['technicians'][0]
        self.assertEqual(technician['id'], self.technician.id)
        self.assertEqual(technician['email'], self.tech_user.email)
        self.assertEqual(technician['account_phone'], self.tech_user.phone)
        self.assertEqual(technician['availability'], 'available')
        self.assertFalse(technician['documents_verified'])

        first = self.client.post(
            f'/api/admin/technicians/{self.technician.id}/verify'
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertTrue(first.json()['documents_verified'])
        self.technician.refresh_from_db()
        self.assertTrue(self.technician.documents_verified)

        second = self.client.post(
            f'/api/admin/technicians/{self.technician.id}/verify'
        )
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertFalse(second.json()['documents_verified'])
        self.technician.refresh_from_db()
        self.assertFalse(self.technician.documents_verified)

    def test_customers_list_includes_account_and_request_count(self):
        self.authenticate_admin()
        response = self.client.get('/api/admin/customers')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer = response.json()['customers'][0]
        self.assertEqual(customer['id'], self.customer.id)
        self.assertEqual(customer['email'], self.customer_user.email)
        self.assertEqual(customer['account_phone'], self.customer_user.phone)
        self.assertEqual(customer['requests_count'], 2)

    def test_cancel_frees_technician_and_expires_offers(self):
        self.technician.availability = 'busy'
        self.technician.save(update_fields=['availability'])

        self.authenticate_admin()
        response = self.client.post(
            f'/api/admin/requests/{self.assigned.id}/cancel'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['message'], 'تم إلغاء الطلب')

        self.assigned.refresh_from_db()
        self.technician.refresh_from_db()
        self.assertEqual(self.assigned.status, 'cancelled')
        self.assertEqual(self.technician.availability, 'available')
        broadcast = RequestBroadcast.objects.get(request=self.assigned)
        self.assertEqual(broadcast.status, 'expired')

    def test_cancel_rejects_completed_and_missing_requests(self):
        self.pending.status = 'completed'
        self.pending.save(update_fields=['status'])

        self.authenticate_admin()
        completed = self.client.post(
            f'/api/admin/requests/{self.pending.id}/cancel'
        )
        self.assertEqual(completed.status_code, status.HTTP_400_BAD_REQUEST)

        missing = self.client.post('/api/admin/requests/999999/cancel')
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_endpoints_reject_non_admins(self):
        self.client.force_authenticate(user=self.customer_user)
        self.assertEqual(
            self.client.get('/api/admin/stats').status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get('/api/admin/customers').status_code,
            status.HTTP_403_FORBIDDEN,
        )


class SecuritySettingsTests(APITestCase):
    """The production security layers stay wired together."""

    def test_axes_and_token_blacklist_are_installed(self):
        self.assertIn('axes', settings.INSTALLED_APPS)
        self.assertIn(
            'rest_framework_simplejwt.token_blacklist', settings.INSTALLED_APPS
        )
        self.assertEqual(
            settings.MIDDLEWARE[0], 'axes.middleware.AxesMiddleware'
        )
        self.assertIn(
            'django.middleware.csrf.CsrfViewMiddleware', settings.MIDDLEWARE
        )
        self.assertEqual(
            settings.AUTHENTICATION_BACKENDS[0],
            'axes.backends.AxesStandaloneBackend',
        )

    def test_password_validators_and_cors_are_locked_down(self):
        validator_names = {
            validator['NAME']
            for validator in settings.AUTH_PASSWORD_VALIDATORS
        }
        self.assertIn(
            'django.contrib.auth.password_validation.MinimumLengthValidator',
            validator_names,
        )
        self.assertIn(
            'django.contrib.auth.password_validation.CommonPasswordValidator',
            validator_names,
        )
        self.assertIn(
            'django.contrib.auth.password_validation.NumericPasswordValidator',
            validator_names,
        )
        self.assertIn(
            'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
            validator_names,
        )
        minimum_length = next(
            validator
            for validator in settings.AUTH_PASSWORD_VALIDATORS
            if validator['NAME'].endswith('MinimumLengthValidator')
        )
        self.assertEqual(minimum_length['OPTIONS']['min_length'], 8)

        self.assertFalse(settings.CORS_ALLOW_ALL_ORIGINS)
        self.assertNotIn('*', settings.CORS_ALLOWED_ORIGINS)
        self.assertEqual(settings.X_FRAME_OPTIONS, 'DENY')


class UploadValidationTests(APITestCase):
    """Magic-byte validation rejects spoofed technician documents."""

    def test_validator_accepts_real_jpeg_png_and_pdf(self):
        self.assertEqual(validate_document_upload(
            SimpleUploadedFile('photo.jpg', JPEG_BYTES, content_type='image/jpeg')
        ), 'image/jpeg')
        self.assertEqual(validate_document_upload(
            SimpleUploadedFile('photo.png', PNG_BYTES, content_type='image/png')
        ), 'image/png')
        self.assertEqual(validate_document_upload(
            SimpleUploadedFile('scan.pdf', PDF_BYTES, content_type='application/pdf')
        ), 'application/pdf')

    def test_serializer_accepts_valid_documents(self):
        serializer = TechnicianDocumentUploadSerializer(
            data={
                'criminal_record': SimpleUploadedFile(
                    'criminal.pdf', PDF_BYTES, content_type='application/pdf'
                ),
                'national_id': SimpleUploadedFile(
                    'id.png', PNG_BYTES, content_type='image/png'
                ),
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_serializer_rejects_spoofed_content_type(self):
        spoofed = SimpleUploadedFile(
            'document.pdf',
            b'<html><script>alert(1)</script></html>',
            content_type='application/pdf',
        )
        serializer = TechnicianDocumentUploadSerializer(
            data={
                'criminal_record': spoofed,
                'national_id': spoofed,
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('criminal_record', serializer.errors)
        self.assertIn('national_id', serializer.errors)
