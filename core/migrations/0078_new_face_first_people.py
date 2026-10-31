"""Turns the new layout on for its first user and sets up the menu roles. Safe to run on any database: it matches people by email or first name, changes nothing when nobody matches, and never limits what anyone can open.

  * Justin (the owner, matched by his work email, or by first name among company admins) gets new_face and the "owner" menu.
  * Charlotte is a cleaner, Anna a manager, Gia and Alexis property managers: only their menu_role is set, never new_face, so they keep seeing the original site.
"""
from django.db import migrations

OWNER_EMAILS = ['justin@proper-realty.com', 'justinbrowne02@gmail.com']
ROLE_BY_FIRST_NAME = {'charlotte': 'cleaner', 'anna': 'manager', 'ana': 'manager', 'gia': 'property_manager', 'alexis': 'property_manager'}


def set_up(apps, schema_editor):
    StaffProfile = apps.get_model('core', 'StaffProfile')
    for sp in StaffProfile.objects.select_related('user'):
        u = sp.user
        first = (u.first_name or u.username or '').strip().lower().split(' ')[0]
        email = (u.email or '').strip().lower()
        is_owner = email in OWNER_EMAILS or (first == 'justin' and (sp.is_company_admin or u.is_superuser))
        if is_owner:
            sp.new_face = True
            sp.menu_role = 'owner'
            sp.save(update_fields=['new_face', 'menu_role'])
        elif first in ROLE_BY_FIRST_NAME and not sp.menu_role:
            sp.menu_role = ROLE_BY_FIRST_NAME[first]
            sp.save(update_fields=['menu_role'])


class Migration(migrations.Migration):
    dependencies = [('core', '0077_staffprofile_menu_role_staffprofile_new_face')]
    operations = [migrations.RunPython(set_up, migrations.RunPython.noop)]
