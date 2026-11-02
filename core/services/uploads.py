"""Remove a user's uploaded files when their account goes.

The database rows cascade with the ``users`` row, but the files they point
at (identity documents, profile and portfolio photos, order photos) stayed in
storage — and therefore in every backup — which breaks the privacy policy's
erasure promise. Names are collected *before* the rows are deleted and the
files removed once the surrounding transaction commits.
"""

import logging

from django.core.files.storage import default_storage
from django.db import transaction

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
