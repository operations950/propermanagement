# Split out of what was originally one combined migration
# (0008_standard_list_redesign) — on Postgres, deleting rows from
# PropertySupply/SupplyReading/SupplyOrderLine in the same transaction as
# the ALTER TABLE operations that come next (0009) fails with "cannot
# ALTER TABLE ... because it has pending trigger events": Postgres won't
# let you ALTER TABLE a relation that still has queued FK trigger events
# from an earlier DELETE in the SAME transaction. Since each migration
# file runs in its own transaction, splitting the data clear into its
# own migration lets that transaction fully commit (flushing the pending
# trigger queue) before 0009's schema changes start their own transaction.
# SQLite has no such restriction, which is why this wasn't caught in
# local dev testing before the first attempt at deploying this to
# Railway/Postgres.
#
# Per the user's explicit go-ahead for this rebuild ("we're still in the
# testing phase... not too worried about history... the only thing I
# want preserved is the Walmart IDs"), this clears operational history
# that can't be mapped forward into the new (property, unit, supply_item)
# direct-reference shape. SupplyItem itself (name/unit_label/
# walmart_item_id/is_active) is untouched — nothing here deletes from it.
from django.db import migrations


def _clear_operational_supply_data(apps, schema_editor):
    apps.get_model('supplies', 'SupplyOrderLine').objects.all().delete()
    apps.get_model('supplies', 'SupplyOrder').objects.all().delete()
    apps.get_model('supplies', 'SupplyReading').objects.all().delete()
    apps.get_model('supplies', 'PropertySupply').objects.all().delete()


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('supplies', '0007_alter_propertysupply_options_and_more'),
    ]

    operations = [
        migrations.RunPython(_clear_operational_supply_data, _noop),
    ]
