"""Removes the per-property amenity gating on standard checklist items.
required_attributes let a StandardChecklistItem only resolve at properties
tagged with a matching PropertyAttribute (e.g. only show the pool item at
Pool-tagged properties) — removed per direct user feedback: with zero
properties ever actually tagged with any amenity, this silently dropped 8
real items (including "Strip all beds and start laundry") from every
cleaner's checklist everywhere, with no way to see why from the screen
staff actually use to edit checklists. See
onsite/services/checklist.py::resolve_checklist's docstring and
ONSITE_DESIGN.md for the full story. See the next migration
(0029_backfill_ungated_checklist_items) for repairing already-created
visits that are missing these items as a result."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0027_visit_manual_price_override'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='standardchecklistitem',
            name='required_attributes',
        ),
    ]
