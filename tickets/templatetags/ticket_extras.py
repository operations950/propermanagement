from django import template

register = template.Library()

# Mirrors the --role-<code> CSS custom properties defined in
# templates/base.html — one fixed color per department, reused everywhere
# a department needs a badge (dashboard boxes use the CSS vars directly;
# this filter is for places, like a table cell, that need an inline style
# string instead).
DEPARTMENT_COLORS = {
    'property_manager': '#2a78d6',
    'admin': '#4a3aa7',
    'cleaner': '#1baf7a',
    'maintenance': '#eb6834',
    'accounting': '#008300',
    'contractor': '#eda100',
}


@register.filter
def department_badge_style(role):
    color = DEPARTMENT_COLORS.get(role)
    if not color:
        return 'background-color: #e9ecef; color: #495057;'
    return f'background-color: {color}; color: #fff;'
