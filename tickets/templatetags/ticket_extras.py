from django import template

register = template.Library()

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
