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
    # A real PDF starts with its marker. Readers tolerate a little leading
    # whitespace or a BOM, so allow that and nothing else: accepting the
    # marker anywhere in the first kilobyte let an HTML or script file pass
    # as a "PDF" by mentioning ``%PDF-`` in a comment.
    if data.lstrip(b'\xef\xbb\xbf \t\r\n\x00')[:5] == b'%PDF-':
        return 'application/pdf'
    return ''


def looks_like_complete_pdf(handle):
    """A PDF ends with ``%%EOF`` (possibly followed by whitespace)."""
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(max(0, size - 2048))
    tail = handle.read()
    handle.seek(0)
    return b'%%EOF' in tail


def detect_file_mime(handle):
    """Detect the MIME type of an open binary file, leaving it at offset 0."""
    handle.seek(0)
    header = handle.read(HEADER_BYTES)
    handle.seek(0)
    return detect_mime(header)
