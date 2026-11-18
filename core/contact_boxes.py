"""The boxes of the Contacts screen (the "newspaper" format, see core/newspaper.py).

NO type chosen: the first box is "Recent" (the 25 people you dealt with last: core/contact_activity.py keeps the ticker), then one box for each type with only its 8 most recently dealt-with
people and a last row "All 245 owners" that opens the whole type, so nobody is handed a list of hundreds.

A type chosen: the whole type, grouped the way that type is looked up:
  * owners, tenants, board members, association members, on-site staff and guests: one box for each PROPERTY they belong to (a person tied to several properties is in each; "No property" last);
  * vendors / contractors: one box for each TRADE (what kind of vendor; "No type set" last, highlighted, with a count so the gaps are easy to find);
  * staff, leads and others: alphabetical (one box, or a box for each first letter once there are many).
A search narrows either view. Every function here reads the database a fixed number of times, never once for each person.
"""
from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Count, F, Prefetch, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from .models import Contact, Unit
from .newspaper import box

RECENT_LIMIT = 25
PER_TYPE_LIMIT = 8
LETTER_BOXES_AFTER = 30          # an alphabetical type with more people than this is split into a box for each first letter

# The filter buttons, in the order they are shown and the order the unfiltered boxes come in.
TYPE_ORDER = ['owner', 'tenant', 'board_member', 'association_member', 'vendor', 'on_site_staff', 'staff_adjacent', 'guest', 'lead', 'other']
SHORT_LABELS = {'vendor': 'Vendor / contractor', 'board_member': 'Board member', 'association_member': 'Association member', 'on_site_staff': 'On-site staff', 'staff_adjacent': 'Staff'}
BY_PROPERTY = {'owner', 'tenant', 'board_member', 'association_member', 'on_site_staff', 'guest'}
BY_TRADE = {'vendor'}


def type_label(value):
    return SHORT_LABELS.get(value) or dict(Contact.ContactType.choices).get(value, value)


PLURALS = {'owner': 'owners', 'tenant': 'tenants', 'board_member': 'board members', 'association_member': 'association members', 'vendor': 'vendors / contractors', 'on_site_staff': 'on-site staff',
           'staff_adjacent': 'staff', 'guest': 'guests', 'lead': 'leads', 'other': 'others'}


def plural_label(value, n):
    """'owners' for the "All 245 owners" row; the singular when there is just one."""
    if n == 1:
        return {'vendor': 'vendor / contractor', 'staff_adjacent': 'staff', 'on_site_staff': 'on-site staff'}.get(value, type_label(value).lower())
    return PLURALS.get(value) or type_label(value).lower() + 's'


def ago(moment, now=None):
    """'3m ago', '5h ago', '2d ago', '3w ago', '4mo ago', '1y ago' for a moment, or '' for none."""
    if not moment:
        return ''
    seconds = int(((now or timezone.now()) - moment).total_seconds())
    if seconds < 60:
        return 'just now'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes}m ago'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}h ago'
    days = hours // 24
    if days < 14:
        return f'{days}d ago'
    if days < 60:
        return f'{days // 7}w ago'
    if days < 365:
        return f'{days // 30}mo ago'
    return f'{days // 365}y ago'


def _placeholder(prop):
    """'Commercial (general)' and its kind exist so a ticket can be about a whole line of business: a person tied only to those has no property of their own."""
    return prop.is_general and (prop.property_type == 'general' or '(general)' in prop.name.lower())


def search(qs, q):
    q = (q or '').strip()
    if not q:
        return qs
    return qs.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q) | Q(trade__icontains=q) | Q(contact_type__icontains=q))


def with_related(qs):
    """The related rows a row needs, read in two extra queries for the whole set (never one for each person)."""
    return qs.prefetch_related('properties', Prefetch('units', queryset=Unit.objects.only('id', 'label', 'property_id')))


