"""Third round of audit fixes: notification fan-out, uploads, API exposure,
sessions, counters, money races and the script policy."""

import gzip
import io
import json
import os
import re
import tempfile
import time
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import RequestFactory, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .exceptions import ApiError
from .models import (
    Customer,
    PushSubscription,
    RateCounter,
    Request,
    RevokedAccessToken,
    Specialty,
    StoredFile,
    Technician,
    TechnicianWallet,
    User,
    WalletTransaction,
    utc_now,
)
from .tests import PDF_BYTES, PNG_BYTES

PASSWORD = 'TesterPass123'
DATABASE_STORAGES = {
    'default': {'BACKEND': 'core.storage.DatabaseStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class Round3Base(APITestCase):
    def setUp(self):
        cache.clear()
        stub = patch('core.services.notifications.send_telegram', return_value=True)
        stub.start()
        self.addCleanup(stub.stop)
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        self.customer_user = User.objects.create(
            email='r3-customer@example.com', phone='+201000000701',
            password_hash=hash_password(PASSWORD), role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل', phone='+201000000701'
        )
        self.tech_user = User.objects.create(
            email='r3-tech@example.com', phone='+201000000702',
            password_hash=hash_password(PASSWORD), role='technician',
        )
        self.technician = Technician.objects.create(
            user=self.tech_user, name='فني', phone='+201000000702',
            specialty='dish_install', availability='available', approval_status='approved',
        )
        self.admin = User.objects.create(
            email='r3-admin@example.com', password_hash=hash_password(PASSWORD),
            role='admin', is_super_admin=True,
        )

    def order(self):
        self.client.force_authenticate(user=self.customer_user)
        response = self.client.post(
            '/api/customer/requests',
            {'specialty': 'dish_install', 'description': 'الدش مش شغال', 'area_key': 'smouha'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.json()['request']['id']

    def accept(self, request_id):
        self.client.force_authenticate(user=self.tech_user)
        offer = next(
            o for o in self.client.get('/api/technician/incoming').json()['requests']
            if o['request_id'] == request_id
        )
        return self.client.post(f"/api/technician/broadcasts/{offer['broadcast_id']}/accept")

    def completed_job(self, amount=None):
        request_id = self.order()
        self.accept(request_id)
        pin = Request.objects.get(pk=request_id).start_pin
        self.client.post(f'/api/technician/requests/{request_id}/start', {'pin': pin})
        self.client.post(
            f'/api/technician/requests/{request_id}/complete',
            {'work_notes': 'ركبت الدش وظبطت الإشارة'}, format='json',
        )
        if amount is not None:
            for user, prefix in ((self.tech_user, 'technician'), (self.customer_user, 'customer')):
                self.client.force_authenticate(user=user)
                self.client.post(
                    f'/api/{prefix}/requests/{request_id}/report-payment',
                    {'amount': amount}, format='json',
                )
        return request_id


class FanOutTests(Round3Base):
    def test_offers_go_out_in_parallel_and_clean_up_dead_channels(self):
        from .services import notifications, push, telegram

        other_user = User.objects.create(
            email='r3-tech2@example.com', phone='+201000000703', password_hash='x',
            role='technician', telegram_chat_id='999',
        )
        PushSubscription.objects.create(user=self.tech_user, endpoint='https://fcm.googleapis.com/a', p256dh='k', auth='a')
        PushSubscription.objects.create(user=other_user, endpoint='https://fcm.googleapis.com/gone', p256dh='k', auth='a')

        def deliver(endpoint, *_):
            return push.GONE if endpoint.endswith('gone') else push.OK

        with patch.object(push, 'is_enabled', return_value=True), \
                patch.object(push, 'deliver', side_effect=deliver), \
                patch.object(telegram, 'is_configured', return_value=True), \
                patch.object(telegram, 'deliver_message', return_value=telegram.UNREACHABLE):
            result = notifications.fan_out([self.tech_user.pk, other_user.pk], {'title': 't', 'body': 'b'})

        self.assertEqual(result, {'push': 2, 'telegram': 1, 'dropped': 0})
        self.assertEqual(list(PushSubscription.objects.values_list('endpoint', flat=True)), ['https://fcm.googleapis.com/a'])
        other_user.refresh_from_db()
        self.assertIsNone(other_user.telegram_chat_id)

    def test_slow_sends_do_not_hold_the_request(self):
        from .services import notifications, push

        PushSubscription.objects.create(user=self.tech_user, endpoint='https://fcm.googleapis.com/slow', p256dh='k', auth='a')

        def slow(*_):
            time.sleep(2)
            return push.OK

        started = time.monotonic()
        with patch.object(push, 'is_enabled', return_value=True), patch.object(push, 'deliver', side_effect=slow):
            result = notifications.fan_out([self.tech_user.pk], {'title': 't'}, budget=0.2)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result['dropped'], 1)


@override_settings(STORAGES=DATABASE_STORAGES)
class UploadTests(Round3Base):
    def test_public_upload_type_comes_from_the_bytes(self):
        storage = storages['default']
        storage.save('technicians/profiles/tech-1/real.png', ContentFile(PNG_BYTES))
        storage.save('technicians/profiles/tech-1/evil.png', ContentFile(b'<svg onload="alert(1)"></svg>'))
        storage.save('technicians/profiles/tech-1/evil.svg', ContentFile(b'<svg onload="alert(1)"></svg>'))

        ok = self.client.get('/uploads/technicians/profiles/tech-1/real.png')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok['Content-Type'], 'image/png')
        self.assertIn('sandbox', ok['Content-Security-Policy'])
        self.assertEqual(self.client.get('/uploads/technicians/profiles/tech-1/evil.png').status_code, 404)
        self.assertEqual(self.client.get('/uploads/technicians/profiles/tech-1/evil.svg').status_code, 404)

    def test_pdf_must_start_and_end_like_a_pdf(self):
        from .upload_validators import validate_document_upload

        self.assertEqual(
            validate_document_upload(SimpleUploadedFile('a.pdf', PDF_BYTES)), 'application/pdf'
        )
        disguised = b'<html><!-- %PDF-1.4 --><script>alert(1)</script></html>'
        with self.assertRaises(ValidationError):
            validate_document_upload(SimpleUploadedFile('b.pdf', disguised))
        truncated = PDF_BYTES.replace(b'%%EOF', b'')
        with self.assertRaises(ValidationError):
            validate_document_upload(SimpleUploadedFile('c.pdf', truncated))

    def test_quota_refuses_new_uploads_when_full(self):
        from .upload_validators import save_private_upload

        StoredFile.objects.create(name='orders/big.bin', content=b'x', size=2 * 1024 * 1024)
        with patch.dict(os.environ, {'UPLOAD_QUOTA_MB': '2'}):
            with self.assertRaises(ApiError) as caught:
                save_private_upload(SimpleUploadedFile('p.png', PNG_BYTES), 'orders', 'image/png')
        self.assertEqual(caught.exception.status_code, 503)
        with patch.dict(os.environ, {'UPLOAD_QUOTA_MB': '50'}):
            path = save_private_upload(SimpleUploadedFile('p.png', PNG_BYTES), 'orders', 'image/png')
        self.assertTrue(path.startswith('/uploads/orders/'))

    def test_orphans_are_swept_after_a_day(self):
        from .services.uploads import sweep_orphan_uploads

        old = utc_now() - timedelta(hours=30)
        StoredFile.objects.create(name='orders/orphan.png', content=PNG_BYTES, size=len(PNG_BYTES), created_at=old)
        StoredFile.objects.create(name='orders/fresh.png', content=PNG_BYTES, size=len(PNG_BYTES))
        StoredFile.objects.create(name='orders/kept.png', content=PNG_BYTES, size=len(PNG_BYTES), created_at=old)
        request_id = self.order()
        Request.objects.filter(pk=request_id).update(order_photo_path='/uploads/orders/kept.png')

        result = sweep_orphan_uploads()
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(
            sorted(StoredFile.objects.values_list('name', flat=True)),
            ['orders/fresh.png', 'orders/kept.png'],
        )

    def test_admin_deleting_a_request_deletes_its_photo(self):
        StoredFile.objects.create(name='orders/job.png', content=PNG_BYTES, size=len(PNG_BYTES))
        request_id = self.order()
        Request.objects.filter(pk=request_id).update(order_photo_path='/uploads/orders/job.png')
        self.client.force_authenticate(user=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f'/api/admin/requests/{request_id}')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StoredFile.objects.filter(name='orders/job.png').exists())


class BackupPartsTests(Round3Base):
    def test_large_backup_is_split_and_restore_joins_the_parts(self):
        from django.core.management import call_command

        from .services.backup import build_backup, split_parts

        filename, data = build_backup()
        self.assertEqual(split_parts(filename, data), [(filename, data)])
        parts = split_parts(filename, data, part_bytes=max(1, len(data) // 3))
        self.assertGreaterEqual(len(parts), 3)
        self.assertEqual(b''.join(chunk for _, chunk in parts), data)
        self.assertTrue(parts[0][0].endswith(f'.part1-of-{len(parts)}'))

        Request.objects.all().delete()
        self.order()
        expected = Request.objects.count()
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name, chunk in reversed(parts):  # order on the command line must not matter
                path = os.path.join(tmp, name)
                with open(path, 'wb') as handle:
                    handle.write(chunk)
                paths.append(path)
            Request.objects.all().delete()
            call_command('restore_backup', *paths, yes=True, verbosity=0)
            with self.assertRaises(Exception):
                call_command('restore_backup', *paths[:-1], yes=True, verbosity=0)
        self.assertEqual(Request.objects.count(), expected - 1)
        self.assertEqual(json.loads(gzip.decompress(data))[0]['model'].split('.')[0], 'core')


class ExposureTests(Round3Base):
    def test_each_side_sees_only_its_fields(self):
        request_id = self.completed_job()
        self.client.force_authenticate(user=self.tech_user)
        self.client.post(f'/api/technician/requests/{request_id}/report-payment', {'amount': 120}, format='json')

        self.client.force_authenticate(user=self.customer_user)
        mine = self.client.get(f'/api/customer/requests/{request_id}').json()['request']
        for field in ('declined_technicians', 'payment_confirmed_by', 'dispute_resolved_by', 'start_pin_attempts'):
            self.assertNotIn(field, mine)

        self.client.force_authenticate(user=self.tech_user)
        theirs = self.client.get('/api/technician/requests?filter=history').json()['requests']
        job = next(r for r in theirs if r['id'] == request_id)
        for field in ('declined_technicians', 'guest_phone', 'start_pin', 'payment_confirmed_by'):
            self.assertNotIn(field, job)

        # The admin still sees the technician's figure while the customer has
        # not answered (the customer-side masking must not reach the admin).
        self.client.force_authenticate(user=self.admin)
        rows = self.client.get('/api/admin/requests').json()['requests']
        row = next(r for r in rows if r['id'] == request_id)
        self.assertEqual(row['technician_reported_amount'], 120)
        self.assertIn('declined_technicians', row)
        self.assertNotIn('start_pin', row)

    def test_public_technician_pages_have_no_coordinates(self):
        Technician.objects.filter(pk=self.technician.pk).update(latitude=31.2, longitude=29.9)
        listed = self.client.get('/api/public/technicians').json()['technicians'][0]
        self.assertNotIn('latitude', listed)
        detail = self.client.get(f'/api/public/technicians/{self.technician.pk}').json()
        self.assertNotIn('latitude', json.dumps(detail))


class SessionTests(Round3Base):
    def login(self):
        response = self.client.post(
            '/api/auth/login', {'identifier': self.customer_user.email, 'password': PASSWORD}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        return response.json()['token']

    def test_logout_ends_the_access_token(self):
        token = self.login()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(self.client.get('/api/auth/me').status_code, 200)
        self.assertEqual(self.client.post('/api/auth/logout', {}, format='json').status_code, 200)
        self.assertTrue(RevokedAccessToken.objects.exists())
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)
        # A fresh sign-in still works.
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + self.login())
        self.assertEqual(self.client.get('/api/auth/me').status_code, 200)

    def test_unknown_accounts_cost_a_bcrypt_check_too(self):
        with patch('core.auth_backend._burn_bcrypt') as burn:
            response = self.client.post(
                '/api/auth/login', {'identifier': 'nobody@example.com', 'password': 'x' * 10}, format='json'
            )
        self.assertEqual(response.status_code, 401)
        burn.assert_called_once()

    def test_contacts_are_unique_in_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(email='dupe@example.com', phone=self.customer_user.phone, password_hash='x', role='customer')
        # Accounts without a phone are not affected.
        User.objects.create(email='nophone1@example.com', phone=None, password_hash='x', role='customer')
        User.objects.create(email='nophone2@example.com', phone=None, password_hash='x', role='customer')


class CounterTests(APITestCase):
    def test_throttle_counts_once_per_call_and_stops_at_the_limit(self):
        from .ratelimit import throttle

        request = RequestFactory().get('/', REMOTE_ADDR='8.8.8.8')
        for _ in range(3):
            throttle(request, 'unit', limit=3, window_seconds=60)
        with self.assertRaises(ApiError):
            throttle(request, 'unit', limit=3, window_seconds=60)
        self.assertEqual(RateCounter.objects.get().count, 3)
        # count=False only looks.
        with self.assertRaises(ApiError):
            throttle(request, 'unit', limit=3, window_seconds=60, count=False)
        throttle(request, 'other', limit=3, window_seconds=60, count=False)
        self.assertEqual(RateCounter.objects.count(), 1)


class MoneyAndCancelTests(Round3Base):
    def test_cancel_releases_the_technician_who_accepted(self):
        request_id = self.order()
        self.assertEqual(self.accept(request_id).status_code, 200)
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'busy')

        self.client.force_authenticate(user=self.customer_user)
        self.assertEqual(self.client.post(f'/api/customer/requests/{request_id}/cancel').status_code, 200)
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')
        self.assertEqual(self.client.post(f'/api/customer/requests/{request_id}/cancel').status_code, 400)

        from .services.broadcast import cancel_request

        self.assertIsNone(cancel_request(request_id, ('pending',)))

    def test_refund_takes_back_the_commission_once(self):
        request_id = self.completed_job(amount=300)
        wallet = TechnicianWallet.objects.get(technician=self.technician)
        self.assertEqual(wallet.total_debt, 60)

        self.client.force_authenticate(user=self.admin)
        first = self.client.post(f'/api/admin/payments/{request_id}/refund')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['reversed'], 60)
        wallet.refresh_from_db()
        self.assertEqual(wallet.total_debt, 0)
        again = self.client.post(f'/api/admin/payments/{request_id}/refund')
        self.assertTrue(again.json()['already_refunded'])
        wallet.refresh_from_db()
        self.assertEqual(wallet.total_debt, 0)
        self.assertEqual(
            WalletTransaction.objects.filter(request_id=request_id, type='adjustment').count(), 1
        )

    def test_refund_needs_a_completed_job(self):
        request_id = self.order()
        self.client.force_authenticate(user=self.admin)
        self.assertEqual(self.client.post(f'/api/admin/payments/{request_id}/refund').status_code, 400)

    def test_no_report_after_a_dispute_or_refund(self):
        request_id = self.completed_job()
        self.client.force_authenticate(user=self.tech_user)
        self.client.post(f'/api/technician/requests/{request_id}/report-payment', {'amount': 100}, format='json')
        self.client.force_authenticate(user=self.customer_user)
        self.client.post(f'/api/customer/requests/{request_id}/report-payment', {'amount': 200}, format='json')
        self.assertEqual(Request.objects.get(pk=request_id).amount_status, 'disputed')


