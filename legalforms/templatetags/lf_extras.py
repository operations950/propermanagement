from django import template

register = template.Library()


@register.filter
def item(mapping, key):
    """mapping[key] in a template, '' when it is not there."""
    try:
        value = mapping.get(key, '')
    except AttributeError:
        return ''
    return '' if value is None else value
