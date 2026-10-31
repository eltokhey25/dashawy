"""
Copy the local SQLite database and uploads into the hosted Postgres database.

Run once from your own machine before (or right after) the first Vercel
deploy, with ``DATABASE_URL`` pointing at the Neon database::

    DATABASE_URL='postgresql://...' python manage.py import_sqlite

It applies the migrations, creates the cache table, copies every row of the
``core`` app and Django's admin accounts, resets the Postgres id sequences and
stores each file under ``uploads/`` in the ``stored_files`` table. The target
database must be empty; pass ``--skip-data`` or ``--skip-files`` to run only
part of it.
"""

import io
import tempfile
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from core.models import StoredFile
from core.storage import DatabaseStorage, save_bytes

LEGACY_ALIAS = 'legacy_sqlite'
DATA_LABELS = ['core', 'auth.group', 'auth.user']


class Command(BaseCommand):
    help = 'Import the SQLite database and uploads into the DATABASE_URL database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sqlite',
            default=str(Path(settings.BASE_DIR) / 'database' / 'fixrequest.db'),
            help='Path to the SQLite database to copy.',
        )
        parser.add_argument(
            '--uploads',
            default=str(settings.MEDIA_ROOT),
            help='Uploads directory to copy into stored_files.',
        )
        parser.add_argument('--skip-data', action='store_true')
        parser.add_argument('--skip-files', action='store_true')

    def handle(self, *args, **options):
        if connections['default'].vendor != 'postgresql':
            raise CommandError(
                'DATABASE_URL must point at the Postgres database to import into.'
            )

        call_command('migrate', interactive=False, verbosity=1)
        call_command('createcachetable', verbosity=1)

        if not options['skip_data']:
            self._import_rows(Path(options['sqlite']))
        if not options['skip_files']:
            self._import_files(Path(options['uploads']))
        self.stdout.write(self.style.SUCCESS('Import finished.'))

    def _import_rows(self, sqlite_path):
        if not sqlite_path.is_file():
            raise CommandError(f'SQLite database not found: {sqlite_path}')

        user_model = apps.get_model('core', 'User')
        if user_model.objects.using('default').exists():
            raise CommandError(
                'The Postgres database already has users; refusing to import '
                'twice. Use --skip-data to import only the files.'
            )

        legacy = dict(connections.databases['default'])
        legacy.update(
            ENGINE='django.db.backends.sqlite3',
            NAME=str(sqlite_path),
            OPTIONS={'timeout': 20},
        )
        connections.databases[LEGACY_ALIAS] = legacy

        dump = io.StringIO()
        call_command(
            'dumpdata',
            *DATA_LABELS,
            database=LEGACY_ALIAS,
            exclude=['core.storedfile'],
            natural_foreign=True,
            format='json',
            stdout=dump,
        )
        connections[LEGACY_ALIAS].close()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / 'import.json'
            fixture.write_text(dump.getvalue(), encoding='utf-8')
            with transaction.atomic():
                call_command('loaddata', str(fixture), verbosity=1)

        sql = io.StringIO()
        call_command('sqlsequencereset', 'core', 'auth', stdout=sql, no_color=True)
        with connections['default'].cursor() as cursor:
            cursor.execute(sql.getvalue())
        self.stdout.write('Rows imported and id sequences reset.')

    def _import_files(self, uploads_dir):
        if not uploads_dir.is_dir():
            self.stdout.write(f'No uploads directory at {uploads_dir}; skipped.')
            return
        storage = DatabaseStorage()
        count = 0
        for path in sorted(uploads_dir.rglob('*')):
            if not path.is_file():
                continue
            name = path.relative_to(uploads_dir).as_posix()
            save_bytes(storage, name, path.read_bytes())
            count += 1
        total = StoredFile.objects.count()
        self.stdout.write(f'{count} files stored ({total} rows in stored_files).')
