import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0024_commercial_books_on'),
        ('core', '0085_tourist_tax_payment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CloseStepDone',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('month', models.DateField(help_text='First day of the month the step was done for.')),
                ('key', models.CharField(help_text="The close-sheet step, e.g. 'late_fees'.", max_length=30)),
                ('marked_at', models.DateTimeField(auto_now_add=True)),
                ('note', models.CharField(blank=True, max_length=300)),
                ('association', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='close_steps_done', to='core.property')),
                ('marked_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('association', 'month', 'key'), name='uniq_close_step_done')],
            },
        ),
    ]
