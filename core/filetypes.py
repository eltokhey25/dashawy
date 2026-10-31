"""Identify the few file formats Dashawy accepts from their magic bytes.

Uploads are limited to JPEG, PNG, WebP and PDF, so a short signature check is
enough and avoids depending on the system ``libmagic`` library, which is not
available on serverless hosts such as Vercel.
"""

# How many leading bytes callers should read before calling detect_mime().
HEADER_BYTES = 2048


def detect_mime(header):
    """Return the MIME type for ``header`` (the file's first bytes), or ''."""
    data = bytes(header or b'')
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    # libmagic tolerates a few bytes before the PDF marker; so do PDF readers.
    if b'%PDF-' in data[:1024]:
        return 'application/pdf'
    return ''


def detect_file_mime(handle):
    """Detect the MIME type of an open binary file, leaving it at offset 0."""
    handle.seek(0)
    header = handle.read(HEADER_BYTES)
    handle.seek(0)
    return detect_mime(header)
