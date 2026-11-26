"""Owner portal: a ticket is private until staff release it, and only status updates marked "show to owner" are read there. Additive; every existing ticket and note stays private."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0045_remove_old_recurring'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticket',
            name='owner_visible',
            field=models.BooleanField(default=False, help_text="Shown to the property's owners in the owner portal. Every ticket starts private; staff release one on purpose."),
        ),
        migrations.AddField(
            model_name='ticketstatusnote',
            name='show_to_owner',
            field=models.BooleanField(default=False, help_text='The owner portal shows this update (only when the ticket itself is shown to the owner too).'),
        ),
    ]
