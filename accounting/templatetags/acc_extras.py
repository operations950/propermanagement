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


@register.filter
def dollars(value):
    """$1,750 (whole dollars) and -$1,750."""
    try:
        d = int(round(float(value)))
    except (TypeError, ValueError):
        return ''
    return f'-${abs(d):,}' if d < 0 else f'${d:,}'


@register.filter
def dchange(value):
    """+$1,750 / -$1,750 / a dash for no change."""
    try:
        d = int(round(float(value)))
    except (TypeError, ValueError):
        return ''
    return '—' if d == 0 else (f'+${d:,}' if d > 0 else f'-${abs(d):,}')


@register.filter
def pchange(value):
    """+5.0% / -3.3% / new when there was nothing before / a dash for no change."""
    if value is None:
        return 'new'
    v = float(value)
    return '—' if abs(v) < 0.05 else f'{v:+.1f}%'


@register.filter
def dparen(value):
    """$1,750 / ($1,750) / a dash for no change: the board sheet's way."""
    try:
        d = int(round(float(value)))
    except (TypeError, ValueError):
        return ''
    return '—' if d == 0 else (f'${d:,}' if d > 0 else f'(${abs(d):,})')


@register.filter
def pparen(value):
    """5.0% / (3.3%) / new when there was nothing before / a dash for no change."""
    if value is None:
        return 'new'
    v = float(value)
    return '—' if abs(v) < 0.05 else (f'{v:.1f}%' if v > 0 else f'({abs(v):.1f}%)')


@register.filter
def cparen(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ''
    return '—' if abs(v) < 0.005 else (f'${v:,.2f}' if v > 0 else f'(${abs(v):,.2f})')


@register.filter
def cents_change(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ''
    return '—' if abs(v) < 0.005 else (f'+${v:,.2f}' if v > 0 else f'-${abs(v):,.2f}')
