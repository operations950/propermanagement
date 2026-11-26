from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0038_bank_kind_trust'),
    ]

    operations = [
        migrations.AddField(
            model_name='budgetline',
            name='months',
            field=models.JSONField(blank=True, help_text='Optional: the budget by month, twelve amounts starting with the first month of the budget year. Blank (or not adding up to the annual amount): spread evenly.', null=True),
        ),
    ]
