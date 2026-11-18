from django import template

from core import permissions

register = template.Library()


@register.simple_tag(takes_context=True)
def dark_mode_allowed(context):
    """{% dark_mode_allowed as can_dark %}: whether this person is offered dark mode (core.permissions.can_use_dark_mode). Everyone else is always light."""
    request = context.get('request')
    user = getattr(request, 'user', None) or context.get('user')
    return permissions.can_use_dark_mode(user)
