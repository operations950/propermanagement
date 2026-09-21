"""The property page's unit-level, trash-schedule and listing-link forms (the rest of the page's
forms are in core/views.py). Each returns the anchor to land on after the redirect."""
from django.contrib import messages
from django.shortcuts import get_object_or_404

from . import listings, trash
from .models import ListingLink, PropertySystemLocation, Unit

ANCHORS = {
    'save_unit_details': '#units', 'save_listing_links': '#units', 'check_listing_rating': '#units', 'set_listing_rating': '#units',
    'trash_new': '#trash', 'trash_edit': '#trash',
}


def _int(value):
    try:
        return int(str(value).strip()) if str(value).strip() != '' else None
    except (TypeError, ValueError):
        return None


def _decimal(value):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value).strip()) if str(value).strip() != '' else None
    except InvalidOperation:
        return None


def _unit_of(prop, raw):
    """The unit a form is about, None for 'the property itself'."""
    if not raw:
        return None
    return get_object_or_404(Unit, pk=raw, property=prop)


def run(request, prop, action, is_admin):
    post = request.POST
    if action == 'save_unit_details':
        unit = _unit_of(prop, post.get('unit_id'))
        target = unit if unit is not None else prop
        fields = []
        for name in ('bedroom_count', 'bed_count', 'square_footage'):
            if name in post:
                setattr(target, name, _int(post[name]))
                fields.append(name)
        if 'bathroom_count' in post:
            target.bathroom_count = _decimal(post['bathroom_count'])
            fields.append('bathroom_count')
        text_fields = ['lockbox_code', 'alarm_code', 'wifi_network', 'wifi_password', 'access_notes']
        text_fields.append('access_code' if unit is not None else 'door_code')          # a unit's door code / the property's
        for name in text_fields:
            if name in post:
                setattr(target, name, post[name].strip())
                fields.append(name)
        if unit is not None and 'stats_field' in post:      # the checkbox is only on this form, so absent means unticked
            unit.exclude_from_stats = post.get('exclude_from_stats') == 'on'
            fields.append('exclude_from_stats')
        if unit is not None and 'notes' in post:
            unit.notes = post['notes'].strip()
            fields.append('notes')
        if is_admin and 'turnover_price_override' in post:
            target.turnover_price_override = _decimal(post['turnover_price_override'])
            fields.append('turnover_price_override')
        target.save(update_fields=sorted(set(fields)))
        messages.success(request, f'{unit.label if unit else prop.name} saved.')

    elif action == 'save_listing_links':
        unit = _unit_of(prop, post.get('unit_id'))
        problems = 0
        for platform in (ListingLink.Platform.AIRBNB, ListingLink.Platform.VRBO):
            key = f'{platform}_url'
            if key not in post:
                continue
            try:
                listings.set_link(prop, unit, platform, post[key])
            except listings.ListingError as exc:
                problems += 1
                messages.error(request, f'{dict(ListingLink.Platform.choices)[platform]}: {exc}')
        if not problems:
            messages.success(request, 'Listing links saved.')

    elif action == 'check_listing_rating':
        link = get_object_or_404(ListingLink, pk=post.get('link_id'), property=prop)
        if listings.refresh(link):
            messages.success(request, f'{link.get_platform_display()} rating read: {link.rating} ({link.review_count or "?"} reviews).')
        else:
            messages.error(request, f'{link.get_platform_display()}: {link.check_error}')

    elif action == 'set_listing_rating':
        link = get_object_or_404(ListingLink, pk=post.get('link_id'), property=prop)
        try:
            listings.set_manual(link, post.get('rating'), post.get('reviews'))
            messages.success(request, f'{link.get_platform_display()} rating saved.')
        except listings.ListingError as exc:
            messages.error(request, str(exc))

    elif action == 'trash_new':
        entries = []
        for kind in post.getlist('categories'):
            if kind in trash.STANDARD:
                entries.append({'kind': kind, 'label': '', 'days': post.getlist(f'days_{kind}')})
        for i in range(1, trash.MAX_CUSTOM + 1):
            if post.get(f'custom_on_{i}'):
                entries.append({'kind': 'custom', 'label': post.get(f'custom_name_{i}', ''), 'days': post.getlist(f'custom_days_{i}')})
        try:
            trash.replace_schedule(prop, entries, request.user)
            messages.success(request, 'New trash schedule saved (the old one was replaced).' if post.get('had_schedule') else 'Trash schedule saved.')
        except trash.TrashError as exc:
            messages.error(request, str(exc))

    elif action == 'trash_edit':
        changes = {}
        for key in post:
            if key.startswith('rule_') and key[5:].isdigit():
                pk = int(key[5:])
                changes[pk] = {'days': post.getlist(key), 'label': post.get(f'label_{pk}')}
        try:
            trash.edit_schedule(prop, changes, request.user)
            messages.success(request, 'Trash schedule updated.')
        except trash.TrashError as exc:
            messages.error(request, str(exc))
    return ANCHORS.get(action, '')


def add_system_location(request, prop):
    """Adds a shutoff/panel/etc. to the building, or to one unit when unit_id is sent."""
    system_name = request.POST.get('system_name', '').strip()
    location = request.POST.get('location', '').strip()
    unit = _unit_of(prop, request.POST.get('unit_id'))
    if system_name and location:
        PropertySystemLocation.objects.create(property=prop, unit=unit, system_name=system_name, location=location, notes=request.POST.get('notes', '').strip())
        messages.success(request, 'Added.')
    else:
        messages.error(request, 'System name and location are both required.')
