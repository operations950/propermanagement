"""Idempotent: seeds the 20 most common property amenities/characteristics
as PropertyAttribute rows, so the property detail dashboard's Amenities
picker has a real catalog to offer out of the box instead of just the one
"Pool" tag that predated this. get_or_create by key — safe to run on every
deploy, and safe if staff have already added more in Django admin."""
from django.core.management.base import BaseCommand

from core.models import PropertyAttribute

Category = PropertyAttribute.Category

COMMON_ATTRIBUTES = [
    ('pool', 'Pool', Category.PHYSICAL),
    ('hot_tub_spa', 'Hot Tub / Spa', Category.PHYSICAL),
    ('elevator', 'Elevator', Category.PHYSICAL),
    ('washer_dryer_in_unit', 'Washer/Dryer In-Unit', Category.PHYSICAL),
    ('central_ac', 'Central Air Conditioning', Category.PHYSICAL),
    ('fenced_yard', 'Fenced Yard', Category.PHYSICAL),
    ('garage', 'Garage', Category.PHYSICAL),
    ('balcony_patio', 'Balcony / Patio', Category.PHYSICAL),
    ('gated_community', 'Gated Community', Category.PHYSICAL),
    ('dock_waterfront', 'Dock / Waterfront', Category.PHYSICAL),
    ('storage_unit', 'Storage Unit', Category.PHYSICAL),
    ('security_system', 'Security System', Category.SERVICE),
    ('pool_service', 'Pool Service', Category.SERVICE),
    ('landscaping_service', 'Landscaping Service', Category.SERVICE),
    ('pest_control_service', 'Pest Control Service', Category.SERVICE),
    ('cleaning_service', 'Cleaning Service', Category.SERVICE),
    ('trash_valet', 'Trash Valet', Category.SERVICE),
    ('cable_internet_included', 'Cable/Internet Included', Category.SERVICE),
    ('hoa', 'HOA', Category.COMPLIANCE),
    ('flood_zone', 'Flood Zone', Category.COMPLIANCE),
]


class Command(BaseCommand):
    help = 'Seeds the 20 most common property amenity/attribute tags.'

    def handle(self, *args, **options):
        created = 0
        for key, label, category in COMMON_ATTRIBUTES:
            _, was_created = PropertyAttribute.objects.get_or_create(
                key=key, defaults={'label': label, 'category': category},
            )
            if was_created:
                created += 1
        self.stdout.write(self.style.SUCCESS(f'Created {created} new amenity tag(s) (of {len(COMMON_ATTRIBUTES)}).'))
