"""Remove uploaded files when the row that owned them goes away.

Identity documents, order photos, avatars and portfolio photos are stored
by path on their rows. Deleting a technician, a customer (and so their
requests) or erasing an account under a data-subject request must take the
files with it, or the backup keeps carrying a national ID whose owner asked
for it to be gone. Cascades fire ``post_delete`` for every collected row, so
one receiver per model covers the admin delete, the erase and the cascade.

Files are removed after the surrounding transaction commits: a rolled-back
delete must not lose its files.
"""

import logging

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Request, Technician, TechnicianPortfolio, TechnicianProfile
from .upload_validators import resolve_stored_upload

log = logging.getLogger(__name__)

FILE_FIELDS = {
    Technician: ('criminal_record_path', 'national_id_path'),
    Request: ('order_photo_path',),
    TechnicianProfile: ('profile_image_path',),
    TechnicianPortfolio: ('image_path',),
}


def delete_stored_files(paths):
    """Best effort: a missing or unreadable file never fails the delete."""
    for stored in paths:
        name = resolve_stored_upload(stored)
        if not name:
            continue
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001
            log.warning('could not delete upload %s', name, exc_info=True)


def _schedule(instance, fields):
    paths = [getattr(instance, field, None) for field in fields]
    paths = [p for p in paths if p]
    if paths:
        transaction.on_commit(lambda: delete_stored_files(paths))


for _model, _fields in FILE_FIELDS.items():

    @receiver(post_delete, sender=_model, weak=False, dispatch_uid=f'delete-files-{_model.__name__}')
    def _on_delete(sender, instance, fields=_fields, **kwargs):
        _schedule(instance, fields)
