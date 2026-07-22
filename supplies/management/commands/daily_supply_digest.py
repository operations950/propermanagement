import logging

from django.core.management.base import BaseCommand

from ...models import SupplyRequest

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Log a once-daily summary of pending supply requests per property, so staff know to "
        "check the /supplies/ digest page and turn them into an Amazon order list."
    )

    def handle(self, *args, **options):
        pending = SupplyRequest.objects.filter(status=SupplyRequest.Status.PENDING).select_related('property')
        counts = {}
        for req in pending:
            name = req.property.name if req.property_id else '[needs property]'
            counts[name] = counts.get(name, 0) + 1

        if not counts:
            self.stdout.write('No pending supply requests today.')
            return

        for name, count in counts.items():
            logger.info('Supply digest: %s has %d pending request(s)', name, count)
            self.stdout.write(f'{name}: {count} pending request(s)')
