from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0020_check_down_mm'),
    ]

    operations = [
        migrations.AddField(
            model_name='unitprofile',
            name='webvault_bill_pay',
            field=models.CharField(blank=True, db_index=True, help_text="The unit's bill pay number in Truist's Web Vault (the regular assessment one, which ends in 1): how a Web Vault payment finds its unit.", max_length=20),
        ),
    ]
