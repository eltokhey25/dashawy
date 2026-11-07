"""Register (or inspect) the Telegram bot's webhook.

Usage::

    python manage.py telegram_webhook                 # register for SITE_URL
    python manage.py telegram_webhook --url https://dashawy.vercel.app
    python manage.py telegram_webhook --show          # what Telegram has now

Reads ``TELEGRAM_BOT_TOKEN`` from the environment; the webhook secret is
derived from it, so nothing else has to be configured. The same thing is
available to the super admin at ``POST /api/admin/telegram/webhook``.
"""

from django.core.management.base import BaseCommand, CommandError

from core.services import telegram
from core.site_config import get_site_config


class Command(BaseCommand):
    help = 'Register the Telegram webhook and command menu for this site.'

    def add_arguments(self, parser):
        parser.add_argument('--url', help='Public site URL (default: SITE_URL).')
        parser.add_argument(
            '--show', action='store_true', help='Only print what Telegram has registered.'
        )

    def handle(self, *args, **options):
        if not telegram.is_configured():
            raise CommandError('TELEGRAM_BOT_TOKEN is not set.')
        site_url = (options.get('url') or get_site_config()['site_url']).rstrip('/')
        if not options['show'] and not site_url.startswith('https://'):
            raise CommandError('Telegram only accepts an https:// webhook URL.')
        try:
            if options['show']:
                info = telegram.webhook_info()
                self.stdout.write(f"registered: {info.get('url') or '(none)'}")
                self.stdout.write(f"expected:   {telegram.webhook_url(site_url)}")
                self.stdout.write(f"pending:    {info.get('pending_update_count', 0)}")
                if info.get('last_error_message'):
                    self.stdout.write(f"last error: {info['last_error_message']}")
                return
            url = telegram.setup_webhook(site_url)
        except telegram.TelegramError as exc:
            raise CommandError(f'Telegram refused: {exc}')
        self.stdout.write(self.style.SUCCESS(f'Webhook registered: {url}'))
        username = telegram.bot_username()
        if username:
            self.stdout.write(f'Bot: https://t.me/{username}')
