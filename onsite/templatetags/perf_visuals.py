"""Template access to the small SVG graphics in onsite/services/visuals.py."""
from django import template

from onsite.services import visuals

register = template.Library()


@register.simple_tag
def ring(pct, size=64, label='Occupancy'):
    return visuals.ring(pct, size=int(size), label=label)


@register.simple_tag
def gap_track(gap, today):
    return visuals.gap_track(gap, today)


@register.simple_tag
def heatstrip(start, shares, uid='hs'):
    return visuals.heatstrip(start, shares, uid=uid)


@register.simple_tag
def daystrip(start, states, uid='ds'):
    return visuals.daystrip(start, states, uid=uid)


@register.simple_tag
def stackbar(booked, gaps, opened):
    return visuals.stackbar([
        ('Booked', booked, visuals.INK, False),
        ('In gaps between stays', gaps, visuals.GAP, True),
        ('Open', opened, visuals.TRACK, False),
    ])


@register.filter
def pct_width(value, maximum=100):
    """A width for a data bar behind a table figure (0–100)."""
    try:
        return max(0, min(100, float(value) / float(maximum) * 100))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0
