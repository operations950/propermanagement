"""Read an uploaded lease and propose its term, its rent and the rent increases for the life of the lease.

An uploaded lease is an accounting.AssociationDocument of kind 'lease': it is read ONCE (the words of every page are kept, accounting/docreader.py), the same as an association's governing
documents. From the kept words Claude reports the term, the rent, the CAM estimate, the deposit and EVERY rent change over the term (a yearly percent or dollar increase is worked out into
a dollar amount for each year), each with the page and the exact words. That is saved as a LeaseProposal, which changes nothing until a person looks at it and applies it.
With no Claude connection set up, everything else still works.
"""
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from accounting import documents
from accounting.documents import DocumentError
from accounting.models import AssociationDocument, DocumentPage

from .models import Lease, LeaseProposal, LongTermLease, LongTermRentStep, RentStep

logger = logging.getLogger(__name__)
LEASE_QUERY = 'lease term commencement date expiration rent base rent monthly rent annual increase escalation percent cpi additional rent common area maintenance cam security deposit renewal option extend late fee schedule exhibit'
CENT = Decimal('0.01')

LEASE_TOOL = {
    'name': 'report_lease',
    'description': 'Report the term, the rent and the rent changes the lease states.',
    'input_schema': {'type': 'object', 'properties': {
        'term_start': {'type': ['string', 'null'], 'description': 'The date the lease term (or the rent) starts, YYYY-MM-DD.'},
        'term_end': {'type': ['string', 'null'], 'description': 'The last day of the term, YYYY-MM-DD.'},
        'monthly_rent': {'type': ['number', 'null'], 'description': 'The monthly base rent at the start of the term, before any increase.'},
        'cam_monthly': {'type': ['number', 'null'], 'description': 'The monthly CAM / additional rent estimate at the start, when the lease states one.'},
        'security_deposit': {'type': ['number', 'null']},
        'steps': {'type': 'array', 'description': 'EVERY later change in the monthly base rent over the whole term, one entry per change, in date order. When the lease says rent rises by a percent or a dollar amount each year, work the dollar amount out for each year and list each year. The first entry is NOT the starting rent.',
                  'items': {'type': 'object', 'properties': {
                      'date': {'type': 'string', 'description': 'The date the new monthly rent starts, YYYY-MM-DD.'},
                      'monthly_rent': {'type': 'number', 'description': 'The new monthly base rent, to the cent.'},
                      'basis': {'type': 'string', 'description': 'How it was found, such as "3% over the prior year" or "stated in the rent schedule".'},
                      'page': {'type': ['integer', 'null'], 'description': 'The page number as labelled above.'},
                      'quote': {'type': 'string', 'description': 'The exact words that set it.'}},
                      'required': ['date', 'monthly_rent']}},
        'renewal': {'type': 'string', 'description': 'Renewal or extension options and the notice dates, in a sentence or two. Empty when none.'},
        'notes': {'type': 'string', 'description': 'Anything else a property manager must know about the rent (free rent months, a late fee, who pays what). Short.'}},
        'required': ['steps']},
}


def _date(value):
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _money(value):
    try:
        return Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _clean(found):
    """What Claude reported, as plain values ready to save as JSON (dates as ISO text, money as text), steps in date order without repeats or unreadable rows."""
    steps, seen = [], set()
    for s in found.get('steps') or []:
        d, amt = _date(s.get('date')), _money(s.get('monthly_rent'))
        if d is None or amt is None or amt < 0 or d in seen:
            continue
        seen.add(d)
        steps.append({'date': d.isoformat(), 'monthly_rent': str(amt), 'basis': (s.get('basis') or '')[:200], 'page': s.get('page'), 'quote': (s.get('quote') or '')[:600]})
    steps.sort(key=lambda s: s['date'])
    out = {'steps': steps, 'renewal': (found.get('renewal') or '')[:1000], 'notes': (found.get('notes') or '')[:1000]}
    for key in ('term_start', 'term_end'):
        d = _date(found.get(key))
        out[key] = d.isoformat() if d else ''
    for key in ('monthly_rent', 'cam_monthly', 'security_deposit'):
        m = _money(found.get(key))
        out[key] = str(m) if m is not None else ''
    return out


