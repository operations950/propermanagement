from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounting', '0041_account_name_locked')]
    operations = [migrations.AddField(model_name='budgetproject', name='mail_all_addresses', field=models.BooleanField(default=False, help_text="Also mail every owner whose address of record is not the unit itself a copy at the unit address (on-site and off-site). Off: one letter, to the main address."))]
