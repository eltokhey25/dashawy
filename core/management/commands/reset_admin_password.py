"""
Force-reset an account's legacy bcrypt password.

This is a testing/recovery utility, not part of the public API. It writes a new
bcrypt hash into ``users.password_hash`` using the same cost (12) the Node app
used, and clears the failed-login lock so the account can sign in immediately.

Usage::

    python manage.py reset_admin_password
    python manage.py reset_admin_password --email admin@example.com --password 'NewPass123'

When ``DEBUG`` is off, the built-in default password is refused: pass
``--password`` explicitly so a production deployment can never be reset to a
credential that is printed in this file.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.auth_backend import hash_password
from core.models import User
from core.validation import normalize_email, password_policy_error

DEFAULT_EMAIL = 'eltokhey@fixrequest.com'
DEFAULT_PASSWORD = 'Eltokhey251120'


class Command(BaseCommand):
    help = (
        'Reset an account password with bcrypt (test/recovery helper). '
        f'Defaults to {DEFAULT_EMAIL} in DEBUG mode.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            default=DEFAULT_EMAIL,
            help=f'Account email to reset (default: {DEFAULT_EMAIL}).',
        )
        parser.add_argument(
            '--password',
            default=None,
            help=(
                'New password. Defaults to the built-in test password only when '
                'DEBUG is on; required when DEBUG is off.'
            ),
        )

    def handle(self, *args, **options):
        email = normalize_email(options['email'])
        if not email:
            raise CommandError('An email address is required.')

        password = options['password']
        if not password:
            if not settings.DEBUG:
                raise CommandError(
                    'Refusing to use the built-in test password with DEBUG=False. '
                    'Pass --password explicitly.'
                )
            password = DEFAULT_PASSWORD

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
