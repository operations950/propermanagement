"""A property's trash and recycling schedule.

One schedule per property. It is a set of pickups — regular trash, bulk pickup, recycling and any
number of custom ones (vegetation, yard waste, ...) — each with the weekdays it happens. Making a
new schedule REPLACES the old one (the screen warns first); an existing one can be edited in place
(its days, and the name of a custom pickup).

Weekdays are numbers, 0 = Monday ... 6 = Sunday."""
import re

from django.db import transaction
from django.utils import timezone

from .models import Property, TrashRule, TrashSchedule

DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
DAY_ABBR = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
STANDARD = ('trash', 'bulk', 'recycling')
LABEL_MAX = 60
MAX_CUSTOM = 3


class TrashError(ValueError):
    """A schedule that can't be saved, in words a person can act on."""


def clean_days(raw):
    """Sorted, de-duplicated weekday numbers from whatever the form sent."""
    days = set()
    for value in raw or []:
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= n <= 6:
            days.add(n)
    return sorted(days)


def _name_key(text):
    return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()


def _validate(entries):
    """entries = [{'kind', 'label', 'days'}] -> cleaned list, or TrashError."""
    if not entries:
        raise TrashError('Choose at least one kind of pickup.')
    seen_kinds, seen_labels, out = set(), set(), []
    standard_names = {_name_key(k.label) for k in TrashRule.Kind if k != TrashRule.Kind.CUSTOM}
    for entry in entries:
        kind = entry.get('kind')
        days = clean_days(entry.get('days'))
        label = ' '.join((entry.get('label') or '').split())[:LABEL_MAX]
        if kind not in TrashRule.Kind.values:
            raise TrashError('That kind of pickup isn\'t recognised.')
        if kind == TrashRule.Kind.CUSTOM:
            if not label:
                raise TrashError('Give each custom pickup a name (vegetation, yard waste, ...).')
            key = _name_key(label)
            if key in seen_labels or key in standard_names:
                raise TrashError(f'"{label}" is already in the schedule.')
            seen_labels.add(key)
        else:
            if kind in seen_kinds:
                raise TrashError('Each kind of pickup can only be in the schedule once.')
            seen_kinds.add(kind)
            label = ''
        name = label or dict(TrashRule.Kind.choices)[kind]
        if not days:
            raise TrashError(f'Choose the days for {name.lower() if kind != TrashRule.Kind.CUSTOM else name}.')
        out.append({'kind': kind, 'label': label, 'days': days})
    if sum(1 for e in out if e['kind'] == TrashRule.Kind.CUSTOM) > MAX_CUSTOM:
        raise TrashError(f'At most {MAX_CUSTOM} custom pickups.')
    return out


@transaction.atomic
def replace_schedule(prop, entries, user=None):
    """Deletes the property's current schedule (if any) and creates the new one."""
    cleaned = _validate(entries)
    TrashSchedule.objects.filter(property=prop).delete()
    schedule = TrashSchedule.objects.create(property=prop, updated_by=user)
    for entry in cleaned:
        TrashRule.objects.create(schedule=schedule, **entry)
    return schedule


@transaction.atomic
def edit_schedule(prop, changes, user=None):
    """Changes the days (and a custom pickup's name) of the existing schedule's rules.
    `changes` = {rule id: {'days': [...], 'label': str or None}}. A pickup left with no days is
    removed; at least one must remain (to start over, make a new schedule)."""
    schedule = TrashSchedule.objects.filter(property=prop).first()
    if schedule is None:
        raise TrashError('There is no schedule to edit yet — make a new one.')
    rules = {r.pk: r for r in schedule.rules.all()}
    entries = []
    for pk, rule in rules.items():
        change = changes.get(pk)
        if change is None:
            entries.append({'kind': rule.kind, 'label': rule.label, 'days': rule.days, '_rule': rule})
            continue
        label = change.get('label') if rule.kind == TrashRule.Kind.CUSTOM and change.get('label') is not None else rule.label
        days = clean_days(change.get('days'))
        if days:
            entries.append({'kind': rule.kind, 'label': label, 'days': days, '_rule': rule})
    if not entries:
        raise TrashError('That would leave no pickups. To start over, make a new schedule instead.')
    cleaned = _validate([{k: v for k, v in e.items() if k != '_rule'} for e in entries])
    keep = set()
    for entry, clean in zip(entries, cleaned):
        rule = entry['_rule']
        rule.label, rule.days = clean['label'], clean['days']
        rule.save(update_fields=['label', 'days'])
        keep.add(rule.pk)
    schedule.rules.exclude(pk__in=keep).delete()
    schedule.updated_by = user
    schedule.save(update_fields=['updated_by', 'updated_at'])
    return schedule


def delete_schedule(prop):
    return TrashSchedule.objects.filter(property=prop).delete()[0]


# --- reading -------------------------------------------------------------------------------------

def day_text(days):
    """[0, 3] -> "Mon and Thu"; [0, 2, 4] -> "Mon, Wed and Fri"."""
    names = [DAY_ABBR[d] for d in sorted(days)]
    if len(names) <= 1:
        return ''.join(names)
    return ', '.join(names[:-1]) + ' and ' + names[-1]


def schedule_for(prop):
    """{'set', 'rules': [{'id','kind','name','days','day_text'}], 'by_day': [...], 'summary', 'updated_at'} —
    everything a screen or the assistant needs."""
    schedule = TrashSchedule.objects.filter(property=prop).prefetch_related('rules').first()
    if schedule is None:
        return {'set': False, 'rules': [], 'by_day': [], 'summary': '', 'updated_at': None}
    rules = [{'id': r.pk, 'kind': r.kind, 'name': r.name, 'days': list(r.days), 'day_text': day_text(r.days)} for r in schedule.rules.all()]
    by_day = []
    groups = {}
    for r in rules:
        groups.setdefault(tuple(r['days']), []).append(r['name'])
    for days, names in sorted(groups.items()):
        by_day.append({'days': list(days), 'day_text': day_text(days), 'names': names, 'text': ' + '.join(n.lower() if n in ('Regular trash', 'Bulk pickup') else n.lower() for n in names)})
    summary = '; '.join(f'{g["day_text"]}: {g["text"]}' for g in by_day)
    return {'set': True, 'rules': rules, 'by_day': by_day, 'summary': summary, 'updated_at': schedule.updated_at}


def missing_count():
    """Active short-term rentals with no schedule yet (a gentle reminder, never a requirement)."""
    have = set(TrashSchedule.objects.values_list('property_id', flat=True))
    return Property.objects.filter(is_active=True, is_general=False, property_type=Property.Type.SHORT_TERM_RENTAL).exclude(pk__in=have).count()