class PagePolicyTests(Round3Base):
    def test_inline_scripts_run_only_with_this_requests_nonce(self):
        response = self.client.get('/order.html')
        policy = response['Content-Security-Policy']
        script_src = next(part for part in policy.split(';') if part.strip().startswith('script-src '))
        self.assertNotIn('unsafe-inline', script_src)
        nonce = re.search(r"'nonce-([^']+)'", script_src).group(1)
        html = response.content.decode('utf-8')
        inline = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>', html)
        self.assertTrue(inline)
        for tag in inline:
            self.assertIn(f'nonce="{nonce}"', tag)
        self.assertNotRegex(html, r'\son[a-z]+="')
        # A new request gets a new nonce.
        self.assertNotIn(nonce, self.client.get('/order.html')['Content-Security-Policy'])

    def test_social_links_must_be_https(self):
        from .site_config import safe_https_url

        self.client.force_authenticate(user=self.admin)
        bad = self.client.put(
            '/api/admin/settings', {'settings': {'facebook_url': 'javascript:alert(1)'}}, format='json'
        )
        self.assertEqual(bad.status_code, 400)
        good = self.client.put(
            '/api/admin/settings', {'settings': {'facebook_url': 'https://facebook.com/dashawy'}}, format='json'
        )
        self.assertEqual(good.status_code, 200)
        self.assertEqual(safe_https_url('javascript:alert(1)'), '')
        self.assertEqual(safe_https_url('http://x.com'), '')
        self.assertEqual(safe_https_url('https://x.com/a" onmouseover="x'), '')

    def test_super_admin_cannot_delete_their_own_account(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(f'/api/admin/admins/{self.admin.pk}')
        self.assertEqual(response.status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
