"""Follow-up to migration 0041: restores turnover_price_override for the
712 NE 8th / 706-710 NE 7th Ct quadplex, which production has already
consolidated from 6 fake standalone Properties into one real Property
with real Units (per the Unit-model migration plan) — deliberately left
out of 0041 since this dev database still has the old fake-property
structure, so there was no way to confirm the exact current property/unit
names from here.

Prices per the user directly: Modern and Stylish are $70, every other
unit (Bamboo, Pearl, Seashell, Blue Ocean) is $80 — this also resolves
0041's other exclusion (Bamboo's one found payment was for a "Normal
Cleaning," not a Turnover Clean, so its real turnover price needed
separate confirmation; $80 is that confirmation).

Property name and two of the six unit labels ("Bamboo",
"Pearl (708 NE 7th Ct)") are known exactly from a real payment record the
user showed directly. The other four unit labels (Modern, Stylish,
Seashell, Blue Ocean) are inferred by the same naming pattern those two
confirmed ones establish — a unit that shares 712 NE 8th's own address
keeps just its plain distinguishing name (Bamboo), one that was
originally a different street address keeps that address alongside it
(Pearl (708 NE 7th Ct)) — but aren't independently confirmed. To stay
safe against a transcription mismatch either way, both the property and
each unit try an exact-name match first and fall back to a same-model
icontains match on just the distinguishing keyword; either path prints
exactly what it matched (or didn't) so Railway's deploy log is the real
audit trail — an ambiguous or failed match is skipped, never guessed
past."""
from decimal import Decimal

from django.db import migrations

PROPERTY_NAME = "712 NE 8th Ave (Joe's)"
PROPERTY_KEYWORD = '712 NE 8th'

# (unit_label_exact_guess, distinguishing_keyword_for_fallback, price)
UNIT_PRICES = [
    ('Bamboo', 'Bamboo', Decimal('80.00')),
    ('Modern', 'Modern', Decimal('70.00')),
    ('Stylish', 'Stylish', Decimal('70.00')),
    ('Pearl (708 NE 7th Ct)', 'Pearl', Decimal('80.00')),
    ('Seashell (706 NE 7th Ct)', 'Seashell', Decimal('80.00')),
    ('Blue Ocean (710 NE 7th Ct)', 'Blue Ocean', Decimal('80.00')),
]


def restore_quadplex_prices(apps, schema_editor):
    Property = apps.get_model('core', 'Property')
    Unit = apps.get_model('core', 'Unit')

    prop = Property.objects.filter(name=PROPERTY_NAME).first()
    if prop is None:
        candidates = list(Property.objects.filter(name__icontains=PROPERTY_KEYWORD))
        if len(candidates) == 1:
            prop = candidates[0]
            print(f'MATCH (fallback): property name {PROPERTY_NAME!r} not found exactly, '
                  f'used the one property containing {PROPERTY_KEYWORD!r}: {prop.name!r} (pk={prop.pk})')
        elif len(candidates) > 1:
            print(f'SKIP ALL (ambiguous): {len(candidates)} properties contain {PROPERTY_KEYWORD!r}: '
                  f'{[p.name for p in candidates]} — none touched.')
            return
        else:
            print(f'SKIP ALL (property not found): no property named {PROPERTY_NAME!r} or containing '
                  f'{PROPERTY_KEYWORD!r} — the quadplex consolidation may not have happened yet, or the '
                  'name differs from both guesses tried here.')
            return

    for label_guess, keyword, price in UNIT_PRICES:
        unit = Unit.objects.filter(property=prop, label=label_guess).first()
        if unit is not None:
            unit.turnover_price_override = price
            unit.save(update_fields=['turnover_price_override'])
            print(f'MATCH: Unit {prop.name!r} / {label_guess!r} (pk={unit.pk}) -> ${price}')
            continue

        candidates = list(Unit.objects.filter(property=prop, label__icontains=keyword))
        if len(candidates) == 1:
            unit = candidates[0]
            unit.turnover_price_override = price
            unit.save(update_fields=['turnover_price_override'])
            print(f'MATCH (fallback): Unit label {label_guess!r} not found exactly, used the one unit '
                  f'containing {keyword!r}: {prop.name!r} / {unit.label!r} (pk={unit.pk}) -> ${price}')
        elif len(candidates) > 1:
            print(f'SKIP (ambiguous): {len(candidates)} units under {prop.name!r} contain {keyword!r}: '
                  f'{[u.label for u in candidates]} — none touched.')
        else:
            print(f'SKIP (unit not found): {prop.name!r} has no unit named {label_guess!r} or containing '
                  f'{keyword!r} — check its real unit list directly.')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0041_restore_turnover_price_overrides'),
    ]

    operations = [
        migrations.RunPython(restore_quadplex_prices, migrations.RunPython.noop),
    ]
