"""Give assigned requests a doorstep code and clear used ones.

Django never generated the legacy ``start_pin`` column, so any job that was
assigned before this release had no code and could not be started through the
arrival-code flow. This data migration fills those rows and removes stale codes
from requests that are already past the ``assigned`` stage.
"""

import secrets

from django.db import migrations


def _code():
    return ''.join(str(secrets.randbelow(10)) for _ in range(4))


def backfill_start_pins(apps, schema_editor):
    Request = apps.get_model('core', 'Request')

    assigned = Request.objects.filter(status='assigned').filter(
        start_pin__isnull=True
    ) | Request.objects.filter(status='assigned', start_pin='')
    for job in assigned:
        Request.objects.filter(pk=job.pk).update(
            start_pin=_code(),
            start_pin_attempts=0,
            start_pin_locked_until=None,
        )

    # A completed/cancelled job must not keep a code that the API could still
    # return; it has no further use after the job starts.
    Request.objects.exclude(status='assigned').exclude(
        start_pin__isnull=True
    ).update(
        start_pin=None,
        start_pin_attempts=0,
        start_pin_locked_until=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_guestsubmissionlog_request_guest_linked_at_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_start_pins, migrations.RunPython.noop),
    ]
