"""Making a document: start from what the program knows, take what the person types, work out the derived figures, render
the wording, and freeze the result (see models.GeneratedDocument)."""
from decimal import Decimal, InvalidOperation

from django.template import Context, Engine
from django.utils import timezone

from core.models import Contact

from . import catalog, config
from .models import GeneratedDocument

LOGO_TOKEN = '@@LOGO@@'          # swapped for the logo's current address when a document is shown (see views.show_html)


class FormError(ValueError):
    pass


# --------------------------------------------------------------------------------------------- what the program knows
def _address_parts(prop, unit):
    if prop.street and prop.city and prop.state and prop.zip_code:
        street = f'{prop.street} Unit {unit.label}' if unit else prop.street
        return street, f'{prop.city}, {prop.state} {prop.zip_code}'
    street = f'{prop.address} Unit {unit.label}' if unit else prop.address
    return street, ''


def prefill_tokens(prop, unit, user):
    street, rest = _address_parts(prop, unit)
    owners = Contact.objects.filter(contact_type=Contact.ContactType.OWNER)
    owners = owners.filter(units=unit) if unit else owners.filter(properties=prop)
    names = ' and '.join(dict.fromkeys(c.name for c in owners.order_by('name')))
    co = config.company()
    return {
        'association_name': prop.name,
        'unit_label': unit.label if unit else '',
        'unit_address': f'{street}, {rest}' if rest else street,
        'owner_address': f'{street}\n{rest}' if rest else street,
        'owner_names': names if unit else '',
        'manager_name': (user.get_full_name() or user.username) if user else '',
        'manager_title': co['default_manager_title'],
        'county': co['default_county'],
        'company_phone_display': co['company_phone_display'],
        'company_email': co['company_email'],
    }


def _remembered(template, prop, unit, key):
    qs = GeneratedDocument.objects.filter(template=template, property=prop)
    if unit:
        qs = qs.filter(unit=unit)
    for doc in qs.order_by('-created_at')[:10]:
        value = (doc.values or {}).get(key, '')
        if value not in ('', None):
            return value
    return ''


def initial_values(template, prop, unit, user, parent=None):
    """What each field starts as: its default, then what the program knows, then what was typed last time for this
    unit, then (for a companion form) the document it goes with."""
    tokens = prefill_tokens(prop, unit, user)
    out = {}
    for f in template.fields:
        value = f.get('default', '')
        if value == 'today':
            value = timezone.localdate().isoformat()
        if f.get('prefill') and tokens.get(f['prefill']):
            value = tokens[f['prefill']]
        if f.get('remember'):
            value = _remembered(template, prop, unit, f['key']) or value
        if parent is not None and f.get('parent'):
            src = parent.mailed_on.isoformat() if (f['parent'] == 'mailed_on' and parent.mailed_on) else (parent.values or {}).get(f['parent'], '')
            if f['parent'] == 'mailed_on' and not src:
                src = (parent.values or {}).get('letter_date', '')
            value = src or value
        out[f['key']] = value
    return out


# ------------------------------------------------------------------------------------------------- reading the answers
def _parse(field, raw):
    raw = (raw or '').strip()
    kind = field['type']
    if raw == '':
        return None
    if kind in ('text', 'textarea'):
        return raw
    if kind == 'date':
        from datetime import date
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise FormError('Enter a date.')
    if kind in ('money', 'percent'):
        try:
            d = Decimal(raw.replace('$', '').replace(',', '').replace('%', ''))
        except InvalidOperation:
            raise FormError('Enter a number.')
        return d.quantize(Decimal('0.01')) if kind == 'money' else d
    if kind == 'integer':
        try:
            return int(raw)
        except ValueError:
            raise FormError('Enter a whole number.')
    if kind == 'yesno':
        if raw not in ('Yes', 'No'):
            raise FormError('Choose Yes or No.')
        return raw
    if kind == 'choice':
        if raw not in field.get('choices', []):
            raise FormError('Choose one of the options.')
        return raw
    return raw


def read_values(template, raw_values):
    """(typed values, errors by field key). Blank optional fields come out as ''; a required blank is an error."""
    typed, errors = {}, {}
    for f in template.fields:
        try:
            value = _parse(f, raw_values.get(f['key'], ''))
        except FormError as exc:
            errors[f['key']] = str(exc)
            continue
        if value is None:
            if f.get('required'):
                errors[f['key']] = 'Required.'
            value = ''
        typed[f['key']] = value
    return typed, errors


def _derive(template, typed):
    ctx = dict(typed)
    for f in template.fields:
        value = typed.get(f['key'])
        if value in ('', None):
            for suffix in ('_long', '_short', '_fmt', '_fmt0', '_pct'):
                ctx.setdefault(f['key'] + suffix, '')
            continue
        if f['type'] == 'date':
            ctx[f['key'] + '_long'] = f'{value:%A, %B} {value.day}, {value.year}'
            ctx[f['key'] + '_short'] = catalog.short_date(value)
        elif f['type'] == 'money':
            ctx[f['key'] + '_fmt'] = catalog.money(value)
            ctx[f['key'] + '_fmt0'] = f'${value:,.0f}' if value == value.to_integral_value() else catalog.money(value)
        elif f['type'] == 'percent':
            ctx[f['key'] + '_pct'] = format(value.normalize(), 'f') + '%'
    return ctx


def render(template, typed, extra=None):
    """(body html, derived context). `extra` carries facts that are not fields (the unit)."""
    ctx = _derive(template, typed)
    ctx.update(extra or {})
    ctx.update(config.company())
    ctx['logo_token'] = LOGO_TOKEN
    try:
        catalog.COMPUTE[template.slug](ctx)
    except KeyError:
        pass
    except ValueError as exc:
        raise FormError(str(exc))
    html = Engine.get_default().from_string(template.body).render(Context(ctx))
    return html, ctx


# ------------------------------------------------------------------------------------------------------ saving it
def save_document(template, prop, unit, user, raw_values, parent=None, doc=None):
    """Make (or, for a draft, remake) the document. Returns (document, errors); document is None when there are errors."""
    if doc is not None and doc.status != GeneratedDocument.Status.DRAFT:
        raise FormError('A final document cannot be changed — make a revised copy.')
    typed, errors = read_values(template, raw_values)
    if errors:
        return None, errors
    try:
        html, ctx = render(template, typed, extra={'unit_label': unit.label if unit else ''})
    except FormError as exc:
        return None, {'general': str(exc)}
    stored = {f['key']: (typed[f['key']].isoformat() if hasattr(typed[f['key']], 'isoformat') else str(typed[f['key']])) if typed[f['key']] != '' else '' for f in template.fields}
    if doc is None:
        doc = GeneratedDocument(template=template, property=prop, unit=unit, parent=parent, created_by=user)
    doc.template_version = template.version
    doc.values = stored
    doc.body_html = html
    doc.subject = (ctx.get('subject') or '')[:200]
    doc.save()
    return doc, {}


def revised_copy(doc, user):
    return GeneratedDocument.objects.create(
        template=doc.template, template_version=doc.template.version, property=doc.property, unit=doc.unit, parent=doc.parent,
        subject=doc.subject, values=doc.values, body_html=doc.body_html, created_by=user, mailed_on=None,
    )
