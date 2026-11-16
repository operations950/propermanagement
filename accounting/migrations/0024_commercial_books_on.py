from django.db import migrations


def books_on(apps, schema_editor):
    """The user's rule: we do the financials for every commercial property we have. Idempotent."""
    Property = apps.get_model('core', 'Property')
    Profile = apps.get_model('accounting', 'AssociationProfile')
    for prop in Property.objects.filter(property_type='commercial'):
        profile, made = Profile.objects.get_or_create(property=prop, defaults={'we_do_financials': True})
        if not made and not profile.we_do_financials:
            profile.we_do_financials = True
            profile.save(update_fields=['we_do_financials'])


class Migration(migrations.Migration):
    dependencies = [('accounting', '0023_reserve_ack_and_board_send')]
    operations = [migrations.RunPython(books_on, migrations.RunPython.noop)]
