"""One-time (idempotent) cleanup after reversing reactive/AI ticket intake
(see proptasks/scheduler.py) — deletes every Ticket the system created
automatically from a monitored source (email, phone/Quo, shared calendar,
Airbnb, VRBO, or the dev fake adapter) rather than a human. Tickets
created manually by staff or by the Functions/recurring-template system
are never touched.

Also clears any leftover possible_duplicate_of/duplicate_reasoning flags
on the tickets that remain — those were only ever reviewable on the now-
decommissioned Pending screen, so a still-set flag has nowhere to be
resolved.

Safe to run repeatedly (and left in Procfile) — once the matching rows
are gone, every subsequent run is a no-op, and nothing schedules new
auto-created tickets anymore for it to have to clean up."""
from django.core.management.base import BaseCommand

from tickets.models import Ticket

AUTO_CREATED_SOURCES = [
    Ticket.Source.EMAIL, Ticket.Source.QUO, Ticket.Source.CALENDAR,
    Ticket.Source.AIRBNB, Ticket.Source.VRBO, Ticket.Source.FAKE,
]


class Command(BaseCommand):
    help = 'Deletes all auto-created (non-manual, non-recurring) tickets and clears stray duplicate flags.'

    def handle(self, *args, **options):
        auto_created = Ticket.objects.filter(source__in=AUTO_CREATED_SOURCES)
        count = auto_created.count()
        if count:
            by_source = {
                src: auto_created.filter(source=src).count() for src in AUTO_CREATED_SOURCES
            }
            for src, n in by_source.items():
                if n:
                    self.stdout.write(f'  {src}: {n}')
            auto_created.delete()
            self.stdout.write(self.style.SUCCESS(f'Deleted {count} auto-created ticket(s).'))
        else:
            self.stdout.write('No auto-created tickets found.')

        stray_flags = Ticket.objects.exclude(possible_duplicate_of__isnull=True)
        stray_count = stray_flags.count()
        if stray_count:
            stray_flags.update(possible_duplicate_of=None, duplicate_reasoning='')
            self.stdout.write(self.style.SUCCESS(f'Cleared {stray_count} stray duplicate flag(s).'))
        else:
            self.stdout.write('No stray duplicate flags found.')
