"""The daily backup: a loadable fixture, sent to Telegram, guarded by the cron secret."""

import gzip
import json
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import override_settings
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import Customer, Request, Setting, StoredFile, User
from .services.backup import backup_recipients, build_backup, run_daily_backup
from .tests import PNG_BYTES

PASSWORD = 'TesterPass123'
DATABASE_STORAGES = {
    'default': {'BACKEND': 'core.storage.DatabaseStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class BackupTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create(
            email='backup-admin@example.com', password_hash=hash_password(PASSWORD),
            role='admin', is_super_admin=True, telegram_chat_id='777',
        )
        customer_user = User.objects.create(
            email='backup-customer@example.com', phone='+201000000951',
            password_hash=hash_password(PASSWORD), role='customer',
        )
        customer = Customer.objects.create(user=customer_user, name='عميل النسخة', phone='+201000000951')
        Request.objects.create(customer=customer, specialty='dish_install', description='x', status='pending')

    @override_settings(STORAGES=DATABASE_STORAGES)
    def test_backup_is_a_complete_loadable_fixture(self):
        storages['default'].save('orders/backup-test.png', ContentFile(PNG_BYTES))
        filename, data = build_backup()
        self.assertRegex(filename, r'^dashawy-backup-\d{4}-\d{2}-\d{2}_\d{4}\.json\.gz$')
        rows = json.loads(gzip.decompress(data).decode('utf-8'))
        models = {row['model'] for row in rows}
        self.assertIn('core.user', models)
        self.assertIn('core.request', models)
        self.assertIn('core.storedfile', models)
        stored = next(r for r in rows if r['model'] == 'core.storedfile')
        self.assertEqual(stored['fields']['name'], 'orders/backup-test.png')

        # A restore brings a deleted row back.
        Request.objects.all().delete()
        StoredFile.objects.all().delete()
        with patch('builtins.input', return_value='yes'):
            from django.core.management import call_command
            import tempfile, os

            with tempfile.NamedTemporaryFile(suffix='.json.gz', delete=False) as handle:
                handle.write(data)
            try:
                call_command('restore_backup', handle.name, yes=True, verbosity=0)
            finally:
                os.unlink(handle.name)
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(StoredFile.objects.count(), 1)

    def test_recipients_prefer_the_setting_over_linked_admins(self):
        self.assertEqual(backup_recipients(), ['777'])
        Setting.objects.create(key='backup_telegram_chat_id', value='111, 222')
        self.assertEqual(backup_recipients(), ['111', '222'])

    @patch('core.services.telegram.send_document', return_value=True)
    def test_daily_backup_goes_to_telegram(self, send_document):
        result = run_daily_backup()
        self.assertTrue(result['ok'])
        self.assertEqual(result['sent'], 1)
        chat_id, filename, data, caption = send_document.call_args.args
        self.assertEqual(chat_id, '777')
        self.assertTrue(filename.endswith('.json.gz'))
        self.assertIn('نسخة احتياطية', caption)
        self.assertEqual(json.loads(gzip.decompress(data))[0]['model'].split('.')[0], 'core')

    @patch('core.services.telegram.send_document', return_value=True)
    def test_no_linked_admin_means_no_send(self, send_document):
        User.objects.filter(pk=self.admin.pk).update(telegram_chat_id=None)
        result = run_daily_backup()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'no_recipient')
        send_document.assert_not_called()

    @patch('core.services.telegram.send_document', return_value=True)
    def test_cron_endpoint_requires_the_secret(self, send_document):
        with patch.dict('os.environ', {'CRON_SECRET': 'top-secret-value'}):
            self.assertEqual(self.client.get('/api/cron/backup').status_code, 404)
            wrong = self.client.get('/api/cron/backup', HTTP_AUTHORIZATION='Bearer nope')
            self.assertEqual(wrong.status_code, 404)
            ok = self.client.get('/api/cron/backup', HTTP_AUTHORIZATION='Bearer top-secret-value')
            self.assertEqual(ok.status_code, 200)
            self.assertTrue(ok.json()['ok'])
        with patch.dict('os.environ', {'CRON_SECRET': ''}):
            unset = self.client.get('/api/cron/backup', HTTP_AUTHORIZATION='Bearer top-secret-value')
            self.assertEqual(unset.status_code, 404)

    def test_super_admin_can_download_and_others_cannot(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get('/api/admin/backup')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/gzip')
        self.assertIn('dashawy-backup-', response['Content-Disposition'])
        self.assertTrue(gzip.decompress(response.content).startswith(b'['))

        plain_admin = User.objects.create(
            email='backup-plain@example.com', password_hash='x', role='admin'
        )
        self.client.force_authenticate(user=plain_admin)
        self.assertEqual(self.client.get('/api/admin/backup').status_code, 403)
