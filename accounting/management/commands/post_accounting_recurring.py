"""Posts the recurring charges and recurring bills that have come due, for each association we keep the books for that has automatic posting on."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounting import charges, closing, payables


class Command(BaseCommand):
    help = 'Post recurring owner charges and recurring bills that are due.'

    def handle(self, *args, **options):
        today = timezone.localdate()
        for profile in closing.associations():
            if not profile.auto_post:
                continue
            prop = profile.property
            done = charges.post_due(prop, today)
            made = payables.post_recurring(prop, today)
            if done or made:
                self.stdout.write(f'{prop.name}: {sum(n for _, _, n in done)} charges, {len(made)} bills')