def type_counts(q=''):
    """{type: how many people}, one query (the search narrows it)."""
    rows = search(Contact.objects.all(), q).values('contact_type').annotate(n=Count('pk')).order_by()
    return {r['contact_type']: r['n'] for r in rows}


def with_type(qs, value):
    """The people of one type: those whose main type it is, and those who carry it as another hat (an Owner who is also a Board Member is in both)."""
    secondary = [c.pk for c in Contact.objects.exclude(secondary_types=[]).only('pk', 'secondary_types') if value in (c.secondary_types or [])]
    return qs.filter(Q(contact_type=value) | Q(pk__in=secondary))


def _row(c, now, kind='person', prop=None, show_type=False):
    """One person as a row of the screen: what the row template draws."""
    units = [u.label for u in c.units.all() if prop is None or u.property_id == prop.pk]
    props = [p.name for p in c.properties.all() if not _placeholder(p)]
    if c.contact_type in BY_TRADE:
        facts = c.trade or 'No type set'
    elif prop is not None:
        facts = ('Unit ' + ', '.join(units)) if units else (c.phone or c.email or '')
    else:
        facts = ' · '.join(props[:2]) + (f' +{len(props) - 2}' if len(props) > 2 else '') if props else (c.phone or c.email or '')
    line2 = (type_label(c.contact_type) + (' · ' + facts if facts else '')) if show_type else facts
    secondary = [type_label(t) for t in (c.secondary_types or []) if t != c.contact_type]
    if secondary and not show_type:
        line2 = (line2 + ' · ' if line2 else '') + ' / '.join(secondary)
    return {'kind': kind, 'pk': c.pk, 'name': c.name, 'line2': line2, 'ago': ago(c.last_interaction_at, now), 'phone': c.phone, 'email': c.email}


def _more_row(value, n, q):
    return {'kind': 'more', 'label': f'All {n} {plural_label(value, n)}', 'type': value, 'q': q}


def unfiltered(q='', counts=None):
    """[boxes] for the screen with no type chosen, and the number of people the search found."""
    now = timezone.now()
    base = search(Contact.objects.all(), q)
    recent = list(with_related(base.filter(last_interaction_at__isnull=False).order_by('-last_interaction_at', 'name'))[:RECENT_LIMIT])
    boxes = []
    if recent:
        boxes.append(box('Recent', [_row(c, now, show_type=True) for c in recent], hint='who you dealt with last', key='recent'))
    counts = type_counts(q) if counts is None else counts
    ranked = base.annotate(_rank=Window(expression=RowNumber(), partition_by=[F('contact_type')], order_by=[F('last_interaction_at').desc(nulls_last=True), F('name').asc()]))
    top = with_related(ranked.filter(_rank__lte=PER_TYPE_LIMIT).order_by('contact_type'))
    by_type = defaultdict(list)
    for c in top:
        by_type[c.contact_type].append(c)
    for value in TYPE_ORDER + sorted(set(counts) - set(TYPE_ORDER)):
        people = by_type.get(value)
        if not people:
            continue
        people.sort(key=lambda c: (c.last_interaction_at is None, -(c.last_interaction_at.timestamp() if c.last_interaction_at else 0), c.name.lower()))
        rows = [_row(c, now) for c in people]
        total = counts.get(value, len(people))
        rows.append(_more_row(value, total, q))
        one = box(type_label(value), rows, key=f'type-{value}')
        one['count'] = total
        boxes.append(one)
    return boxes, sum(counts.values())


def _letter(c):
    first = (c.name or '?').strip()[:1].upper()
    return first if first.isalpha() else '#'


