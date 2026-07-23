from django import template

register = template.Library()

# Lucide icon names (see templates/base.html's lucide script) — one per
# Property.Type, shown to the left of the name on the properties list so
# the 6 kinds of property are visually distinguishable at a glance.
PROPERTY_TYPE_ICONS = {
    'general': 'circle-dot',
    'association': 'users',
    'str': 'bed',
    'ltr': 'key',
    'snowbird': 'eye',
    'commercial': 'store',
}


@register.filter
def property_type_icon(property_type):
    return PROPERTY_TYPE_ICONS.get(property_type, 'circle-dot')
