"""Blocked days on the platform calendars that nothing explains. A block is not a reservation and never counts toward occupancy (a handyman day is a real
vacancy). But an owner staying in their own unit is blocked on the calendar and entered as an in-house reservation for the same days: a block covered by
a reservation (an in-house one, or any stay on a calendar at that unit, which is how another platform's reservation shows up as a block here) is
explained. What is left is flagged for a person, who can mark it fine with a reason (maintenance, say)."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from ..models import BlockReview, Booking, CalendarBlock
from . import str_board


def _local(dt):
    return timezone.localtime(dt).date()


def _runs(nights):
    out, run = [], []
    for n in sorted(nights):
        if run and n == run[-1] + timedelta(days=1):
            run.append(n)
        else:
            if run:
                out.append(run)
            run = [n]
    if run:
        out.append(run)
    return out


def unexplained(property_id=None, unit_id=None, today=None):
    """[{property, unit, label, start, end (first night after), nights, sources}], soonest first: blocked nights from today on that no reservation covers
    and nobody has marked fine."""
    today = today or timezone.localdate()
    eligible = {p.pk: p for p in str_board.eligible_properties()}
    blocks = CalendarBlock.objects.filter(end__gt=today, property_id__in=list(eligible)).select_related('unit')
    if property_id:
        blocks = blocks.filter(property_id=property_id)
    if unit_id:
        blocks = blocks.filter(unit_id=unit_id)
    blocks = list(blocks)
    if not blocks:
        return []
    props = {b.property_id for b in blocks}
    stays = Booking.objects.filter(property_id__in=props, status=Booking.Status.ACTIVE, check_out__date__gt=today).filter(Q(on_calendar=True) | Q(source=Booking.Source.MANUAL))
    covered = {}
    for b in stays:
        day, end = max(_local(b.check_in), today), _local(b.check_out)
        while day < end:
            covered.setdefault((b.property_id, b.unit_id), set()).add(day)
            day += timedelta(days=1)
    for r in BlockReview.objects.filter(property_id__in=props, end__gt=today):
        day = max(r.start, today)
        while day < r.end:
            covered.setdefault((r.property_id, r.unit_id), set()).add(day)
            day += timedelta(days=1)
    blocked = {}
    for blk in blocks:
        day = max(blk.start, today)
        while day < blk.end:
            blocked.setdefault((blk.property_id, blk.unit_id), {}).setdefault(day, set()).add(blk.source)
            day += timedelta(days=1)
    rows = []
    unit_of = {b.unit_id: b.unit for b in blocks if b.unit_id}
    for (pid, uid), nights in blocked.items():
        loose = {n: s for n, s in nights.items() if n not in covered.get((pid, uid), set())}
        for run in _runs(loose):
            prop = eligible[pid]
            unit = unit_of.get(uid)
            rows.append({'property': prop, 'unit': unit, 'label': f'{prop.name} — {unit.label}' if unit else prop.name, 'start': run[0], 'end': run[-1] + timedelta(days=1),
                         'nights': len(run), 'sources': sorted({src for n in run for src in loose[n]})})
    return sorted(rows, key=lambda r: (r['start'], r['label']))


def without_calendar():
    """The units (or whole properties) that have a calendar line never set up: nothing tells us who is staying there. Labels, for a warning."""
    from .feeds import coverage_report
    return sorted({line['label'] for line in coverage_report()['attention'] if line['status'] == 'missing'})
