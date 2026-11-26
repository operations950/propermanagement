"""A commercial building's released month keeps its financial package (PDF), frozen at release. Additive: a new optional file column."""
from django.db import migrations, models

import core.storage


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0088_contact_interaction_tracker'),
    ]

    operations = [
        migrations.AddField(
            model_name='monthrelease',
            name='package',
            field=models.FileField(blank=True, help_text="A commercial building: the month's whole financial package (PDF), frozen at release like the snapshot. Blank for a rental.", storage=core.storage.DocumentStorage(), upload_to='owner_packages/%Y/%m/'),
        ),
    ]
