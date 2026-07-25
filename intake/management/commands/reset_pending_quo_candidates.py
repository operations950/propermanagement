from django.core.management.base import BaseCommand

from core.models import Contact, ContactImportCandidate


class Command(BaseCommand):
    help = (
        'One-time reset for the Quo contact review queue: deletes every still-PENDING Quo-sourced '
        'ContactImportCandidate row outright (not a reject — these were staged before the phone-format '
        'and nameless-bare-phone-number fixes, so they\'re not worth keeping around for audit). Never '
        'touches already-approved/rejected candidates, Yardi/Gmail candidates, or real Contacts. Run '
        '"Sync Quo contacts now" again afterward to repopulate cleanly.'
    )

    def handle(self, *args, **options):
        qs = ContactImportCandidate.objects.filter(
            source=Contact.Source.QUO, status=ContactImportCandidate.Status.PENDING,
        )
        count = qs.count()
        qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {count} pending Quo candidate(s). Run the sync again to repopulate.'
        ))
