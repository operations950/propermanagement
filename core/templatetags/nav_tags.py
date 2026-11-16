"""{% up_link %} and {% up_url %}: the smart Up link (see core/nav.py).

    {% load nav_tags %}
    {% up_link %}                                  the standard link: arrow, label, small muted text
    {% up_link css="mb-1 no-print" %}              the standard link plus extra classes
    {% up_link bare="ml-back" icon="arrow-left" %} a phone variant: only the classes given, no standard look
    {% up_url %}                                   just the address (the phone top bar's Back button)

Where it goes comes from the view's own `up` (a core.nav.Up put in the context) when it has one, else from the registry in core/nav_map.py, else from the accounting views'
acc_back / acc_back_label, else nothing is drawn.
"""
from django import template

from core import nav

register = template.Library()


def _up(context):
    found = context.get('up')
    if isinstance(found, nav.Up):
        return found
    request = context.get('request')
    if request is not None:
        found = nav.resolve(request)
        if found:
            return found
    if context.get('acc_back'):
        return nav.Up(context['acc_back'], context.get('acc_back_label') or 'Back')
    return nav.Up('', '')


@register.inclusion_tag('core/_up_link.html', takes_context=True)
def up_link(context, css='', bare='', icon='arrow-up', label=''):
    found = _up(context)
    return {'up': found, 'css': css, 'bare': bare, 'icon': icon, 'label': label or found.label}


@register.simple_tag(takes_context=True)
def up_url(context):
    return _up(context).url
