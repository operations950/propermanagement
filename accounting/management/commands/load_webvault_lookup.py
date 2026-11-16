"""Save an association's Web Vault unit lookup on its units, from the file Truist gives (the Unit Lookup CSV).

  python manage.py load_webvault_lookup --association "St Andrews Grand" --csv "/path/to/UnitLookup (2).csv"           look only: prints what it would save, changes NOTHING
  python manage.py load_webvault_lookup --association "St Andrews Grand" --csv "..." --apply                          save the units that are certain
  python manage.py load_webvault_lookup --association "St Andrews Grand" --csv "..." --apply --accept-changes        also replace a different number already saved

Each unit of the file is tied to one of ours by its unit number, else by its owner's name. Units that fit more than one of ours, or none, are listed and left alone (choose them
on the Sort deposits screen). A number already saved for a unit is never replaced unless --accept-changes is given.
"""
from django.core.management.base import BaseCommand, CommandError

from accounting import webvault
from core.models import Property


class Command(BaseCommand):
    help = "Save an association's Web Vault unit lookup on its units. Looks only, unless --apply."

    def add_arguments(self, parser):
        parser.add_argument('--association', required=True, help='the association (its name, or enough of it to be only one)')
        parser.add_argument('--csv', required=True, help='the Web Vault Unit Lookup CSV')
        parser.add_argument('--apply', action='store_true', help='save it (without this nothing is changed)')
        parser.add_argument('--accept-changes', action='store_true', help='also replace a different number that is already saved')

    def handle(self, *args, association, csv, apply=False, accept_changes=False, **opts):
        props = list(Property.objects.filter(name__iexact=association, property_type=Property.Type.ASSOCIATION)) or list(Property.objects.filter(name__icontains=association, property_type=Property.Type.ASSOCIATION))
        if len(props) != 1:
            raise CommandError(f'"{association}" matches {len(props)} associations' + (': ' + ', '.join(p.name for p in props) if props else '') + '. Give the name more exactly.')
        prop = props[0]
        try:
            with open(csv, 'rb') as f:
                rows = webvault.read_unit_lookup(f.read())
        except OSError as e:
            raise CommandError(f'Could not open {csv}: {e.strerror}')
        except webvault.WebVaultError as e:
            raise CommandError(str(e))
        plan = webvault.plan_lookup(prop, rows)
        by = {k: [x for x in plan['rows'] if x['status'] == k] for k in ('new', 'same', 'changed')}
        self.stdout.write(f'{prop.name}: {len(webvault._representatives(rows))} units in the file, {len(plan["rows"])} tied to one of ours.')
        for key, title in (('new', 'To save'), ('same', 'Already saved the same'), ('changed', 'A different number is already saved')):
            self.stdout.write(f'{title}: {len(by[key])}')
            for x in by[key]:
                extra = f' (saved now {x["stored"] or "on another unit"})' if key == 'changed' else ''
                self.stdout.write(f'  {x["unit"].label:<12} {x["bill"]}  <- {x["lookup_unit"]} {x["owner"]}{extra}')
        self.stdout.write(f'Ambiguous or not found: {len(plan["undecided"])}')
        for d in plan['undecided']:
            names = ', '.join(u.label for u in d['candidates']) or 'no unit of ours looks like it'
            self.stdout.write(f'  {d["unit"]} {d["owner"]}  ->  {names}')
        if not apply:
            self.stdout.write('Nothing was saved. Add --apply to save it.')
            return
        accept = [x['prefix'] for x in by['changed']] if accept_changes else []
        done = webvault.save_lookup(prop, rows, accept=accept)
        self.stdout.write(self.style.SUCCESS(f'Saved {done["saved"]} unit{"s" if done["saved"] != 1 else ""}; {done["same"]} already saved; {len(done["changed"])} left as they were; {len(done["undecided"])} to choose.'))
