"""Retires the generic `admin` bootstrap login: deactivates it, makes its
password unusable, and strips its superuser / staff / Company Admin /
Portfolio flags. The row is KEPT (not deleted) so anything it created stays
attributed to it and its staff record — with any Google Calendar connection
— isn't cascaded away.

Guarded: it only acts when at least one OTHER active user is a superuser AND
a Company Admin (0046 makes the owner's own login exactly that). If none
exists it changes nothing and says so, rather than risk leaving the app with
no administrator. Idempotent; a login that's already inactive is skipped.
bootstrap_admin (run every deploy) leaves a deactivated login alone, so it
stays retired even if ADMIN_PASSWORD is still set."""
from django.conf import settings
from django.db import migrations

RETIRED_USERNAME = 'admin'


def retire(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    StaffProfile = apps.get_model('core', 'StaffProfile')
    GoogleCalendarToken = apps.get_model('core', 'GoogleCalendarToken')

    admin = User.objects.filter(username=RETIRED_USERNAME).first()
    if admin is None:
        print(f'Retire admin: no login named {RETIRED_USERNAME!r} — nothing to do.')
        return
    if not admin.is_active:
        print(f'Retire admin: {RETIRED_USERNAME!r} is already deactivated.')
        return

    company_admin_ids = set(StaffProfile.objects.filter(is_company_admin=True).values_list('user_id', flat=True))
    successors = [
        u for u in User.objects.filter(is_active=True, is_superuser=True).exclude(pk=admin.pk)
        if u.pk in company_admin_ids
    ]
    if not successors:
        print(f'Retire admin: NOT retired — no other active superuser who is also a Company Admin exists, '
              f'so {RETIRED_USERNAME!r} is still needed. (Owner access must succeed first; see the previous line.)')
        return

    holds_calendar = GoogleCalendarToken.objects.filter(staff__user_id=admin.pk).exists()
    successor_has_calendar = GoogleCalendarToken.objects.filter(staff__user_id__in=[u.pk for u in successors]).exists()

    admin.is_active = False
    admin.is_superuser = False
    admin.is_staff = False
    admin.password = '!' + 'retired-generic-admin-login'  # Django's "unusable password" marker: no password can match
    admin.save(update_fields=['is_active', 'is_superuser', 'is_staff', 'password'])
    StaffProfile.objects.filter(user_id=admin.pk).update(is_company_admin=False, is_portfolio_owner=False)

    print(f'Retire admin: {RETIRED_USERNAME!r} deactivated (kept for history). Carrying on: '
          + ', '.join(repr(u.username) for u in successors) + '.')
    if holds_calendar and not successor_has_calendar:
        print('  NOTE: the retired login held the connected Google Calendar. The on-site calendar will keep publishing '
              'with that connection, but connect Google Calendar from your own login too so it does not depend on it.')


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0046_grant_owner_superuser'),
    ]

    operations = [
        migrations.RunPython(retire, migrations.RunPython.noop),
    ]
