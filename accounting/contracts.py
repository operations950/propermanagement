"""The association's contracts: the record the law makes us keep and show, what drives each contract's recurring bill, and the alerts (term ending, cancellation notice due) that turn up
on the compliance screen.

Alerts are lines on a "Contract alerts" compliance item (worksessions), one item a day, made a couple of weeks ahead by the daily job.
"""
import base64
import json
import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from . import charges, ledger
from .models import Contract, RecurringBill

logger = logging.getLogger(__name__)
END_ALERTS = (120, 90, 60, 30)         # days before the term ends
HORIZON_DAYS = 14                      # an alert goes on the screen this many days before its date


# ---------------------------------------------------------------- the dates

def current_end(c, today=None):
    """The end of the term we are in: the contract's own end date, or, when it renews by itself and that date has passed, the end of the renewal we are in."""
    today = today or timezone.localdate()
    end = c.end_date
    if end is None:
        return None
    if c.auto_renews and c.renewal_months:
        guard = 0
        while end < today and guard < 240:
            end = charges.add_months(end, c.renewal_months)
            guard += 1
    return end


def notice_deadline(c, today=None):
    end = current_end(c, today)
    return end - timedelta(days=c.notice_days) if end and c.auto_renews and c.notice_days else None


def milestones(c, today=None):
    """[(key, day, label)]: the alerts a contract needs. The key names the term, so a renewal makes new ones."""
    today = today or timezone.localdate()
    end = current_end(c, today)
    if end is None:
        return []
    out = []
    for n in END_ALERTS:
        out.append((f'end{n}:{end}', end - timedelta(days=n), f'{c.name}: the term ends in {n} days ({end:%b} {end.day}, {end.year}).' + (' It renews by itself unless cancelled.' if c.auto_renews else '')))
    dl = notice_deadline(c, today)
    if dl:
        out.append((f'notice30:{end}', dl - timedelta(days=30), f'{c.name}: written notice to cancel is due in 30 days ({dl:%b} {dl.day}, {dl.year}).'))
        out.append((f'notice0:{end}', dl, f'{c.name}: written notice to cancel is due today. After this it renews.'))
    return out


def upcoming(prop, today=None, within=120):
    """For the contracts screen: each active contract with its next date that matters."""
    today = today or timezone.localdate()
    rows = []
    for c in Contract.objects.filter(association=prop).select_related('vendor', 'recurring_bill'):
        end, dl = current_end(c, today), notice_deadline(c, today)
        candidates = [d for d in (dl, end) if d and d >= today]
        nxt = min(candidates) if candidates else None
        state = ''
        if c.is_active and end and end < today:
            state = 'ended'
        elif c.is_active and dl and today <= dl <= today + timedelta(days=within):
            state = 'notice'
        elif c.is_active and end and today <= end <= today + timedelta(days=within):
            state = 'ending'
        rows.append({'c': c, 'end': end, 'deadline': dl, 'next': nxt, 'state': state, 'days': (nxt - today).days if nxt else None})
    rows.sort(key=lambda r: (not r['c'].is_active, r['next'] is None, r['next'] or date.max, r['c'].name))
    return rows


# ---------------------------------------------------------------- the compliance item

def _template():
    from core.models import StaffProfile
    from worksessions.models import Frequency, SessionTemplate
    t = SessionTemplate.objects.filter(name='Contract alerts').first()
    if t is None:
        owner = StaffProfile.objects.filter(is_company_admin=True).order_by('pk').first() or StaffProfile.objects.order_by('pk').first()
        if owner is None:
            return None
        t = SessionTemplate.objects.create(name='Contract alerts', description='A contract is about to end, or its cancellation notice is about to be due. Opened by the contracts, not by a schedule.',
                                           owner=owner, department=StaffProfile.Role.ACCOUNTING, frequency=Frequency.MONTHLY, next_open_date=timezone.localdate(), is_active=False)
    return t


def add_alert(contract, day, label):
    try:
        from worksessions.models import Session, SessionLine
        from .delinquency import review_owner
        template = _template()
        if template is None:
            return None
        owner = review_owner(contract.association)
        session, _ = Session.objects.get_or_create(template=template, period_key=day, defaults={
            'owner': owner, 'department': template.department, 'period_label': f'{day:%b} {day.day}', 'opens_at': day, 'due_at': day})
        if session.status == Session.Status.SUBMITTED:
            session.status = Session.Status.OPEN
            session.save(update_fields=['status'])
        return SessionLine.objects.create(session=session, label=f'{contract.association.name}: {label}', property=contract.association, display_order=session.lines.count())
    except Exception:
        logger.exception('Could not put a contract alert on the compliance screen')
        return None


