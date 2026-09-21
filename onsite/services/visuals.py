"""Small inline-SVG graphics for the rental performance screens: sparklines, occupancy
rings, stacked bars, and day-by-day strips. Server-drawn (no script needed), each with an
accessible label and hover text, and never colour alone: a gap in the calendar is hatched as
well as amber, every figure is also printed beside its graphic.

Colours are the app's steel-blue ramp for the one measure being drawn (a single hue, light to
dark), with the warning amber reserved for "a gap to fill"."""
from datetime import timedelta
from html import escape

from django.utils.safestring import mark_safe

INK = '#3d6178'            # the measure being drawn
INK_SOFT = '#93b2c6'
TRACK = '#e3e7ec'          # the empty part of a track
GAP = '#fab219'            # a vacancy gap to fill (the app's warning colour)
SURFACE = '#ffffff'

FORMATS = {
    'pct': lambda v: f'{v:.0f}%',
    'money': lambda v: f'${v:,.0f}',
    'dec1': lambda v: f'{v:.1f}',
    'int': lambda v: f'{v:.0f}',
}


def _fmt(kind, value):
    return FORMATS[kind](value)


def _svg(width, height, inner, label, extra=''):
    return mark_safe(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" style="width:100%;height:auto;display:block;" '
        f'role="img" aria-label="{escape(label, quote=True)}" {extra}>{inner}</svg>'
    )


# --- sparklines ------------------------------------------------------------------------------

