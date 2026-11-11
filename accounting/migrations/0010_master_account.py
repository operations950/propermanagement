from django.db import migrations, models


def seed(apps, schema_editor):
    """The master chart starts as the standard chart the program has always used."""
    from accounting.standard_chart import CHART
    Master = apps.get_model('accounting', 'MasterAccount')
    if not Master.objects.exists():
        Master.objects.bulk_create([Master(code=code, name=name, kind=kind) for code, name, kind in CHART])


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0009_prior_year_to_date'),
    ]

    operations = [
        migrations.CreateModel(
            name='MasterAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=8, unique=True)),
                ('name', models.CharField(max_length=120)),
                ('kind', models.CharField(choices=[('asset', 'Asset'), ('liability', 'Liability'), ('equity', 'Equity'), ('income', 'Income'), ('expense', 'Expense')], max_length=10)),
                ('is_active', models.BooleanField(default=True, help_text='Off: new associations do not start with it. Existing associations keep theirs.')),
            ],
            options={
                'ordering': ['code'],
            },
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