def filtered(value, q=''):
    """[boxes] for one type chosen, and the number of people in it (after the search)."""
    now = timezone.now()
    people = list(with_related(with_type(search(Contact.objects.all(), q), value).order_by('name')))
    boxes = []
    if value in BY_PROPERTY:
        groups, none = {}, []
        for c in people:
            tied = [p for p in c.properties.all() if not _placeholder(p)]
            if not tied:
                none.append(c)
            for p in tied:
                groups.setdefault(p.pk, (p, []))[1].append(c)
        for p, members in sorted(groups.values(), key=lambda g: g[0].name.lower()):
            title = p.name + ('' if p.is_active else ' (inactive)')
            one = box(title, [_row(c, now, prop=p) for c in members], key=f'property-{p.pk}')
            boxes.append(one)
        if none:
            boxes.append(box('No property', [_row(c, now) for c in none], key='no-property', tone='late'))
    elif value in BY_TRADE:
        trades, spelling, none = defaultdict(list), defaultdict(Counter), []
        for c in people:
            key = ' '.join((c.trade or '').split()).lower()
            if not key:
                none.append(c)
                continue
            trades[key].append(c)
            spelling[key][' '.join(c.trade.split())] += 1
        for key in sorted(trades):
            title = spelling[key].most_common(1)[0][0]
            boxes.append(box(title, [_row(c, now) for c in trades[key]], key=f'trade-{key}'))
        if none:
            boxes.append(box('No type set', [_row(c, now) for c in none], hint='give each vendor a type', key='no-type-set', tone='late'))
    elif len(people) > LETTER_BOXES_AFTER:
        letters = defaultdict(list)
        for c in people:
            letters[_letter(c)].append(c)
        for letter in sorted(letters, key=lambda x: (x == '#', x)):
            boxes.append(box(letter, [_row(c, now) for c in letters[letter]], key=f'letter-{letter.lower()}'))
    elif people:
        boxes.append(box(type_label(value) + ', A to Z', [_row(c, now) for c in people], key='a-to-z'))
    return boxes, len(people)


def vendors_without_a_type():
    return Contact.objects.filter(contact_type='vendor', trade='').count()


def alerts(user):
    """The reminders the Contacts screen keeps behind its Alerts button: [{icon, text, detail, url, level}], most pressing first. Contacts waiting for a yes or no (the words are the
    admin reminder's; everyone else sees "to review"), possible duplicates, vendors with no type. A problem working one out never breaks the page."""
    import logging

    from django.core.cache import cache
    from django.urls import reverse

    from . import nudges
    from .duplicates import find_duplicate_groups
    from .models import ContactImportCandidate, ContactUpdateCandidate

    log = logging.getLogger(__name__)
    items = []
    try:
        n = (ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING).count()
             + ContactUpdateCandidate.objects.filter(status=ContactUpdateCandidate.Status.PENDING).count())
        if n:
            word = 'contact' if n == 1 else 'contacts'
            admin = nudges._is_admin(user)
            items.append({'icon': 'contact', 'level': 'warn', 'url': reverse('contact_review'),
                          'text': f'{n} {word} waiting for a yes or no' if admin else f'{n} to review',
                          'detail': 'Imported or changed contacts. Nothing changes until you say yes.'})
    except Exception:
        log.exception('A contacts alert could not be worked out')
    try:
        groups = cache.get_or_set('contacts:duplicate-group-count', lambda: len(find_duplicate_groups()), 120)
        if groups:
            items.append({'icon': 'copy', 'level': 'info', 'url': reverse('contact_duplicates'), 'text': f'{groups} possible duplicate{"" if groups == 1 else "s"}',
                          'detail': 'The same phone number or email on more than one contact.'})
    except Exception:
        log.exception('A contacts alert could not be worked out')
    try:
        missing = vendors_without_a_type()
        if missing:
            items.append({'icon': 'tag', 'level': 'info', 'url': reverse('contact_list') + '?type=vendor', 'text': f'{missing} vendor{" has" if missing == 1 else "s have"} no type set',
                          'detail': 'Vendors are grouped by type; the ones without one are in the last box.'})
    except Exception:
        log.exception('A contacts alert could not be worked out')
    return items
