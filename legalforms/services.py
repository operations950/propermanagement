"""Making a document: start from what the program knows, take what the person types, work out the derived figures, render
the wording, and freeze the result (see models.GeneratedDocument)."""
from datetime import timedelta
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


def _owner_roster(prop):
    """"Unit | Owner(s)" lines for every unit of the association, from the owner records (a sign-in sheet's starting point)."""
    rows = []
    for unit in prop.units.filter(is_active=True).order_by('label'):
        names = ' and '.join(dict.fromkeys(c.name for c in Contact.objects.filter(contact_type=Contact.ContactType.OWNER, units=unit).order_by('name')))
        rows.append(f'{unit.label} | {names}')
    if not rows:
        rows = [f'| {c.name}' for c in Contact.objects.filter(contact_type=Contact.ContactType.OWNER, properties=prop).order_by('name')]
    return chr(10).join(rows)


def run_tokens(run):
    """What a project run already knows (its meeting details, the person entered in it), as prefill tokens."""
    if run is None:
        return {}
    from processes.projects import run_meeting, run_person
    meeting = run_meeting(run)
    person = run_person(run)
    out = {
        'meeting_date': meeting['date'], 'meeting_time': meeting['time'], 'meeting_place': meeting['place'], 'video_link': meeting['video_link'],
    }
    if person:
        out.update({'person_name': person.name, 'run_person_name': person.name, 'person_email': person.email})
    return {k: v for k, v in out.items() if v}


def prefill_tokens(prop, unit, user, run=None):
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
        'person_name': names if unit else '',
        'meeting_place': prop.board_meeting_address or '',
        'owner_roster': _owner_roster(prop),
        'manager_name': (user.get_full_name() or user.username) if user else '',
        'manager_title': co['default_manager_title'],
        'county': co['default_county'],
        'company_phone_display': co['company_phone_display'],
        'company_email': co['company_email'],
        **run_tokens(run),
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


NOTICE_KIND_BY_SLUG = {
    'board-meeting-notice': 'Meeting of the Board of Directors', 'members-meeting-notice': 'Meeting of the Members',
    'annual-meeting-notice': 'Annual Meeting of the Members', 'first-election-notice': 'Annual Meeting of the Members',
}


def initial_values(template, prop, unit, user, parent=None, run=None):
    """What each field starts as: its default, then what the program knows, then what was typed last time for this
    unit, then (for a companion form) the document it goes with."""
    tokens = prefill_tokens(prop, unit, user, run=run)
    out = {}
    for f in template.fields:
        value = f.get('default', '')
        if value == 'today':
            value = timezone.localdate().isoformat()
        elif value == 'last_month_end':
            value = (timezone.localdate().replace(day=1) - timedelta(days=1)).isoformat()
        if f.get('prefill') and tokens.get(f['prefill']):
            value = tokens[f['prefill']]
        if f.get('remember'):
            value = _remembered(template, prop, unit, f['key']) or value
        if parent is not None and f.get('parent'):
            src = parent.mailed_on.isoformat() if (f['parent'] == 'mailed_on' and parent.mailed_on) else (parent.values or {}).get(f['parent'], '')
            if f['parent'] == 'mailed_on' and not src:
                src = (parent.values or {}).get('letter_date', '')
            value = src or value
        if parent is not None and f['key'] == 'notice_kind':
            value = NOTICE_KIND_BY_SLUG.get(parent.template.slug, value)
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
def save_document(template, prop, unit, user, raw_values, parent=None, doc=None, process_step=None):
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
        doc = GeneratedDocument(template=template, property=prop, unit=unit, parent=parent, created_by=user, process_step=process_step)
    doc.template_version = template.version
    doc.values = stored
    doc.body_html = html
    doc.subject = (ctx.get('subject') or '')[:200]
    doc.save()
    if doc.process_step_id:
        step = doc.process_step
        step.response = {**(step.response or {}), 'document_id': doc.pk}
        step.save(update_fields=['response'])
    return doc, {}


def revised_copy(doc, user):
    return GeneratedDocument.objects.create(
        template=doc.template, template_version=doc.template.version, property=doc.property, unit=doc.unit, parent=doc.parent,
        subject=doc.subject, values=doc.values, body_html=doc.body_html, created_by=user, mailed_on=None, process_step=doc.process_step,
    )


# ------------------------------------------------------------------------------------------------ emailing it as a PDF
def suggested_email(doc):
    """Who the document would go to: the person entered in the project it was made for, else nobody."""
    if doc.process_step_id:
        from processes.projects import run_person
        person = run_person(doc.process_step.run)
        if person and person.email:
            return person.email
    return ''


def default_email_subject(doc):
    return f'{doc.template.name} - {doc.property.name}'


def default_email_message(doc):
    co = config.company()
    return (f'Hello,\n\nPlease find your {doc.template.name.lower()} attached.\n\n'
            f'If you have any questions, please contact us at {co["company_phone_display"]} or {co["company_email"]}.\n\nSincerely,\n{co["company_name"]}')


def email_document(doc, to, subject, message):
    """Send the document to one address as a PDF attachment, and record that it went. Only a final document is sent."""
    from django.core.exceptions import ValidationError
    from django.core.mail import EmailMessage
    from django.core.validators import validate_email

    from . import pdf
    if doc.status != GeneratedDocument.Status.FINAL:
        raise FormError('Make the document final before emailing it.')
    try:
        validate_email(to)
    except ValidationError:
        raise FormError('Enter a valid email address.')
    mail = EmailMessage((subject or default_email_subject(doc))[:200], message or default_email_message(doc), to=[to], reply_to=[config.company()['company_email']])
    mail.attach(pdf.pdf_filename(doc), pdf.document_pdf(doc), 'application/pdf')
    if mail.send(fail_silently=False) != 1:
        raise FormError('The email could not be sent. Nothing was recorded; try again.')
    doc.emailed_to, doc.emailed_at = to, timezone.now()
    doc.save(update_fields=['emailed_to', 'emailed_at'])
