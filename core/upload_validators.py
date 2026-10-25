"""Validation helpers for private file uploads.

Technician identity documents must be checked by their magic bytes, not by the
client-supplied filename or Content-Type. The validated files belong under
``MEDIA_ROOT/uploads/technicians/``; only the ``profiles`` and ``portfolio``
subtrees are exposed publicly, so identity documents are reachable only through
the admin-only streaming view.
"""

import magic
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

# Documents accepted for technician onboarding.
ALLOWED_DOCUMENT_MIME_TYPES = frozenset(
    {
        'application/pdf',
        'image/jpeg',
        'image/png',
    }
)

# A customer's optional order photo is an image only. WebP is accepted because
# modern phone cameras/browsers produce it; PDFs are not.
ALLOWED_ORDER_IMAGE_MIME_TYPES = frozenset(
    {
        'image/jpeg',
        'image/png',
        'image/webp',
    }
)

MAX_DOCUMENT_SIZE = 5 * 1024 * 1024  # 5 MB per file
MAX_ORDER_IMAGE_SIZE = 5 * 1024 * 1024
MAGIC_HEADER_BYTES = 2048

MIME_EXTENSION = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'application/pdf': 'pdf',
}


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


def validate_order_image(uploaded_file):
    """Reject an order photo whose real content is not JPEG/PNG/WebP."""
    if uploaded_file is None:
        raise ValidationError('الملف مطلوب')

    size = getattr(uploaded_file, 'size', None)
    if size is not None and size > MAX_ORDER_IMAGE_SIZE:
        raise ValidationError('حجم الصورة يجب ألا يتجاوز 5 ميجابايت')

    try:
        uploaded_file.seek(0)
        header = uploaded_file.read(MAGIC_HEADER_BYTES)
        uploaded_file.seek(0)
        detected = magic.from_buffer(header, mime=True)
    except (OSError, ValueError, magic.MagicException) as exc:
        raise ValidationError('تعذر التحقق من نوع الصورة') from exc

    if detected not in ALLOWED_ORDER_IMAGE_MIME_TYPES:
        raise ValidationError('يُسمح فقط بصور JPEG أو PNG أو WebP')
    return detected


def random_filename(mime_type):
    """A non-guessable filename derived from the *detected* content type."""
    import secrets

    extension = MIME_EXTENSION.get(mime_type, 'bin')
    stamp = timezone.now().strftime('%Y%m%d%H%M%S')
    return f'{stamp}-{secrets.token_hex(16)}.{extension}'


def save_private_upload(uploaded_file, relative_dir, mime_type):
    """Write an upload below MEDIA_ROOT with a random name.

    Returns the public-facing path stored in the database
    (``/uploads/<relative_dir>/<random>``). The file itself is never served by
    a blanket static route; callers expose it through an authorised view.
    """
    from pathlib import Path

    safe_dir = Path(relative_dir)
    if safe_dir.is_absolute() or '..' in safe_dir.parts:
        raise ValidationError('مسار الملف غير صالح')

    target_dir = Path(settings.MEDIA_ROOT) / safe_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    name = random_filename(mime_type)
    target = target_dir / name
    with open(target, 'wb') as handle:
        for chunk in uploaded_file.chunks():
            handle.write(chunk)
    return f'/uploads/{safe_dir.as_posix().strip("/")}/{name}'
