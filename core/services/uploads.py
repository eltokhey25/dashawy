"""Remove a user's uploaded files when their account goes.

The database rows cascade with the ``users`` row, but the files they point
at (identity documents, profile and portfolio photos, order photos) stayed in
storage — and therefore in every backup — which breaks the privacy policy's
erasure promise. Names are collected *before* the rows are deleted and the
files removed once the surrounding transaction commits.
"""

import logging
import os
from datetime import timedelta

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Sum

from ..models import Customer, Request, Technician, TechnicianPortfolio, TechnicianProfile
from ..upload_validators import resolve_stored_upload

log = logging.getLogger(__name__)


def user_upload_names(user_id):
    """Storage names of every file that belongs to this account."""
    stored = []
    tech = Technician.objects.filter(user_id=user_id).values('pk', 'criminal_record_path', 'national_id_path').first()
    if tech:
        stored += [tech['criminal_record_path'], tech['national_id_path']]
        stored += TechnicianProfile.objects.filter(technician_id=tech['pk']).values_list(
            'profile_image_path', flat=True
        )
        stored += TechnicianPortfolio.objects.filter(technician_id=tech['pk']).values_list(
            'image_path', flat=True
        )
        # Erasing a technician deletes the orders they worked (see
        # AdminUserEraseView); their photos would be orphaned otherwise.
        stored += Request.objects.filter(technician_id=tech['pk']).values_list(
            'order_photo_path', flat=True
        )
    customer = Customer.objects.filter(user_id=user_id).values('pk').first()
    if customer:
        stored += Request.objects.filter(customer_id=customer['pk']).values_list(
            'order_photo_path', flat=True
        )
    names = []
    for path in stored:
        name = resolve_stored_upload(path) if path else None
        if name and name not in names:
            names.append(name)
    return names


def _delete_all(names):
    for name in names:
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 - a missing file must not undo the erasure
            log.warning('Could not delete upload %s', name, exc_info=True)


def delete_user_uploads(user_id):
    """Queue the account's files for deletion after the current transaction."""
    names = user_upload_names(user_id)
    if names:
        transaction.on_commit(lambda: _delete_all(names))
    return names


# ---------------------------------------------------------------------------
# Storage quota and orphan sweep (files kept in the database)
# ---------------------------------------------------------------------------

# Technician registration and order photos are open to the public, so stored
# bytes are capped: the database (Neon) is small, and every stored byte is
# also in the daily backup.
DEFAULT_QUOTA_MB = 150
ORPHAN_GRACE_HOURS = 24


def _uses_database_storage():
    from ..storage import DatabaseStorage

    return isinstance(getattr(default_storage, '_wrapped', default_storage), DatabaseStorage)


def upload_quota_bytes():
    try:
        mb = float(os.environ.get('UPLOAD_QUOTA_MB', '') or DEFAULT_QUOTA_MB)
    except ValueError:
        mb = DEFAULT_QUOTA_MB
    return int(max(mb, 1) * 1024 * 1024)


def stored_bytes():
    from ..models import StoredFile

    return int(StoredFile.objects.aggregate(total=Sum('size'))['total'] or 0)


def ensure_upload_quota(incoming_bytes):
    """Refuse a new upload once the stored files reach the quota."""
    if not _uses_database_storage():
        return
    if stored_bytes() + int(incoming_bytes or 0) > upload_quota_bytes():
        from ..exceptions import ApiError

        log.error('Upload refused: storage quota of %d bytes reached', upload_quota_bytes())
        raise ApiError(
            'مساحة تخزين الملفات ممتلئة حالياً. حاول تاني بعد شوية أو تواصل معنا.', 503
        )


def referenced_upload_names():
    """Every storage name some row still points at."""
    from ..models import Request

    stored = []
    stored += Technician.objects.values_list('criminal_record_path', flat=True)
    stored += Technician.objects.values_list('national_id_path', flat=True)
    stored += TechnicianProfile.objects.values_list('profile_image_path', flat=True)
    stored += TechnicianPortfolio.objects.values_list('image_path', flat=True)
    stored += Request.objects.values_list('order_photo_path', flat=True)
    names = set()
    for path in stored:
        name = resolve_stored_upload(path) if path else None
        if name:
            names.add(name)
    return names


def sweep_orphan_uploads(now=None):
    """Delete stored files no row references, once they are a day old.

    An upload saved during a request that then failed (or a row deleted
    without its file) would otherwise sit in the database and every backup
    forever. The grace period covers a request still in flight.
    """
    if not _uses_database_storage():
        return {'deleted': 0, 'bytes': 0}
    from ..models import StoredFile, utc_now

    cutoff = (now or utc_now()) - timedelta(hours=ORPHAN_GRACE_HOURS)
    keep = referenced_upload_names()
    orphans = [
        (pk, size)
        for pk, name, size in StoredFile.objects.filter(created_at__lte=cutoff).values_list(
            'pk', 'name', 'size'
        )
        if name not in keep
    ]
    if orphans:
        StoredFile.objects.filter(pk__in=[pk for pk, _ in orphans]).delete()
    freed = sum(size or 0 for _, size in orphans)
    if orphans:
        log.info('Removed %d orphan uploads (%d bytes)', len(orphans), freed)
    return {'deleted': len(orphans), 'bytes': freed}


def delete_upload_later(stored_path):
    """Delete one stored file once the current transaction commits."""
    name = resolve_stored_upload(stored_path) if stored_path else None
    if name:
        transaction.on_commit(lambda: _delete_all([name]))
