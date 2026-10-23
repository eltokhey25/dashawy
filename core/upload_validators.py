"""Validation helpers for private file uploads.

Technician identity documents must be checked by their magic bytes, not by the
client-supplied filename or Content-Type. The validated files belong under
``MEDIA_ROOT/documents/``; there is deliberately no ``MEDIA_URL`` route for
that tree, so documents are only reachable through an authenticated view.
"""

import magic
from django.core.exceptions import ValidationError

# Documents accepted for technician onboarding.
ALLOWED_DOCUMENT_MIME_TYPES = frozenset(
    {
        'application/pdf',
        'image/jpeg',
        'image/png',
    }
)

MAX_DOCUMENT_SIZE = 5 * 1024 * 1024  # 5 MB per file
MAGIC_HEADER_BYTES = 2048


def validate_document_upload(uploaded_file):
    """Reject documents whose real content is not a JPEG, PNG, or PDF.

    Returns the detected MIME type so callers can derive a safe extension.
    """
    if uploaded_file is None:
        raise ValidationError('الملف مطلوب')

    size = getattr(uploaded_file, 'size', None)
    if size is not None and size > MAX_DOCUMENT_SIZE:
        raise ValidationError('حجم الملف يجب ألا يتجاوز 5 ميجابايت')

    try:
        uploaded_file.seek(0)
        header = uploaded_file.read(MAGIC_HEADER_BYTES)
        uploaded_file.seek(0)
        detected = magic.from_buffer(header, mime=True)
    except (OSError, ValueError, magic.MagicException) as exc:
        raise ValidationError('تعذر التحقق من نوع الملف') from exc

    if detected not in ALLOWED_DOCUMENT_MIME_TYPES:
        raise ValidationError(
            'يُسمح فقط بملفات JPEG أو PNG أو PDF للمستندات'
        )
    return detected