def sparkline(values, labels, kind='line', fmt='pct', ymax=None, partial_last=False, height=38, width=240):
    """One value per month. kind 'bars' or 'line'. None is a month with nothing to show (a
    gap in the line, no bar). `labels` are the month names for the hover text."""
    n = len(values)
    present = [v for v in values if v is not None]
    if not n or not present:
        return mark_safe('<div class="text-muted small" style="height: %spx; line-height: %spx;">no data yet</div>' % (height, height))
    top = ymax if ymax is not None else max(present) * 1.12 or 1
    bottom = 0 if kind == 'bars' else min(present) - (max(present) - min(present)) * 0.25
    if kind == 'line':
        top = max(present) + (max(present) - min(present)) * 0.15 or (max(present) * 1.1 or 1)
        if top == bottom:
            top = bottom + 1
    pad = 4
    plot = height - 2 * pad
    step = width / n
    inner = []

    def y(v):
        return pad + plot * (1 - (v - bottom) / ((top - bottom) or 1))

    if kind == 'bars':
        bar = max(step - 4, 3)
        for i, v in enumerate(values):
            x = i * step + (step - bar) / 2
            h = 0 if v is None else max(plot * (v - bottom) / ((top - bottom) or 1), 1.5 if v else 0)
            if v is None:
                inner.append(f'<rect x="{x:.1f}" y="{pad + plot - 2}" width="{bar:.1f}" height="2" rx="1" fill="{TRACK}"><title>{escape(labels[i])}: no data</title></rect>')
                continue
            opacity = 0.55 if (partial_last and i == n - 1) else 1
            inner.append(f'<rect x="{x:.1f}" y="{pad + plot - h:.1f}" width="{bar:.1f}" height="{h:.1f}" rx="2" fill="{INK}" opacity="{opacity}"><title>{escape(labels[i])}: {_fmt(fmt, v)}{" (to date)" if partial_last and i == n - 1 else ""}</title></rect>')
    else:
        points = [(i * step + step / 2, y(v), v, i) for i, v in enumerate(values) if v is not None]
        segments, current = [], []
        for i, v in enumerate(values):
            if v is None:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append((i * step + step / 2, y(v)))
        if current:
            segments.append(current)
        for seg in segments:
            if len(seg) > 1:
                area = ' '.join(f'{px:.1f},{py:.1f}' for px, py in seg)
                inner.append(f'<polygon points="{seg[0][0]:.1f},{pad + plot:.1f} {area} {seg[-1][0]:.1f},{pad + plot:.1f}" fill="{INK}" opacity="0.10"/>')
                inner.append(f'<polyline points="{area}" fill="none" stroke="{INK}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        for px, py, v, i in points:
            last = i == max(p[3] for p in points)
            inner.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{3.2 if last else 2}" fill="{INK}" stroke="{SURFACE}" stroke-width="{1.5 if last else 0}"><title>{escape(labels[i])}: {_fmt(fmt, v)}</title></circle>')
    what = 'bars' if kind == 'bars' else 'line'
    first_i = next(i for i, v in enumerate(values) if v is not None)
    last_i = max(i for i, v in enumerate(values) if v is not None)
    label = f'Twelve-month trend ({what}): {labels[first_i]} {_fmt(fmt, values[first_i])}, {labels[last_i]} {_fmt(fmt, values[last_i])}, high {_fmt(fmt, max(present))}, low {_fmt(fmt, min(present))}'
    return _svg(width, height, ''.join(inner), label)


# --- ring ----------------------------------------------------------------------------------------

def ring(pct, size=64, stroke=8, label='Occupancy'):
    """A donut showing a percentage; the number is printed in the middle."""
    if pct is None:
        return mark_safe(f'<div class="text-muted small text-center" style="width:{size}px;">—</div>')
    pct = max(0.0, min(100.0, float(pct)))
    r = (size - stroke) / 2
    c = 2 * 3.141592653589793 * r
    dash = c * pct / 100
    mid = size / 2
    return mark_safe(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" aria-label="{escape(label)} {pct:.0f} percent">'
        f'<circle cx="{mid}" cy="{mid}" r="{r:.1f}" fill="none" stroke="{TRACK}" stroke-width="{stroke}"/>'
        f'<circle cx="{mid}" cy="{mid}" r="{r:.1f}" fill="none" stroke="{INK}" stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-dasharray="{dash:.1f} {c:.1f}" transform="rotate(-90 {mid} {mid})"/>'
        f'<text x="{mid}" y="{mid + 4}" text-anchor="middle" font-size="{size * 0.24:.0f}" font-weight="600" fill="#2b2b2e">{pct:.0f}%</text></svg>'
    )


# --- stacked bar (booked / gaps / open) -------------------------------------------------------

def stackbar(parts, height=12):
    """parts = [(label, count, colour, hatched)]: one bar, segments in proportion, a 2px surface
    gap between them. Segments with nothing in them are left out."""
    total = sum(c for _l, c, _col, _h in parts)
    if not total:
        return mark_safe(f'<div style="height:{height}px;border-radius:6px;background:{TRACK};"></div>')
    width = 300
    inner = [f'<defs><pattern id="hatch-sb" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="5" height="5" fill="{GAP}" opacity="0.35"/><rect width="2" height="5" fill="{GAP}"/></pattern></defs>']
    x = 0.0
    live = [p for p in parts if p[1]]
    for i, (label, count, colour, hatched) in enumerate(live):
        w = width * count / total
        seg = max(w - (2 if i < len(live) - 1 else 0), 1)
        fill = 'url(#hatch-sb)' if hatched else colour
        inner.append(f'<rect x="{x:.1f}" y="0" width="{seg:.1f}" height="{height}" rx="3" fill="{fill}"><title>{escape(label)}: {count}</title></rect>')
        x += w
    desc = ', '.join(f'{l} {c}' for l, c, _col, _h in parts)
    return _svg(width, height, ''.join(inner), f'Nights: {desc}')


# --- day strips ---------------------------------------------------------------------------------

def _month_ticks(start, days, cell):
    ticks = []
    for i in range(days):
        d = start + timedelta(days=i)
        if d.day == 1 or i == 0:
            ticks.append((i * cell, d.strftime('%b')))
    return ticks


def daystrip(start, states, height=16, uid='ds', ticks=True):
    """One cell per day from `start`. `states` are 'booked', 'gap' (an empty stretch between two
    stays: worth filling), or 'open' (nothing booked, nothing after it either). Gaps are hatched
    as well as amber."""
    days = len(states)
    if not days:
        return ''
    cell = 300 / days
    label_h = 12 if ticks else 0
    inner = [f'<defs><pattern id="hatch-{uid}" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="4" height="4" fill="{GAP}" opacity="0.3"/><rect width="1.6" height="4" fill="{GAP}"/></pattern></defs>']
    counts = {'booked': 0, 'gap': 0, 'open': 0}
    for i, state in enumerate(states):
        d = start + timedelta(days=i)
        counts[state] += 1
        fill = {'booked': INK, 'gap': f'url(#hatch-{uid})', 'open': TRACK}[state]
        text = {'booked': 'booked', 'gap': 'empty between two stays — fill this', 'open': 'open'}[state]
        inner.append(f'<rect x="{i * cell:.2f}" y="0" width="{max(cell - 0.6, 0.8):.2f}" height="{height}" fill="{fill}"><title>{d:%a %b} {d.day}: {text}</title></rect>')
    if ticks:
        for x, name in _month_ticks(start, days, cell):
            inner.append(f'<text x="{x + 1:.1f}" y="{height + 10}" font-size="8" fill="#92908c">{name}</text>')
    label = f'Next {days} days: {counts["booked"]} booked, {counts["gap"]} in gaps to fill, {counts["open"]} open'
    return _svg(300, height + label_h, ''.join(inner), label)


def heatstrip(start, shares, height=16, uid='hs'):
    """One cell per day; darker = a larger share of the property's units booked that day."""
    days = len(shares)
    if not days:
        return ''
    cell = 300 / days
    inner = []
    for i, share in enumerate(shares):
        d = start + timedelta(days=i)
        if share <= 0:
            fill, opacity = TRACK, 1
        else:
            fill, opacity = INK, 0.3 + 0.7 * min(share, 1)
        inner.append(f'<rect x="{i * cell:.2f}" y="0" width="{max(cell - 0.5, 0.8):.2f}" height="{height}" fill="{fill}" opacity="{opacity:.2f}"><title>{d:%a %b} {d.day}: {share * 100:.0f}% booked</title></rect>')
    booked = sum(1 for s in shares if s >= 1)
    label = f'Next {days} days, darker means more of the units booked: {booked} days fully booked'
    return _svg(300, height, ''.join(inner), label)


def gap_track(gap, today, days=90, height=10):
    """Where in the next `days` days one vacancy gap sits: a thin track with the gap marked."""
    start = max((gap['start'] - today).days, 0)
    span = max(min((gap['end'] - today).days, days) - start, 1)
    cell = 300 / days
    inner = (
        f'<rect x="0" y="3" width="300" height="4" rx="2" fill="{TRACK}"/>'
        f'<rect x="{start * cell:.1f}" y="0" width="{max(span * cell, 4):.1f}" height="{height}" rx="3" fill="{GAP}"><title>{gap["start"]:%b} {gap["start"].day} to {gap["end"]:%b} {gap["end"].day}: {gap["length"]} night(s)</title></rect>'
    )
    return _svg(300, height, inner, f'Gap starts in {start} days and lasts {gap["length"]} nights')


# --- trends ---------------------------------------------------------------------------------------

def trend(values, higher_is_better=True, unit='pct_points', recent=3):
    """The last `recent` complete months against the `recent` before them. `values` are the
    monthly figures, complete months only, oldest first (None for a month with nothing). Returns
    None when there isn't enough to compare, else {'text', 'direction', 'good', 'basis'}: 'good' is
    True/False/None (None = about the same) so the colour says whether the change is welcome."""
    usable = list(values)
    if len(usable) < recent * 2:
        return None
    now = [v for v in usable[-recent:] if v is not None]
    before = [v for v in usable[-recent * 2:-recent] if v is not None]
    if not now or not before:
        return None
    a, b = sum(now) / len(now), sum(before) / len(before)
    if unit == 'pct_points':
        diff = a - b
        text = f'{diff:+.0f} pts'
        flat = abs(diff) < 1
    else:
        if not b:
            return None
        diff = (a - b) / b * 100
        text = f'{diff:+.0f}%'
        flat = abs(diff) < 2
    if flat:
        return {'text': 'steady', 'direction': 'flat', 'good': None, 'basis': f'last {recent} months vs the {recent} before'}
    up = diff > 0
    good = None if higher_is_better is None else (up == higher_is_better)
    return {'text': text, 'direction': 'up' if up else 'down', 'good': good, 'basis': f'last {recent} months vs the {recent} before'}
