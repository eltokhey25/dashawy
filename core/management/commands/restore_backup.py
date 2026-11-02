"""
Load a backup made by the daily cron (or /api/admin/backup) into the database.

Usage::

    DATABASE_URL='postgresql://...' python manage.py restore_backup dashawy-backup-2026-11-01_0300.json.gz

The file is the gzipped Django fixture ``services/backup.py`` builds, so this
runs the migrations, then ``loaddata`` on it. A backup too large for one
Telegram file arrives as ``...json.gz.part1-of-N`` files: pass all of them
and they are joined in order. Rows with the same ids are
overwritten; rows that only exist in the database are kept. To restore into an
empty database, create it first and point ``DATABASE_URL`` at it.
"""

import re
import shutil
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Restore a dashawy-backup-*.json.gz file into the configured database.'

    def add_arguments(self, parser):
        parser.add_argument(
            'backup', nargs='+', help='The .json.gz backup, or all of its .partN-of-M files.'
        )
        parser.add_argument(
            '--yes', action='store_true', help='Skip the confirmation prompt.'
        )

    def handle(self, *args, **options):
        paths = [Path(p) for p in options['backup']]
        for path in paths:
            if not path.is_file():
                raise CommandError(f'Backup file not found: {path}')
        if len(paths) == 1 and not re.search(r'\.part\d+-of-\d+$', paths[0].name):
            self._restore(paths[0], options)
            return
        parts = sorted(paths, key=self._part_number)
        total = {int(re.search(r'-of-(\d+)$', p.name).group(1)) for p in parts}
        if len(total) != 1 or total.pop() != len(parts):
            raise CommandError('Pass every part of the backup (e.g. part1-of-3 to part3-of-3).')
        with tempfile.TemporaryDirectory() as tmp:
            joined = Path(tmp) / re.sub(r'\.part\d+-of-\d+$', '', parts[0].name)
            with joined.open('wb') as out:
                for part in parts:
                    with part.open('rb') as handle:
                        shutil.copyfileobj(handle, out)
            self._restore(joined, options)

    @staticmethod
    def _part_number(path):
        match = re.search(r'\.part(\d+)-of-\d+$', path.name)
        if not match:
            raise CommandError(f'Not a backup part: {path.name}')
        return int(match.group(1))

    def _restore(self, path, options):
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