def read_lease(doc, target, user=None):
    """Read one uploaded lease for its terms and rent changes and save a proposal for `target` (a Lease or a LongTermLease). Nothing is applied. Returns the LeaseProposal."""
    documents._ensure_read([doc])
    client = documents._client()
    pages = list(DocumentPage.objects.filter(document=doc).select_related('document').order_by('number'))
    chosen = pages if len(pages) <= 40 else documents.rank_pages(pages, LEASE_QUERY, 32)
    try:
        message = client.messages.create(
            model=documents.MODEL, max_tokens=6000, tools=[LEASE_TOOL], tool_choice={'type': 'tool', 'name': 'report_lease'},
            messages=[{'role': 'user', 'content': [{'type': 'text', 'text': documents._page_text(chosen) + '\n\n' +
                'Read this commercial or residential lease (with its amendments and rent schedules, if any). Report the term, the monthly base rent at the start, the CAM or additional-rent estimate, '
                'the security deposit, and EVERY change in the monthly base rent for the whole term, one entry for each change, each with the date it starts, the new monthly amount to the cent, '
                'the page and the exact words. If the lease says the rent increases by a percent or a fixed dollar amount each year (or on a stated date), calculate the new monthly amount for '
                'each year of the term and list every year. If rent is a stated schedule, copy it. Do not invent anything the document does not support; if it is silent, leave it out.'}]}])
    except Exception:
        logger.exception('Claude could not read a lease')
        raise DocumentError('The lease could not be read right now. Try again in a minute.')
    block = next((b for b in message.content if b.type == 'tool_use'), None)
    data = _clean(block.input if block else {})
    kwargs = {'lease': target} if isinstance(target, Lease) else {'ltr_lease': target}
    LeaseProposal.objects.filter(status=LeaseProposal.Status.PROPOSED, **kwargs).delete()
    return LeaseProposal.objects.create(property=target.property, document=doc, data=data, **kwargs)


@transaction.atomic
def apply_proposal(proposal, user=None, step_dates=None, set_term=True):
    """Put a reviewed proposal on its lease: the term (only dates the lease lacks), the starting rent / CAM / deposit when still empty, the notes, and a rent step for each date in `step_dates`
    (None: all of them). The rent that applies from the start of the term is the base rent, not a step. Returns the number of rent steps made or changed."""
    d = proposal.data or {}
    lease = proposal.lease or proposal.ltr_lease
    commercial = proposal.lease is not None
    start, end = _date(d.get('term_start')), _date(d.get('term_end'))
    if set_term:
        if start and not lease.start_date:
            lease.start_date = start
        if end and not lease.end_date:
            lease.end_date = end
    base = _money(d.get('monthly_rent'))
    rent_field = 'base_rent' if commercial else 'monthly_rent'
    if base is not None and base > 0 and not getattr(lease, rent_field):
        setattr(lease, rent_field, base)
    cam, deposit = _money(d.get('cam_monthly')), _money(d.get('security_deposit'))
    if commercial and cam and not lease.cam_estimate:
        lease.cam_estimate = cam
    if deposit and not lease.security_deposit:
        lease.security_deposit = deposit
    if d.get('renewal') and d['renewal'] not in (lease.renewal_notes or ''):
        lease.renewal_notes = ((lease.renewal_notes + '\n') if lease.renewal_notes else '') + d['renewal']
    wanted = None if step_dates is None else {_date(x) for x in step_dates}
    made = 0
    anchor = lease.start_date or start
    today = timezone.localdate()
    upcoming = []
    for s in d.get('steps') or []:
        day, amt = _date(s.get('date')), _money(s.get('monthly_rent'))
        if day is None or amt is None or (wanted is not None and day not in wanted):
            continue
        if anchor and day <= anchor:
            if not getattr(lease, rent_field):
                setattr(lease, rent_field, amt)       # a "step" on or before the first day is the starting rent
            continue
        note = (s.get('basis') or 'From the lease')[:200]
        if commercial:
            RentStep.objects.update_or_create(lease=lease, effective_date=day, defaults={'monthly_rent': amt, 'note': note})
        else:
            LongTermRentStep.objects.update_or_create(lease=lease, effective_date=day, defaults={'monthly_rent': amt, 'note': note})
        made += 1
        if day > today:
            upcoming.append(day)
    if upcoming and not lease.rate_expires_on:
        lease.rate_expires_on = min(upcoming)
    lease.save()
    proposal.status, proposal.decided_by, proposal.decided_at = LeaseProposal.Status.APPLIED, user, timezone.now()
    proposal.save(update_fields=['status', 'decided_by', 'decided_at'])
    return made


def dismiss_proposal(proposal, user=None):
    proposal.status, proposal.decided_by, proposal.decided_at = LeaseProposal.Status.DISMISSED, user, timezone.now()
    proposal.save(update_fields=['status', 'decided_by', 'decided_at'])


def attach_lease_document(prop, uploaded_file, user=None, name='', kind=AssociationDocument.Kind.LEASE):
    """Keep an uploaded lease (or amendment) so it is read (an AssociationDocument). Returns the document."""
    doc = documents.upload(prop, kind, uploaded_file, user)
    if name:
        doc.name = name[:200]
        doc.save(update_fields=['name'])
    return doc
