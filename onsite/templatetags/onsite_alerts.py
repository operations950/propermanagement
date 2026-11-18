from django import template

from onsite import alerts

register = template.Library()


def _items(context):
    """The alerts, worked out once per page (the button and the panel both read them)."""
    request = context.get('request')
    if request is None:
        return []
    if not hasattr(request, '_onsite_alerts'):
        user = getattr(request, 'user', None)
        request._onsite_alerts = alerts.collect(user) if user is not None and user.is_authenticated else []
    return request._onsite_alerts


@register.inclusion_tag('onsite/_alerts_button.html', takes_context=True)
def onsite_alerts_button(context):
    """The plum Alerts button at the right of the On-Site tab row, with the number of alerts; plain when there are none."""
    return {'alerts': _items(context)}


@register.inclusion_tag('onsite/_alerts_panel.html', takes_context=True)
def onsite_alerts_panel(context):
    """The list the Alerts button opens (hidden until it is pressed), drawn under the tab row."""
    return {'alerts': _items(context)}
