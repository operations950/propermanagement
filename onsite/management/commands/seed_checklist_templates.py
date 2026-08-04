"""Idempotent seed for the 3 default VisitTypes and their standard
checklists — turnover/deep clean/inspection, per ONSITE_DESIGN.md. Wired
into the Procfile alongside this app's other idempotent seed commands.
Safe to run repeatedly: get_or_create on (visit_type, text) means adding a
line here later and re-running only adds the new item, never duplicates
existing ones."""
from django.core.management.base import BaseCommand

from onsite.models import StandardChecklistItem, VisitType

VISIT_TYPES = [
    {
        'slug': 'turnover', 'name': 'Turnover Clean', 'default_duration_minutes': 90,
        'requires_deadline': True,
        'items': [
            ('Bedrooms', 'Strip and remake all beds', True, True, False),
            ('Bedrooms', 'Check under beds and in closets for guest items', True, False, False),
            ('Bathrooms', 'Clean toilets, showers, and sinks', True, True, False),
            ('Bathrooms', 'Restock toilet paper and toiletries', True, False, False),
            ('Kitchen', 'Wash dishes and wipe down counters', True, True, False),
            ('Kitchen', 'Empty and wipe refrigerator', True, False, False),
            ('Kitchen', 'Take out trash and replace liners', True, False, False),
            ('Living areas', 'Vacuum/sweep and mop all floors', True, False, False),
            ('Living areas', 'Dust surfaces and wipe down furniture', True, False, False),
            ('General', 'Check smoke/CO detectors are present', True, False, False),
            ('General', 'Lock all doors and windows before leaving', True, False, False),
        ],
    },
    {
        'slug': 'deep-clean', 'name': 'Deep Clean', 'default_duration_minutes': 180,
        'requires_deadline': False,
        'items': [
            ('Kitchen', 'Clean inside oven and microwave', True, True, False),
            ('Kitchen', 'Descale coffee maker/kettle', False, False, False),
            ('Bathrooms', 'Scrub grout and tile', True, True, False),
            ('Bedrooms', 'Rotate/flip mattresses', False, False, False),
            ('Living areas', 'Wash baseboards and door frames', True, False, False),
            ('Living areas', 'Clean interior windows', True, False, False),
            ('General', 'Wipe down light fixtures and ceiling fans', False, False, False),
            ('General', 'Launder all linens, towels, and throw blankets', True, False, False),
        ],
    },
    {
        'slug': 'inspection', 'name': 'Property Inspection', 'default_duration_minutes': 60,
        'requires_deadline': False,
        'items': [
            ('Safety', 'Test smoke and CO detector batteries', True, False, False),
            ('Safety', 'Check fire extinguisher present and charged', True, True, False),
            ('Systems', 'Check HVAC filter condition', True, True, False),
            ('Systems', 'Check for visible plumbing leaks', True, True, False),
            ('Exterior', 'Inspect exterior/landscaping for damage', True, True, False),
            ('Exterior', 'Check locks, gates, and access points', True, False, False),
            ('General', 'Note any needed repairs or maintenance', False, False, True),
        ],
    },
]


class Command(BaseCommand):
    help = 'Seeds the default VisitTypes (turnover/deep clean/inspection) and their standard checklists.'

    def handle(self, *args, **options):
        for spec in VISIT_TYPES:
            visit_type, created = VisitType.objects.get_or_create(
                slug=spec['slug'],
                defaults={
                    'name': spec['name'],
                    'default_duration_minutes': spec['default_duration_minutes'],
                    'requires_deadline': spec['requires_deadline'],
                },
            )
            self.stdout.write(f'{"Created" if created else "Found"} VisitType: {visit_type.name}')
            for order, (section, text, mandatory, requires_photo, requires_note) in enumerate(spec['items']):
                item, item_created = StandardChecklistItem.objects.get_or_create(
                    visit_type=visit_type, text=text,
                    defaults={
                        'section': section, 'order': order, 'mandatory': mandatory,
                        'requires_photo': requires_photo, 'requires_note': requires_note,
                    },
                )
                if item_created:
                    self.stdout.write(f'  + {text}')
        self.stdout.write(self.style.SUCCESS('Checklist templates seeded.'))
