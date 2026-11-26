from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounting', '0040_budgetversion_from_prior')]
    operations = [migrations.AddField(model_name='account', name='name_locked', field=models.BooleanField(default=False, help_text="This association's own name for the account (its old system's or its budget's wording): keeping the company chart in line leaves it alone."))]
