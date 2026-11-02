"""Daily off-site backup of the database.

Neon keeps only a short restore window on its free tier, and the SQLite copy
that ``scripts/backup.sh`` used to take no longer exists. ``build_backup``
dumps every ``core`` table (uploads included, since they live in the
``stored_files`` table) plus the Django admin accounts as one gzipped JSON
fixture; ``run_daily_backup`` sends that file to the admins' Telegram so a copy
lives outside the hosting provider.

Restore: ``python manage.py restore_backup <file.json.gz>`` (see README).
"""

import gzip
import io
import logging
import os
from datetime import datetime, timezone

from django.core.management import call_command

from ..models import User

logger = logging.getLogger(__name__)

# Everything that holds data; Django's own bookkeeping tables are rebuilt by
# migrate and are not worth carrying.
FIXTURE_LABELS = ['core', 'auth.group', 'auth.user']
# Telegram's document limit is 50 MB. A larger backup is sent as several
# parts of at most PART_BYTES; restore_backup joins them again.
PART_BYTES = 45 * 1024 * 1024


def build_backup():
    """Return ``(filename, gzipped_json_bytes)`` for the whole dataset."""
    buffer = io.StringIO()
    call_command(
        'dumpdata',
        *FIXTURE_LABELS,
        format='json',
        natural_foreign=True,
        indent=None,
        stdout=buffer,
    )
    raw = buffer.getvalue().encode('utf-8')
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')
    data = gzip.compress(raw, compresslevel=9)
    return f'dashawy-backup-{stamp}.json.gz', data


def split_parts(filename, data, part_bytes=PART_BYTES):
    """``[(name, bytes), ...]``: the file itself, or numbered byte slices of it.

    Joining the slices in order (``cat a.part1-of-3 a.part2-of-3 ...``)
    gives the original .json.gz back; ``restore_backup`` accepts the parts.
    """
    if len(data) <= part_bytes:
        return [(filename, data)]
    chunks = [data[i:i + part_bytes] for i in range(0, len(data), part_bytes)]
    total = len(chunks)
    return [(f'{filename}.part{n}-of-{total}', chunk) for n, chunk in enumerate(chunks, 1)]


def backup_recipients():
    """Telegram chat ids the backup goes to.

    Only super admins who linked Telegram, or the chat ids in the
    ``BACKUP_TELEGRAM_CHAT_ID`` environment variable (comma-separated). This is
    deliberately not an admin-editable setting: the file holds every password
    hash and identity document, so a section admin must not be able to point
    it at themselves.
    """
    configured = os.environ.get('BACKUP_TELEGRAM_CHAT_ID', '').strip()
    if configured:
        return [part.strip() for part in configured.split(',') if part.strip()]
    return list(
        User.objects.filter(role='admin', is_super_admin=True, blocked=False)
        .exclude(telegram_chat_id__isnull=True)
        .exclude(telegram_chat_id='')
        .values_list('telegram_chat_id', flat=True)
    )


def run_daily_backup():
    """Build the backup and send it to every recipient. Never raises."""
    from . import telegram

    try:
        filename, data = build_backup()
    except Exception:  # noqa: BLE001
        logger.exception('Backup could not be built')
        return {'ok': False, 'reason': 'build_failed'}

    recipients = backup_recipients()
    if not recipients:
        logger.warning('Backup built (%d bytes) but nobody is linked to receive it', len(data))
        return {'ok': False, 'reason': 'no_recipient', 'filename': filename, 'bytes': len(data)}

    parts = split_parts(filename, data)
    sent = 0
    for chat_id in recipients:
        delivered = 0
        for index, (part_name, chunk) in enumerate(parts, 1):
            caption = (
                f'📦 نسخة احتياطية يومية من قاعدة بيانات دشاوي\n'
                f'{part_name} · {len(chunk) / 1024:.0f} KB\n'
                + (
                    f'الجزء {index} من {len(parts)}: احتفظ بكل الأجزاء مع بعض.\n'
                    if len(parts) > 1
                    else ''
                )
                + 'احتفظ بالملف في مكان آمن.'
            )
            if telegram.send_document(chat_id, part_name, chunk, caption):
                delivered += 1
        if delivered == len(parts):
            sent += 1
    logger.info('Backup %s (%d parts) sent to %d/%d recipients', filename, len(parts), sent, len(recipients))
    return {
        'ok': sent > 0,
        'filename': filename,
        'bytes': len(data),
        'parts': len(parts),
        'sent': sent,
        'recipients': len(recipients),
    }
