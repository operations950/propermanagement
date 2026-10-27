from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


def _d(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        return None


@register.filter
def money(value, blank_zero=False):
    """$1,750.00 and -$1,750.00."""
    d = _d(value)
    if d is None:
        return ''
    if blank_zero and d == 0:
        return ''
    return f'-${abs(d):,.2f}' if d < 0 else f'${d:,.2f}'


@register.filter
def plain(value):
    """1,750.00 (no dollar sign); zero shows as 0.00."""
    d = _d(value)
    return '' if d is None else f'{d:,.2f}'


@register.filter
def plain_blank(value):
    d = _d(value)
    return '' if d is None or d == 0 else f'{d:,.2f}'


@register.filter
def mdate(value, fmt='md'):
    """Oct 1, 2026 (Windows-safe: no %-d)."""
    if not value:
        return ''
    if fmt == 'short':
        return f'{value:%b} {value.day}'
    return f'{value:%b} {value.day}, {value.year}'


@register.filter
def pct(value, places=4):
    try:
        return f'{Decimal(str(value)):.{int(places)}f}'
    except (InvalidOperation, ValueError, TypeError):
        return ''


@register.filter
def absval(value):
    d = _d(value)
    return abs(d) if d is not None else value


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None
