"""Idempotent seed for the default VisitTypes and their standard checklists.
Wired into the Procfile alongside this app's other idempotent seed commands
— runs on every deploy, so it MUST stay purely additive (get_or_create by
(visit_type, text), same as always): deleting and recreating a
StandardChecklistItem here would reset its created_at every single deploy,
which would make VisitChecklistItem.is_new_unreviewed treat every item as
"new" forever (see resolve_checklist's is_new_unreviewed logic) — the exact
bug this comment is here to stop someone from reintroducing.

The turnover/deep-clean content below was rewritten from the original
placeholder list into something comprehensive. The one-time removal of the
OLD placeholder rows (so they don't sit alongside the new ones as
duplicates) is a real data migration
(onsite/migrations/0007_refresh_checklist_content.py), not something this
command does — a migration runs exactly once per environment, which is
what a one-time content swap actually needs; this command runs forever.
"""
from django.core.management.base import BaseCommand

from core.models import PropertyAttribute
from onsite.models import StandardChecklistItem, VisitType

# (section, text, mandatory, requires_photo, requires_note, required_attribute_keys)
TURNOVER_ITEMS = [
    # Entry & safety
    ('Entry & Safety', 'Confirm the property is empty and the previous guest has checked out', True, False, False, ()),
    ('Entry & Safety', 'Check smoke detectors are present and undamaged', True, False, False, ()),
    ('Entry & Safety', 'Check carbon monoxide detector is present and undamaged', True, False, False, ()),
    ('Entry & Safety', 'Test that all light switches and lamps work', False, False, False, ()),

    # Kitchen
    ('Kitchen', 'Wash, dry, and put away all dishes, pots, and pans', True, True, False, ()),
    ('Kitchen', 'Wipe down countertops and backsplash', True, False, False, ()),
    ('Kitchen', 'Clean stovetop and range hood', True, False, False, ()),
    ('Kitchen', 'Empty refrigerator/freezer of guest food and wipe down inside', True, False, False, ()),
    ('Kitchen', 'Clean microwave inside and out', True, False, False, ()),
    ('Kitchen', 'Empty dishwasher and check/clean the filter', False, False, False, ()),
    ('Kitchen', 'Wipe down small appliances (coffee maker, toaster, kettle)', False, False, False, ()),
    ('Kitchen', 'Restock coffee, tea, and dish soap per house standard', True, False, False, ()),
    ('Kitchen', 'Take out kitchen trash and replace the liner', True, False, False, ()),
    ('Kitchen', 'Wipe down dining table and chairs', True, False, False, ()),

    # Bathrooms
    ('Bathrooms', 'Clean and disinfect toilet, inside and out', True, True, False, ()),
    ('Bathrooms', 'Clean shower/tub, removing hair and soap scum', True, True, False, ()),
    ('Bathrooms', 'Clean sink, counter, and mirror', True, False, False, ()),
    ('Bathrooms', 'Restock toilet paper, hand soap, and shampoo per house standard', True, False, False, ()),
    ('Bathrooms', 'Replace bath and hand towels with clean ones', True, False, False, ()),
    ('Bathrooms', 'Empty bathroom trash and replace the liner', True, False, False, ()),

    # Bedrooms
    ('Bedrooms', 'Strip all beds and start laundry', True, False, False, ('washer_dryer_in_unit',)),
    ('Bedrooms', 'Remake all beds with clean linens', True, True, False, ()),
    ('Bedrooms', 'Fluff and arrange pillows and throw blankets', False, False, False, ()),
    ('Bedrooms', 'Check under beds, in closets, and in drawers for guest belongings left behind', True, False, False, ()),
    ('Bedrooms', 'Dust nightstands and dressers', False, False, False, ()),

    # Living areas
    ('Living Areas', 'Vacuum all carpets and rugs', True, False, False, ()),
    ('Living Areas', 'Sweep and mop all hard floors', True, False, False, ()),
    ('Living Areas', 'Dust surfaces, shelves, and electronics', False, False, False, ()),
    ('Living Areas', 'Fluff couch cushions and fold throw blankets', False, False, False, ()),
    ('Living Areas', 'Wipe down TV, remotes, and light switches', False, False, False, ()),
    ('Living Areas', 'Check remotes have working batteries', False, False, False, ()),
    ('Living Areas', 'Empty all trash cans and replace liners', True, False, False, ()),

    # Laundry — only asked when the property actually has an in-unit washer/dryer
    ('Laundry', 'Finish and put away all used linens and towels before leaving', True, False, False, ('washer_dryer_in_unit',)),
    ('Laundry', 'Wipe down washer and dryer exterior', False, False, False, ('washer_dryer_in_unit',)),

    # Exterior — gated to properties that actually have these amenities
    ('Exterior', 'Sweep patio/balcony and straighten outdoor furniture', False, False, False, ('balcony_patio',)),
    ('Exterior', 'Check pool area is clean, gate secured, and skimmer basket emptied', True, False, False, ('pool',)),
    ('Exterior', 'Check hot tub area is clean, cover secured, and chemical levels look normal', True, False, False, ('hot_tub_spa',)),

    # Final walkthrough
    ('Final Walkthrough', 'Set thermostat to house standard temperature', True, False, False, ()),
    ('Final Walkthrough', 'Turn off all lights except any exterior/porch light', True, False, False, ()),
    ('Final Walkthrough', 'Do a final walkthrough of every room', True, False, False, ()),
    ('Final Walkthrough', 'Confirm all doors and windows are locked before leaving', True, False, False, ()),
]

