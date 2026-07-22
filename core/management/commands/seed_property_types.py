"""One-off: categorize the real properties (from normalize_properties) by
type, and create the five general/non-specific placeholders — 'No specific
property' plus one per business line — so a ticket can be scoped to
"associations in general" or "not property-specific at all" instead of being
forced onto one exact address."""
from django.core.management.base import BaseCommand

from core.models import Property

ASSOCIATIONS = ['La Pensee', 'St Andrews Grand', 'Lakeside Point 2', 'Lakeside Point 8', 'St Andrews Glen', 'Linton Woods']
COMMERCIAL = ['324 Lofts', 'Del Park LLC', 'Red Barn 381']
STRS = [
    '712 NE 8th (Modern)', '706 NE 7th Ct (Seashell)', '710 NE 7th Ct (Blue Ocean)', '712 NE 8th (Stylish)',
    '708 NE 7th Ct (Pearl)', '712 NE 8th (Bamboo)', '803 NE 7th Ave', '716 Kittyhawk', '100 Neptune',
    '4606 Brady', '111 NW 1st Ave', '800 Tropic', '323/325 Decarie', '224 NW 4th Ave', '702 NW 1st St',
    '2919 Cormorant Rd', '2821 Frederick', '300 Ross', '2036 Alta Meadows', '1038 E Heritage',
    '324 Harmon Ct', '1291 Laing St',
]

GENERAL_PLACEHOLDERS = [
    ('No specific property', Property.Type.GENERAL),
    ('Associations (general)', Property.Type.ASSOCIATION),
    ('Short-Term Rentals (general)', Property.Type.SHORT_TERM_RENTAL),
    ('Long-Term Rentals (general)', Property.Type.LONG_TERM_RENTAL),
    ('Commercial (general)', Property.Type.COMMERCIAL),
]


class Command(BaseCommand):
    help = 'Categorize real properties by type and create general/non-specific placeholder properties.'

    def handle(self, *args, **options):
        updated = 0
        for name in ASSOCIATIONS:
            updated += Property.objects.filter(name=name).update(property_type=Property.Type.ASSOCIATION)
        for name in COMMERCIAL:
            updated += Property.objects.filter(name=name).update(property_type=Property.Type.COMMERCIAL)
        for name in STRS:
            updated += Property.objects.filter(name=name).update(property_type=Property.Type.SHORT_TERM_RENTAL)
        self.stdout.write(f'Categorized {updated} existing properties.')

        created = 0
        for name, ptype in GENERAL_PLACEHOLDERS:
            _, was_created = Property.objects.get_or_create(
                name=name, defaults={'property_type': ptype, 'is_general': True},
            )
            if was_created:
                created += 1
        self.stdout.write(self.style.SUCCESS(f'Created {created} general placeholder(s) (of {len(GENERAL_PLACEHOLDERS)}).'))
