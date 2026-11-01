"""
Load a backup made by the daily cron (or /api/admin/backup) into the database.

Usage::

    DATABASE_URL='postgresql://...' python manage.py restore_backup dashawy-backup-2026-11-01_0300.json.gz

The file is the gzipped Django fixture ``services/backup.py`` builds, so this
runs the migrations, then ``loaddata`` on it. Rows with the same ids are
overwritten; rows that only exist in the database are kept. To restore into an
empty database, create it first and point ``DATABASE_URL`` at it.
"""

from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Restore a dashawy-backup-*.json.gz file into the configured database.'

    def add_arguments(self, parser):
        parser.add_argument('backup', help='Path to the .json.gz backup file.')
        parser.add_argument(
            '--yes', action='store_true', help='Skip the confirmation prompt.'
        )

    def handle(self, *args, **options):
        path = Path(options['backup'])
        if not path.is_file():
            raise CommandError(f'Backup file not found: {path}')
        if not options['yes']:
            answer = input(
                f'Load {path.name} into the current database, overwriting rows with '
                'the same ids? Type "yes" to continue: '
            )
            if answer.strip().lower() != 'yes':
                raise CommandError('Cancelled; nothing changed.')
        call_command('migrate', interactive=False, verbosity=1)
        call_command('loaddata', str(path), verbosity=1)
        self.stdout.write(self.style.SUCCESS('Backup restored.'))
