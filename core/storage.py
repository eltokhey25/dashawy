"""A Django storage backend that keeps uploaded files in the database.

Serverless hosts (Vercel) have no persistent disk, so ``uploads/`` would be
wiped between requests. When ``UPLOAD_STORAGE=database`` the default storage is
this class and every upload becomes a ``StoredFile`` row. Names keep the same
``technicians/...`` / ``orders/...`` shape as on disk, so the paths saved in the
other tables do not change.
"""

import posixpath
from io import BytesIO

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile, File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


def _clean_name(name):
    clean = posixpath.normpath(str(name or '').replace('\\', '/')).lstrip('/')
    if not clean or clean == '.' or clean.startswith('../') or clean == '..':
        raise SuspiciousFileOperation(f'Invalid storage name: {name!r}')
    return clean


@deconstructible
class DatabaseStorage(Storage):
    def _model(self):
        from .models import StoredFile

        return StoredFile

    def _open(self, name, mode='rb'):
        if 'w' in mode or 'a' in mode or '+' in mode:
            raise ValueError('DatabaseStorage files are read-only once saved.')
        row = self._model().objects.filter(name=_clean_name(name)).first()
        if row is None:
            raise FileNotFoundError(name)
        return File(BytesIO(bytes(row.content)), name=row.name)

    def _save(self, name, content):
        clean = _clean_name(name)
        if hasattr(content, 'chunks'):
            data = b''.join(content.chunks())
        else:
            data = content.read()
        self._model().objects.create(name=clean, content=data, size=len(data))
        return clean

    def exists(self, name):
        return self._model().objects.filter(name=_clean_name(name)).exists()

    def delete(self, name):
        self._model().objects.filter(name=_clean_name(name)).delete()

    def size(self, name):
        row = (
            self._model().objects.filter(name=_clean_name(name))
            .values('size')
            .first()
        )
        if row is None:
            raise FileNotFoundError(name)
        return row['size']

    def listdir(self, path):
        prefix = _clean_name(path).rstrip('/') + '/' if path else ''
        dirs, files = set(), []
        names = self._model().objects.filter(name__startswith=prefix).values_list(
            'name', flat=True
        )
        for full in names:
            rest = full[len(prefix):]
            head, sep, _ = rest.partition('/')
            if sep:
                dirs.add(head)
            else:
                files.append(rest)
        return sorted(dirs), sorted(files)

    def url(self, name):
        # Callers build their own authorised URLs; there is no MEDIA_URL.
        return '/uploads/' + _clean_name(name)


def save_bytes(storage, name, data):
    """Save raw bytes under exactly ``name`` (used by the import command)."""
    if storage.exists(name):
        storage.delete(name)
    return storage.save(name, ContentFile(data))
