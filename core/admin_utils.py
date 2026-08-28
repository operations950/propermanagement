"""Shared helpers for ModelAdmin classes across every app."""


def mask_secret(value, show_last=4):
    """For a readonly_fields/list_display method showing a stored secret
    (an OAuth refresh/access token, a public-link token) in Django admin
    without ever rendering the real value — readonly_fields renders a
    plain field's actual value verbatim, which is fine for most fields
    but not one of these: none of them appear anywhere in this app's own
    templates (they're only ever used server-side to build a URL or an
    API call), so admin was the one place they leaked in full to anyone
    with Django-admin access. Shows just enough of the tail to confirm
    which record you're looking at without exposing anything usable."""
    if not value:
        return '(not set)'
    value = str(value)
    if len(value) <= show_last:
        return '•' * len(value)
    return '•' * (len(value) - show_last) + value[-show_last:]
