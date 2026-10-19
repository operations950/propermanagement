from decimal import Decimal

from django import template

from .. import policy

register = template.Library()


@register.filter
def mins(minutes):
    """7h 10m"""
    return policy.minutes_label(minutes)


@register.filter
def hrs(value):
    """38.50"""
    if value in (None, ''):
        return ''
    return f'{Decimal(value):.2f}'


@register.filter
def hrs1(value):
    """38.5, with a real minus sign"""
    if value in (None, ''):
        return ''
    value = Decimal(value)
    return ('−' if value < 0 else '') + f'{abs(value):.1f}'


@register.filter
def days1(hours):
    """Hours as days of 8 hours, to one decimal: 3.2"""
    if hours in (None, ''):
        return ''
    return hrs1(Decimal(hours) / policy.DAY_HOURS)


@register.filter
def short_date(d):
    """Tue Dec 29"""
    return policy.short_day(d) if d else ''


@register.simple_tag
def span(start, end):
    """Dec 21 – Jan 3"""
    return f'{start:%b} {start.day} – {end:%b} {end.day}'
