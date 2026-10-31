"""Database-backed upload storage and magic-byte detection (Vercel mode)."""

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import TestCase, override_settings

from .filetypes import detect_mime
from .models import StoredFile
from .storage import DatabaseStorage
from .tests import JPEG_BYTES, PDF_BYTES, PNG_BYTES
from .upload_validators import open_stored_upload, resolve_stored_upload

DATABASE_STORAGES = {
    'default': {'BACKEND': 'core.storage.DatabaseStorage'},
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}


class DetectMimeTests(TestCase):
    def test_accepted_formats(self):
        self.assertEqual(detect_mime(JPEG_BYTES), 'image/jpeg')
        self.assertEqual(detect_mime(PNG_BYTES), 'image/png')
        self.assertEqual(detect_mime(PDF_BYTES), 'application/pdf')
        self.assertEqual(detect_mime(b'RIFF\x00\x00\x00\x00WEBPVP8 '), 'image/webp')

    def test_other_content_is_rejected(self):
        self.assertEqual(detect_mime(b'<svg xmlns="http://www.w3.org/2000/svg"/>'), '')
        self.assertEqual(detect_mime(b'GIF89a'), '')
        self.assertEqual(detect_mime(b''), '')


class DatabaseStorageTests(TestCase):
    def setUp(self):
        self.storage = DatabaseStorage()

    def test_round_trip(self):
        name = self.storage.save('technicians/profiles/tech-1/a.png', ContentFile(PNG_BYTES))
        self.assertEqual(name, 'technicians/profiles/tech-1/a.png')
        self.assertTrue(self.storage.exists(name))
        self.assertEqual(self.storage.size(name), len(PNG_BYTES))
        with self.storage.open(name) as handle:
            self.assertEqual(handle.read(), PNG_BYTES)
        self.assertEqual(
            self.storage.listdir('technicians/profiles'), (['tech-1'], [])
        )
        self.storage.delete(name)
        self.assertFalse(StoredFile.objects.exists())

    def test_name_cannot_escape(self):
        from django.core.exceptions import SuspiciousFileOperation

        with self.assertRaises(SuspiciousFileOperation):
            self.storage.exists('../etc/passwd')
        self.assertIsNone(resolve_stored_upload('/uploads/../secret'))


@override_settings(STORAGES=DATABASE_STORAGES)
class DatabaseStorageServingTests(TestCase):
    def setUp(self):
        storages['default'].save('technicians/profiles/tech-9/p.png', ContentFile(PNG_BYTES))
        storages['default'].save('technicians/user-9/id.pdf', ContentFile(PDF_BYTES))

    def test_public_profile_image_is_served_from_database(self):
        response = self.client.get('/uploads/technicians/profiles/tech-9/p.png')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(b''.join(response.streaming_content), PNG_BYTES)

    def test_identity_document_is_not_public(self):
        response = self.client.get('/uploads/technicians/user-9/id.pdf')
        self.assertEqual(response.status_code, 404)

    def test_open_stored_upload_sniffs_type(self):
        handle, mime = open_stored_upload('technicians/user-9/id.pdf')
        with handle:
            self.assertEqual(mime, 'application/pdf')
        self.assertIsNone(open_stored_upload('technicians/user-9/missing.pdf'))
