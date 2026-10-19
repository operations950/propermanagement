from django.db import migrations


def preset(apps, schema_editor):
    """Fill in the two schedules already decided (Gia: Monday to Friday, salaried; Alexis: Monday, Tuesday, Thursday, Friday, Saturday, hourly) for the
    staff accounts with those names. Nobody is switched on: the "On the time clock" switch and the hire date are for the owner to set."""
    StaffProfile = apps.get_model('core', 'StaffProfile')
    TimeProfile = apps.get_model('timeclock', 'TimeProfile')
    for first, last, pay, days in (('gia', 'isolano', 'salaried', '0,1,2,3,4'), ('alexis', 'mccarthy', 'hourly', '0,1,3,4,5')):
        matches = list(StaffProfile.objects.filter(user__first_name__iexact=first, user__last_name__iexact=last))
        if len(matches) == 1:
            TimeProfile.objects.get_or_create(staff=matches[0], defaults={'pay_type': pay, 'work_days': days, 'applies': False})


class Migration(migrations.Migration):
    dependencies = [('timeclock', '0001_initial')]
    operations = [migrations.RunPython(preset, migrations.RunPython.noop)]
