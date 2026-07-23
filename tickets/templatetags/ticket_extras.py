from django import template
from django.utils import timezone

register = template.Library()

NON_ESCALATABLE_STATUSES = ('completed', 'verified', 'cancelled', 'skipped', 'not_applicable')

# Mirrors the --role-<code> CSS custom properties defined in
# templates/base.html — a single tonal ramp in the brand's own steel-blue/
# slate family (not a rainbow categorical palette), reused everywhere a
# department needs a badge (dashboard boxes use the CSS vars directly; this
# filter is for places, like a table cell, that need an inline style string
# instead).
DEPARTMENT_COLORS = {
    'property_manager': '#2c4a61',
    'admin': '#3d6178',
    'cleaner': '#4e7690',
    'maintenance': '#64768a',
    'accounting': '#6e95b2',
    'contractor': '#93b2c6',
}


def _relative_luminance(hex_color):
    hex_color = hex_color.lstrip('#')
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def _linearize(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _linearize(r), _linearize(g), _linearize(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@register.filter
def department_badge_style(role):
    color = DEPARTMENT_COLORS.get(role)
    if not color:
        return 'background-color: #e9ecef; color: #495057;'
    # The ramp's lightest steps (e.g. contractor) don't have enough contrast
    # for white text — pick dark ink instead whenever the background is
    # light enough, rather than assuming every step is dark like the old
    # bold/saturated palette was.
    text_color = '#2b2b2e' if _relative_luminance(color) > 0.4 else '#fff'
    return f'background-color: {color}; color: {text_color};'


# Lucide icon names (see templates/base.html's lucide script) — one per
# department, used anywhere a role needs a quick visual identifier rather
# than reading its full label.
DEPARTMENT_ICONS = {
    'property_manager': 'home',
    'admin': 'shield',
    'cleaner': 'sparkles',
    'maintenance': 'wrench',
    'accounting': 'banknote',
    'contractor': 'hard-hat',
}


@register.filter
def department_icon(role):
    return DEPARTMENT_ICONS.get(role, 'circle-dot')


@register.filter
def is_overdue(ticket, now):
    """True if ticket.due_date's LOCAL calendar date is before now's LOCAL
    calendar date. Deliberately uses timezone.localtime() on both sides —
    the naive `t.due_date.date < now.date` template comparison used
    elsewhere in this app's history compares raw UTC-aware datetimes'
    date() components, which silently disagrees with the business's actual
    local day (settings.TIME_ZONE) for several hours every evening, once
    UTC has rolled to the next calendar day but the local day hasn't yet —
    flagging today's items as Overdue overnight."""
    if not ticket.due_date or ticket.status == 'completed':
        return False
    return timezone.localtime(ticket.due_date).date() < timezone.localtime(now).date()


@register.filter
def is_due_today(ticket, now):
    """True if ticket.due_date's LOCAL calendar date equals now's LOCAL
    calendar date — see is_overdue for why this must use localtime()."""
    if not ticket.due_date or ticket.status == 'completed':
        return False
    return timezone.localtime(ticket.due_date).date() == timezone.localtime(now).date()


@register.filter
def is_escalated(ticket, now):
    """Flag-only escalation (see the build plan): true once a ticket is
    overdue by at least its template's escalation_threshold_days. No
    reassignment happens — this only drives a visible badge/banner."""
    template = ticket.created_from_template
    if not template or not template.escalation_threshold_days or not ticket.due_date:
        return False
    if ticket.status in NON_ESCALATABLE_STATUSES:
        return False
    overdue_days = (timezone.localtime(now).date() - timezone.localtime(ticket.due_date).date()).days
    return overdue_days >= template.escalation_threshold_days
