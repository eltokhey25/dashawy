"""A customer may ask for one technician by name.

``preferred_technician`` is the technician chosen from the directory; the
offer goes to them first and to everyone else only if they pass.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_technicians_general_specialty'),
    ]

    operations = [
        migrations.AddField(
            model_name='request',
            name='preferred_technician',
            field=models.ForeignKey(
                blank=True,
                db_column='preferred_technician_id',
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='core.technician',
            ),
        ),
    ]
