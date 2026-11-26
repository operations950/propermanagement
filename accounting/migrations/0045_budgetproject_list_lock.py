from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounting', '0044_mail_all_addresses_help')]
    operations = [
        migrations.AddField(model_name='budgetproject', name='list_locked_at', field=models.DateTimeField(blank=True, null=True, help_text='When the mailing list was locked: who is mailed to, and at which addresses.')),
        migrations.AddField(model_name='budgetproject', name='list_snapshot', field=models.JSONField(blank=True, default=list, help_text='The mailing list as it was when it was locked.')),
    ]
