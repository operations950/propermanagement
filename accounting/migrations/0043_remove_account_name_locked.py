from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('accounting', '0042_budgetproject_mail_all_addresses')]
    operations = [migrations.RemoveField(model_name='account', name='name_locked')]
