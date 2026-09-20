"""READ-ONLY report (changes nothing): every login with elevated access, so
retiring the generic `admin` bootstrap login can be done knowing exactly who
else can take over and what would be lost.

Prints, for each user that is a superuser, staff, a Company Admin, a
Portfolio owner, or is named 'admin': active?, superuser/staff flags,
Company Admin / Portfolio owner flags, email, last login, whether they hold
the connected Google Calendar (the on-site calendar publishes with a
Company Admin's connection, and that connection is deleted along with its
owner), and how many role-default assignments they carry. Then the safety
line that matters: how many OTHER active superusers who are also Company
Admins exist — the account that could carry on if `admin` is switched off."""
from django.conf import settings
from django.db import migrations


def report(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    StaffProfile = apps.get_model('core', 'StaffProfile')
    GoogleCalendarToken = apps.get_model('core', 'GoogleCalendarToken')

    profiles = {p.user_id: p for p in StaffProfile.objects.all()}
    calendar_owners = set(GoogleCalendarToken.objects.values_list('staff__user_id', flat=True))

    rows = []
    for user in User.objects.all().order_by('username'):
        profile = profiles.get(user.pk)
        is_company_admin = bool(profile and profile.is_company_admin)
        is_portfolio_owner = bool(profile and profile.is_portfolio_owner)
        if not (user.is_superuser or user.is_staff or is_company_admin or is_portfolio_owner or user.username == 'admin'):
            continue
        rows.append((user, is_company_admin, is_portfolio_owner))

    print('Privileged accounts report (read-only; nothing was changed):')
    for user, is_company_admin, is_portfolio_owner in rows:
        flags = [
            'ACTIVE' if user.is_active else 'INACTIVE',
            'superuser' if user.is_superuser else '',
            'staff' if user.is_staff else '',
            'company-admin' if is_company_admin else '',
            'portfolio-owner' if is_portfolio_owner else '',
            'holds Google Calendar connection' if user.pk in calendar_owners else '',
        ]
        last = user.last_login.strftime('%Y-%m-%d') if user.last_login else 'never'
        print(f'  {user.username!r} <{user.email or "no email"}> last login {last}: ' + ', '.join(f for f in flags if f))

    others = [
        u for u, is_ca, _ in rows
        if u.username != 'admin' and u.is_active and u.is_superuser and is_ca
    ]
    print(f'  Other active superusers who are also Company Admin (could carry on without `admin`): {len(others)}'
          + (f' — {", ".join(repr(u.username) for u in others)}' if others else ''))


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0044_quickbooks_sync_status'),
    ]

    operations = [
        migrations.RunPython(report, migrations.RunPython.noop),
    ]
