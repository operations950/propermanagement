from django import template

from core import nudges

register = template.Library()


@register.inclusion_tag('core/_page_alerts.html', takes_context=True)
def page_alerts(context, section):
    """The strip at the top of a page that says what needs attention in that part of the system ({% page_alerts 'onsite' %}). Company admins only; nothing at all when there is nothing to do."""
    request = context.get('request')
    user = getattr(request, 'user', None)
    items = []
    if user is not None and user.is_authenticated and nudges._is_admin(user):
        try:
            items = nudges.section_items(user, section)
        except Exception:
            items = []
    return {'items': items, 'title': nudges.SECTION_TITLE.get(section, '')}
