"""Read-only report, not a data change — direct request: "Since we have
been tracking who created a ticket, can you give me counts of who
created how many tickets?" This session has no direct query access to
the production database, so a RunPython migration (the same mechanism
already used all session for one-off production reads/fixes — it runs
automatically via the Procfile's `python manage.py migrate --noinput`
on every deploy) is how the count actually gets computed there; the
results print to the Railway deploy log for this release.

Ticket.created_by is deliberately blank for anything with no human
author at creation time (email/Quo/booking-triggered/recurring
template/a cleaner's own token-link submit — see the field's own
help_text) — that's real signal, not a gap, so it gets its own line
here rather than being silently excluded. It also only started being
recorded once migration 0037 shipped (2026-08-27) — nothing before that
has ever had it set, schema-only, no backfill — so this necessarily
undercounts anyone's true lifetime total; the report says so plainly
rather than presenting a partial count as a complete one.

No-op reverse; changes nothing, safe to re-run."""
from django.db import migrations


def report(apps, schema_editor):
    Ticket = apps.get_model('tickets', 'Ticket')

    total = Ticket.objects.count()
    no_creator = Ticket.objects.filter(created_by__isnull=True).count()

    print(f'Ticket creator report — {total} ticket(s) total.')
    print(
        f'{no_creator} ticket(s) have no recorded creator (automated origin — email/Quo/booking/recurring/'
        'a cleaner\'s own submit — or created before created_by started being tracked on 2026-08-27).',
    )

    counts = {}
    for ticket in Ticket.objects.filter(created_by__isnull=False).select_related('created_by'):
        user = ticket.created_by
        # Plain field access, not user.get_full_name() — apps.get_model()
        # returns a HISTORICAL model reconstructed only from migration
        # state, which carries fields/relations but none of the real
        # model class's own Python methods (get_full_name() comes from
        # Django's AbstractUser, not from any migration). This crashed
        # the first attempt at this exact migration with AttributeError:
        # 'User' object has no attribute 'get_full_name' — caught only
        # against real production data, since local testing had zero
        # tickets with a creator set, so this code path never actually
        # ran locally the first time.
        full_name = f'{user.first_name} {user.last_name}'.strip()
        label = full_name or user.username
        counts[label] = counts.get(label, 0) + 1

    if not counts:
        print('No ticket has a recorded creator yet.')
        return

    print(f'{sum(counts.values())} ticket(s) have a recorded human creator, by person:')
    for label, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f'  {count:>4}  {label}')


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0039_delete_broken_ticket_451'),
    ]

    operations = [
        migrations.RunPython(report, migrations.RunPython.noop),
    ]
