import django.db.models.deletion
from django.db import migrations, models


def clear_month_level(apps, schema_editor):
    """The first version marked a whole month filed. A filing is now per property, so those month-level rows have no property to belong to and are dropped."""
    apps.get_model('core', 'TouristTaxFiling').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0075_tourist_tax_filing'),
    ]

    operations = [
        migrations.RunPython(clear_month_level, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='touristtaxfiling',
            name='month',
            field=models.DateField(help_text='The first day of the month the return covers.'),
        ),
        migrations.AddField(
            model_name='touristtaxfiling',
            name='property',
            field=models.ForeignKey(default=1, on_delete=django.db.models.deletion.CASCADE, related_name='tourist_tax_filings', to='core.property'),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name='touristtaxfiling',
            constraint=models.UniqueConstraint(fields=('month', 'property'), name='uniq_tourist_tax_filing'),
        ),
    ]
