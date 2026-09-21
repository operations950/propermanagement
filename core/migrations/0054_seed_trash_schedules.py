"""The trash schedules the team already had for six rentals (weekday numbers: 0 = Monday ... 6 = Sunday).

Production has no shell, so they arrive with the deploy. Each is matched by the property id it has in
production AND a word from its name (so on another database, where ids differ, a wrong property is never
touched — it falls back to a name that matches exactly one property, else is skipped). A property that
already has a schedule keeps it."""
import re

from django.db import migrations

# (production property id, a word its name must contain, [(kind, custom label, weekdays)])
SCHEDULES = [
    (42, 'tropic', [('trash', '', [2, 5])]),                        # 800 Tropic: Wednesday and Saturday
    (46, 'cormorant', [('trash', '', [2, 5])]),                     # 2919 Cormorant: Wednesday and Saturday
    (43, 'decarie', [('trash', '', [0, 3])]),                       # 323/325 Decarie: Monday and Thursday
    (37, '803', [('trash', '', [6, 2])]),                           # 803 NE 7th Ave: Sunday and Wednesday
    (44, '224', [('trash', '', [0, 3])]),                           # 224 NW 4th Ave: Monday and Thursday
    (38, 'kittyhawk', [                                              # 716 Kittyhawk
        ('trash', '', [0, 2, 4]), ('custom', 'Vegetation', [0, 2, 4]),   # Mon/Wed/Fri garbage + vegetation
        ('bulk', '', [1, 3]), ('recycling', '', [1, 3]),                 # Tue/Thu bulk + recycling
    ]),
]


def _norm(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def seed(apps, schema_editor):
    Property = apps.get_model('core', 'Property')
    TrashSchedule = apps.get_model('core', 'TrashSchedule')
    TrashRule = apps.get_model('core', 'TrashRule')
    everyone = list(Property.objects.all())
    for pid, word, rules in SCHEDULES:
        prop = next((p for p in everyone if p.pk == pid and word in _norm(p.name)), None)
        if prop is None:
            matches = [p for p in everyone if word in _norm(p.name)]
            prop = matches[0] if len(matches) == 1 else None
        if prop is None or TrashSchedule.objects.filter(property=prop).exists():
            continue
        schedule = TrashSchedule.objects.create(property=prop)
        for kind, label, days in rules:
            TrashRule.objects.create(schedule=schedule, kind=kind, label=label, days=sorted(days))


class Migration(migrations.Migration):
    dependencies = [('core', '0053_trash_listings_unit_access')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
