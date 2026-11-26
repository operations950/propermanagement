from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounting', '0039_budgetline_months')]
    operations = [migrations.AddField(model_name='budgetversion', name='from_prior', field=models.BooleanField(default=False, help_text="Built straight from last year's approved budget: no actuals, no projection, and no income added from last year's activity."))]