def ensure_alerts(today=None):
    """Put the alerts that are within two weeks (or just passed) on the compliance screen, once each. Returns how many."""
    today = today or timezone.localdate()
    n = 0
    for c in Contract.objects.filter(is_active=True, confirmed=True, end_date__isnull=False).select_related('association'):
        made = list(c.alerts_made or [])
        for key, day, label in milestones(c, today):
            if key in made or not (today - timedelta(days=30) <= day <= today + timedelta(days=HORIZON_DAYS)):
                continue
            if add_alert(c, day, label) is not None:
                made.append(key)
                n += 1
        if made != list(c.alerts_made or []):
            Contract.objects.filter(pk=c.pk).update(alerts_made=made)
    return n


# ---------------------------------------------------------------- the recurring bill it drives

def sync_bill(contract, next_date=None):
    """Make (or update) the contract's recurring bill from its price, schedule, vendor and account. Needs the date of the next bill the first time."""
    from . import payables
    if not (contract.amount and contract.expense_account_id and contract.frequency != Contract.Frequency.ONCE):
        return None
    vendor = contract.vendor or payables.vendor_for_name(contract.vendor_name or contract.name)
    if vendor is None:
        return None
    if contract.vendor_id is None:
        contract.vendor = vendor
    fields = dict(vendor=vendor, description=contract.name[:200], account=contract.expense_account, amount=contract.amount, frequency=contract.frequency)
    bill = contract.recurring_bill
    if bill is not None:
        for k, v in fields.items():
            setattr(bill, k, v)
        bill.save()
    elif next_date:
        bill = RecurringBill.objects.create(association=contract.association, next_date=next_date, **fields)
        contract.recurring_bill = bill
    contract.save(update_fields=['vendor', 'recurring_bill'])
    return bill


# ---------------------------------------------------------------- reading the file

CONTRACT_TOOL = {
    'name': 'report_contract',
    'description': 'What the contract says about its term and price.',
    'input_schema': {'type': 'object', 'properties': {
        'vendor': {'type': 'string', 'description': 'The company providing the service.'},
        'name': {'type': 'string', 'description': 'What the contract is for, in two or three words: "Landscaping", "Elevator service".'},
        'description': {'type': 'string', 'description': 'One or two plain sentences on what is provided.'},
        'start_date': {'type': 'string', 'description': 'YYYY-MM-DD, or an empty string if not stated.'},
        'end_date': {'type': 'string', 'description': 'The end of the current term, YYYY-MM-DD, or an empty string if not stated.'},
        'auto_renews': {'type': 'boolean', 'description': 'True if it renews by itself unless cancelled.'},
        'renewal_months': {'type': 'integer', 'description': 'How long each renewal lasts, in months (12 if not stated).'},
        'notice_days': {'type': 'integer', 'description': 'Days before the term ends that cancelling must be given in writing (0 if not stated).'},
        'amount': {'type': 'number', 'description': 'The price each time it is billed, in dollars (0 if not stated).'},
        'frequency': {'type': 'string', 'enum': ['monthly', 'quarterly', 'semiannual', 'annual', 'once']},
        'quote': {'type': 'string', 'description': 'The exact words about the term, renewal and cancellation.'}}, 'required': ['vendor', 'name', 'frequency']},
}


def read_contract(file_bytes, filename):
    """Ask Claude what the contract says. Returns a dict of fields for a person to check (nothing is saved here)."""
    from . import documents
    client = documents._client()
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext == 'pdf':
        blocks = [{'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': base64.b64encode(file_bytes).decode()}}]
    else:
        blocks = documents._file_to_content_blocks(file_bytes, '', filename)
    message = client.messages.create(model=documents.MODEL, max_tokens=2000, tools=[CONTRACT_TOOL], tool_choice={'type': 'tool', 'name': 'report_contract'},
                                     messages=[{'role': 'user', 'content': blocks + [{'type': 'text', 'text':
                                         'This is a contract of a condominium association. Report who the vendor is, what it is for, when the term starts and ends, whether it renews by itself, '
                                         'how many days before the end cancelling must be in writing, and the price and how often it is billed. Leave out anything the contract does not say.'}]}])
    block = next((b for b in message.content if getattr(b, 'type', '') == 'tool_use'), None)
    data = dict(block.input) if block else {}

    def day(v):
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None

    def num(v):
        try:
            d = Decimal(str(v))
            return d if d > 0 else None
        except (InvalidOperation, ValueError):
            return None
    return {'vendor_name': str(data.get('vendor') or '')[:200], 'name': str(data.get('name') or filename)[:200], 'description': str(data.get('description') or ''), 'start_date': day(data.get('start_date')),
            'end_date': day(data.get('end_date')), 'auto_renews': bool(data.get('auto_renews')), 'renewal_months': int(data.get('renewal_months') or 12), 'notice_days': int(data.get('notice_days') or 0),
            'amount': num(data.get('amount')), 'frequency': data.get('frequency') if data.get('frequency') in Contract.Frequency.values else 'monthly', 'notes': str(data.get('quote') or '')[:2000]}
