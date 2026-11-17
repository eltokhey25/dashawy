"""
Force-reset an account's legacy bcrypt password.

This is a testing/recovery utility, not part of the public API. It writes a new
bcrypt hash into ``users.password_hash`` using the same cost (12) the Node app
used, and clears the failed-login lock so the account can sign in immediately.

Usage::

    python manage.py reset_admin_password --email admin@example.com --password 'NewPass123'
    python manage.py reset_admin_password --username admin --password 'NewPass123'

Pass the account by ``--email`` or (admins) ``--username``, plus ``--password``.
``--set-username`` also gives an admin a new sign-in username for /admin. This repository is public, so no account email
or password may ever be written into this file as a default.
"""

from django.core.management.base import BaseCommand, CommandError

from core.auth_backend import hash_password
from core.models import User
from core.validation import (
    is_valid_username,
    normalize_email,
    normalize_username,
    password_policy_error,
)


class Command(BaseCommand):
    help = 'Reset an account password with bcrypt (test/recovery helper).'

    def add_arguments(self, parser):
        who = parser.add_mutually_exclusive_group(required=True)
        who.add_argument('--email', help='Account email to reset.')
        who.add_argument('--username', help='Admin username to reset.')
        parser.add_argument(
            '--set-username', help='Also set this admin username for /admin sign-in.'
        )
        parser.add_argument(
            '--password', required=True, help='New password.'
        )

    def handle(self, *args, **options):
        password = options['password']

        problem = password_policy_error(password)
        if problem:
            raise CommandError(problem)

        if options.get('username'):
            label = normalize_username(options['username'])
            user = User.objects.filter(username=label, role='admin').first()
        else:
            label = normalize_email(options['email'])
            if not label:
                raise CommandError('An email address is required.')
            user = User.objects.filter(email=label).first()
        if user is None:
            raise CommandError(f'No account found for {label}.')

        changes = {}
        new_username = options.get('set_username')
        if new_username:
            if user.role != 'admin':
                raise CommandError('Only admin accounts have a username.')
            if not is_valid_username(new_username):
                raise CommandError(
                    'Username must be 3-32 of a-z, 0-9, . _ - and start with a letter.'
                )
            new_username = normalize_username(new_username)
            if User.objects.filter(username=new_username).exclude(pk=user.pk).exists():
                raise CommandError(f'Username {new_username} is already taken.')
            changes['username'] = new_username

        new_hash = hash_password(password)
        User.objects.filter(pk=user.pk).update(
            **changes,
            password_hash=new_hash,
            # A successful reset should be testable immediately, even if the
            # account was previously locked out by failed attempts.
            failed_login_attempts=0,
            login_locked_until=None,
            # The account was just given a known password on purpose; forcing a
            # change page would make the reset untestable in the browser.
            must_change_password=False,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f'Password reset for {label} (role: {user.role}). '
                'Failed-login lock cleared.'
                + (
                    f' Admin username: {changes.get("username") or user.username}.'
                    if user.role == 'admin' else ''
                )
            )
        )