# Layered onto a turnover when Visit.is_deep_clean is set (see
# onsite/services/checklist.py::set_deep_clean) — incremental tasks ONLY,
# not a repeat of the turnover list above, since the addon is always added
# on top of a full turnover, never done standalone.
DEEP_CLEAN_ITEMS = [
    ('Kitchen', 'Clean inside of oven', True, True, False, ()),
    ('Kitchen', 'Clean inside of refrigerator and freezer, including shelves and drawers', True, True, False, ()),
    ('Kitchen', 'Clean inside and outside of all cabinets', False, False, False, ()),
    ('Kitchen', 'Descale coffee maker and clean out the coffee grounds trap', False, False, False, ()),
    ('Kitchen', 'Clean inside of dishwasher; run a cleaning cycle if needed', False, False, False, ()),
    ('Bathrooms', 'Scrub grout and tile', True, True, False, ()),
    ('Bathrooms', 'Clean exhaust fan and vent covers', False, False, False, ()),
    ('Bathrooms', 'Wash shower curtain/liner, or replace if needed', False, False, True, ()),
    ('Bedrooms', 'Rotate or flip mattresses', False, False, False, ()),
    ('Bedrooms', 'Wash all pillows, mattress protectors, and duvets (not just top sheets)', True, False, False, ('washer_dryer_in_unit',)),
    ('Bedrooms', 'Vacuum under beds and behind furniture', True, False, False, ()),
    ('Living Areas', 'Wash baseboards and door frames throughout', True, False, False, ()),
    ('Living Areas', 'Clean interior windows and window tracks', True, False, False, ()),
    ('Living Areas', 'Dust and wipe down ceiling fans and light fixtures', True, False, False, ()),
    ('Living Areas', 'Vacuum upholstered furniture, including under cushions', False, False, False, ()),
    ('Living Areas', 'Dust blinds and wipe down curtains', False, False, False, ()),
    ('Living Areas', 'Spot-clean walls and light switches for scuffs and marks', False, False, False, ()),
    ('General', 'Wash trash cans inside and out', True, False, False, ()),
    ('General', 'Replace batteries in remotes, smoke detectors, and thermostats if low', False, False, False, ()),
    ('General', 'Deep-clean washer drum and wipe down dryer lint trap housing', False, False, False, ('washer_dryer_in_unit',)),
]

INSPECTION_ITEMS = [
    ('Safety', 'Test smoke and CO detector batteries', True, False, False),
    ('Safety', 'Check fire extinguisher present and charged', True, True, False),
    ('Systems', 'Check HVAC filter condition', True, True, False),
    ('Systems', 'Check for visible plumbing leaks', True, True, False),
    ('Exterior', 'Inspect exterior/landscaping for damage', True, True, False),
    ('Exterior', 'Check locks, gates, and access points', True, False, False),
    ('General', 'Note any needed repairs or maintenance', False, False, True),
]


class Command(BaseCommand):
    help = 'Seeds the default VisitTypes and adds any standard checklist items that are missing (never deletes — see this file\'s module docstring).'

    def handle(self, *args, **options):
        attrs_by_key = {a.key: a for a in PropertyAttribute.objects.all()}

        turnover, created = VisitType.objects.get_or_create(
            slug='turnover',
            defaults={'name': 'Turnover Clean', 'default_duration_minutes': 90, 'requires_deadline': True},
        )
        self.stdout.write(f'{"Created" if created else "Found"} VisitType: {turnover.name}')
        self._add_items(turnover, TURNOVER_ITEMS, attrs_by_key)

        deep_clean, created = VisitType.objects.get_or_create(
            slug='deep-clean',
            defaults={'name': 'Deep Clean', 'default_duration_minutes': 0, 'requires_deadline': False, 'is_addon': True},
        )
        if not created and not deep_clean.is_addon:
            deep_clean.is_addon = True
            deep_clean.save(update_fields=['is_addon'])
        self.stdout.write(f'{"Created" if created else "Found"} VisitType: {deep_clean.name} (addon bundle)')
        self._add_items(deep_clean, DEEP_CLEAN_ITEMS, attrs_by_key)

        inspection, created = VisitType.objects.get_or_create(
            slug='inspection',
            defaults={'name': 'Property Inspection', 'default_duration_minutes': 60, 'requires_deadline': False},
        )
        self.stdout.write(f'{"Created" if created else "Found"} VisitType: {inspection.name}')
        for order, (section, text, mandatory, requires_photo, requires_note) in enumerate(INSPECTION_ITEMS):
            item, item_created = StandardChecklistItem.objects.get_or_create(
                visit_type=inspection, text=text,
                defaults={
                    'section': section, 'order': order, 'mandatory': mandatory,
                    'requires_photo': requires_photo, 'requires_note': requires_note,
                },
            )
            if item_created:
                self.stdout.write(f'  + {text}')

        self.stdout.write(self.style.SUCCESS('Checklist templates seeded.'))

    def _add_items(self, visit_type, spec, attrs_by_key):
        for order, (section, text, mandatory, requires_photo, requires_note, attr_keys) in enumerate(spec):
            item, item_created = StandardChecklistItem.objects.get_or_create(
                visit_type=visit_type, text=text,
                defaults={
                    'section': section, 'order': order, 'mandatory': mandatory,
                    'requires_photo': requires_photo, 'requires_note': requires_note,
                },
            )
            if attr_keys and (item_created or not item.required_attributes.exists()):
                found = [attrs_by_key[k] for k in attr_keys if k in attrs_by_key]
                missing = [k for k in attr_keys if k not in attrs_by_key]
                if missing:
                    self.stdout.write(self.style.WARNING(
                        f'  ! "{text}" references unknown PropertyAttribute key(s): {missing} — skipped for gating.',
                    ))
                if found:
                    item.required_attributes.set(found)
            if item_created:
                self.stdout.write(f'  + {text}')
