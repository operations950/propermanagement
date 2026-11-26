from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounting', '0043_remove_account_name_locked')]
    operations = [migrations.AlterField(model_name='budgetproject', name='mail_all_addresses', field=models.BooleanField(default=False, help_text="Off: one letter per owner, to the address on file by season (seasonal, else mailing). On: a letter to every address on file for the owner, each place once."))]
