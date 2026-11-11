from django.db import migrations


def add(apps, schema_editor):
    """St Andrews Grand already has Reserve Perimeter Fence (3836); the one company chart now carries it for everybody."""
    apps.get_model('accounting', 'MasterAccount').objects.get_or_create(code='3836', defaults={'name': 'Reserve Perimeter Fence', 'kind': 'equity'})


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0010_master_account'),
    ]

    operations = [
        migrations.RunPython(add, migrations.RunPython.noop),
    ]
