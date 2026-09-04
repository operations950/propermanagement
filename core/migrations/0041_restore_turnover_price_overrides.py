"""Restores turnover_price_override for the properties/units whose real
paid price was recoverable after cleaning_fee was removed (migration 0040)
— reconstructed from Visit.paid_amount (frozen at payment time, untouched
by that removal) on two real CleaningPaymentBatch runs the user reviewed
via the Cleaning Payments screen's new expand feature, cross-checked
against backups/set_initial_cleaning_prices_20260819_175252.json (this
app's own pre-removal backup of the original cleaning_fee load, gitignored
so it isn't in this repo, but read directly off the user's machine).

Deliberately does NOT cover 712 NE 8th / 706-710 NE 7th Ct (the quadplex
building) — production has already consolidated these from several fake
standalone Properties into one real Property with real Units (per the
Unit-model migration plan), but this environment's own dev database still
has the old fake-property structure, so there's no reliable way to
confirm the current exact property/unit names to match against from here.
Left for a careful, separate follow-up once those are confirmed directly
against production — see the user conversation this was built from.

Also deliberately excludes "712 NE 8th Ave (Joe's) — Bamboo": the one
payment record found for it ($80, Aug 27) was for a "Normal Cleaning"
visit, not a Turnover Clean — a different service with no confirmed
turnover price of its own, so using that number here would silently
mislabel it.

Matched by exact Property.name / Unit.label — logs a clear per-entry
MATCH/SKIP line (visible in Railway's deploy log) rather than guessing,
so this doubles as an audit trail for whichever names don't resolve here
the same way they do in this dev database. Idempotent — safe to re-run,
each entry is just an unconditional set of the same target value."""
from decimal import Decimal

from django.db import migrations


# (property_name, unit_label_or_None, price)
PRICES = [
    ('100 Neptune', None, Decimal('325.00')),
    ('111 NW 1st Ave', None, Decimal('250.00')),
    # $200.00 as of Aug 25 — up from $185.00 in the original Aug 19 load;
    # using the more recent, actually-paid figure.
    ('224 NW 4th Ave', None, Decimal('200.00')),
    ('2821 Frederick', None, Decimal('195.00')),
    ('2919 Cormorant Rd', None, Decimal('235.00')),
    ('4606 Brady', None, Decimal('200.00')),
    ('716 Kittyhawk', None, Decimal('325.00')),
    ('803 NE 7th Ave', None, Decimal('100.00')),
    # Unit 2 confirmed directly (Aug 22 payment, $125.00); Unit 1 and Chic
    # Cottage aren't in either payment batch reviewed, so these two carry
    # forward their original Aug 19 load values (not contradicted by
    # anything more recent).
    ('323/325 Decarie', 'Unit 1', Decimal('125.00')),
    ('323/325 Decarie', 'Unit 2', Decimal('125.00')),
    ('323/325 Decarie', 'Chic Cottage', Decimal('85.00')),
    # A confirmed directly (Aug 14, $110.00); B carries forward its Aug 19
    # load value (not in either batch reviewed, but consistent with
    # A/C/D); C and D confirmed directly and repeatedly.
    ('800 Tropic', 'A', Decimal('110.00')),
    ('800 Tropic', 'B', Decimal('110.00')),
    ('800 Tropic', 'C', Decimal('110.00')),
    ('800 Tropic', 'D', Decimal('110.00')),
]


def restore_prices(apps, schema_editor):
    Property = apps.get_model('core', 'Property')
    Unit = apps.get_model('core', 'Unit')

    for name, unit_label, price in PRICES:
        try:
            prop = Property.objects.get(name=name)
        except Property.DoesNotExist:
            print(f'SKIP (property not found): {name!r}')
            continue
        except Property.MultipleObjectsReturned:
            print(f'SKIP (ambiguous — multiple properties named): {name!r}')
            continue

        if unit_label is None:
            prop.turnover_price_override = price
            prop.save(update_fields=['turnover_price_override'])
            print(f'MATCH: Property {name!r} (pk={prop.pk}) -> ${price}')
            continue

        try:
            unit = Unit.objects.get(property=prop, label=unit_label)
        except Unit.DoesNotExist:
            print(f'SKIP (unit not found): {name!r} / {unit_label!r}')
            continue
        except Unit.MultipleObjectsReturned:
            print(f'SKIP (ambiguous — multiple units with that label): {name!r} / {unit_label!r}')
            continue

        unit.turnover_price_override = price
        unit.save(update_fields=['turnover_price_override'])
        print(f'MATCH: Unit {name!r} / {unit_label!r} (pk={unit.pk}) -> ${price}')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0040_remove_property_cleaning_fee_and_more'),
    ]

    operations = [
        migrations.RunPython(restore_prices, migrations.RunPython.noop),
    ]
