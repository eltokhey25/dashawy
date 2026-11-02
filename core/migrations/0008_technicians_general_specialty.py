"""Make every technician a general dish technician.

Technicians no longer have specialties: every one handles every service, and
only a request's specialty still describes the job.
"""

from django.db import migrations


def make_general(apps, schema_editor):
    Technician = apps.get_model('core', 'Technician')
    Technician.objects.exclude(specialty='dish_general').update(specialty='dish_general')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_rate_counters'),
    ]

    operations = [
        migrations.RunPython(make_general, migrations.RunPython.noop),
    ]
