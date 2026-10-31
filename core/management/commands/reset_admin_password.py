"""
Force-reset an account's legacy bcrypt password.

This is a testing/recovery utility, not part of the public API. It writes a new
bcrypt hash into ``users.password_hash`` using the same cost (12) the Node app
used, and clears the failed-login lock so the account can sign in immediately.

Usage::

    python manage.py reset_admin_password --email admin@example.com --password 'NewPass123'

Both arguments are required. This repository is public, so no account email
or password may ever be written into this file as a default.
"""

from django.core.management.base import BaseCommand, CommandError

from core.auth_backend import hash_password
from core.models import User
from core.validation import normalize_email, password_policy_error


class Command(BaseCommand):
    help = 'Reset an account password with bcrypt (test/recovery helper).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email', required=True, help='Account email to reset.'
        )
        parser.add_argument(
            '--password', required=True, help='New password.'
        )

    def handle(self, *args, **options):
        email = normalize_email(options['email'])
        if not email:
            raise CommandError('An email address is required.')

        password = options['password']

        problem = password_policy_error(password)
        if problem:
            raise CommandError(problem)

        user = User.objects.filter(email=email).first()
        if user is None:
            raise CommandError(f'No account found for {email}.')

        new_hash = hash_password(password)
        User.objects.filter(pk=user.pk).update(
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
                f'Password reset for {email} (role: {user.role}). '
                'Failed-login lock cleared.'
            )
        )
