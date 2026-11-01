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
from datetime import datetime, timezone

from django.core.management import call_command

from ..models import Setting, User

logger = logging.getLogger(__name__)

# Everything that holds data; Django's own bookkeeping tables are rebuilt by
# migrate and are not worth carrying.
FIXTURE_LABELS = ['core', 'auth.group', 'auth.user']
# Telegram's document limit is 50 MB; refuse well before it.
MAX_BYTES = 45 * 1024 * 1024


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
    if len(data) > MAX_BYTES:
        raise ValueError(f'backup is {len(data)} bytes, over the {MAX_BYTES} limit')
    return f'dashawy-backup-{stamp}.json.gz', data


def backup_recipients():
    """Telegram chat ids the backup goes to.

    The ``backup_telegram_chat_id`` setting wins (comma-separated); otherwise
    every admin who linked Telegram.
    """
    configured = Setting.objects.filter(key='backup_telegram_chat_id').values_list(
        'value', flat=True
    ).first()
    if configured and configured.strip():
        return [part.strip() for part in configured.split(',') if part.strip()]
    return list(
        User.objects.filter(role='admin', blocked=False)
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

    caption = (
        f'📦 نسخة احتياطية يومية من قاعدة بيانات دشاوي\n'
        f'{filename} · {len(data) / 1024:.0f} KB\n'
        'احتفظ بالملف في مكان آمن.'
    )
    sent = 0
    for chat_id in recipients:
        if telegram.send_document(chat_id, filename, data, caption):
            sent += 1
    logger.info('Backup %s sent to %d/%d recipients', filename, sent, len(recipients))
    return {
        'ok': sent > 0,
        'filename': filename,
        'bytes': len(data),
        'sent': sent,
        'recipients': len(recipients),
    }
