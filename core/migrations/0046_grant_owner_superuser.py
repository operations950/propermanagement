"""Makes the owner's own login (the one holding justin@proper-realty.com) a
superuser, Company Admin and Portfolio owner — once.

Superuser can't be granted from Admin Tools (its Company Admin toggle is a
different, in-app flag) and Django's own /admin/ only lets an existing
superuser grant it, so with no server shell the one route was the generic
`admin` bootstrap login being retired. This gives the owner's real account
the same reach first, so nothing depends on `admin` any more.

Only acts when EXACTLY ONE user holds that email — zero or several is
printed and left alone rather than guessed at. Idempotent."""
from django.conf import settings
from django.db import migrations

OWNER_EMAIL = 'justin@proper-realty.com'


def grant(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    StaffProfile = apps.get_model('core', 'StaffProfile')

    matches = list(User.objects.filter(email__iexact=OWNER_EMAIL))
    if len(matches) != 1:
        print(f'Owner access: found {len(matches)} user(s) with email {OWNER_EMAIL} — expected exactly one, so NOTHING was changed. '
              'Make sure your login has that email (Admin Tools > staff), then redeploy.')
        return
    user = matches[0]
    changed = []
    for field in ('is_active', 'is_staff', 'is_superuser'):
        if not getattr(user, field):
            setattr(user, field, True)
            changed.append(field)
    if changed:
        user.save(update_fields=changed)
    profile, _ = StaffProfile.objects.get_or_create(user=user)
    for field in ('is_company_admin', 'is_portfolio_owner'):
        if not getattr(profile, field):
            setattr(profile, field, True)
            profile.save(update_fields=[field])
            changed.append(field)
    print(f'Owner access: {user.username!r} <{OWNER_EMAIL}> is now active, staff, superuser, Company Admin and Portfolio owner'
          + (f' (newly set: {", ".join(changed)}).' if changed else ' (already was).'))


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0045_report_privileged_accounts'),
    ]

    operations = [
        migrations.RunPython(grant, migrations.RunPython.noop),
    ]
