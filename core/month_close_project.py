"""The month-end close as a project with days: each step of the checklist is its own compliance item, due on its working day, and finishes itself when its screen says it is done.

Day 1 is steps 1 to 6 (the bank, the trust accounts, the payouts, the closes), day 2 pays the short-term owners and Proper and does the taxes, day 3 checks the long-term rent and closes the
associations, day 4 pays the long-term owners, closes the commercial properties and checks the lease renewals. Owners must be paid by the 5th. Days are working days: Monday to Friday, less the major US holidays.
The items are made together on day 1 so the whole plan shows; the compliance screen shows what is due each day.
"""
import logging
from datetime import datetime

from django.utils import timezone

from . import ledger, workdays

logger = logging.getLogger(__name__)

STEP_DAY = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 2, 11: 3, 12: 3, 13: 4, 14: 4, 15: 4}
OWNER_STEPS = (7, 13)          # the owner payments: due by the 5th
PREFIX = 'Month-end close: '
NOTE_KEY = 'close-month:'

# The screen each step is done on: (the screen's name, whether it is opened for one month). The checklist (close_views.close_steps) gives the same addresses.
SCREENS = {1: ('close_bank_tieout', True), 2: ('close_misc', True), 3: ('close_escrow', True), 4: ('close_payouts', False), 5: ('close_overview', True), 6: ('close_long_term', True),
           7: ('close_pay_owners', True), 8: ('close_pay_proper', True), 9: ('tourist_tax', True), 10: ('close_sales_tax', True), 11: ('close_rent_collection', True),
           12: ('accounting:home', True), 13: ('close_pay_ltr_owners', True), 14: ('accounting:home_commercial', True), 15: ('commercial:renewals', True)}


def screen_url(session):
    """For a month-end close item, the address of its step's own screen for the month it is for, so the item can open there instead of on a screen of its own.
    None for any other item, or for one whose step or month cannot be read."""
    from django.urls import reverse
    name = session.template.name if session.template_id else ''
    notes = session.notes or ''
    if not name.startswith(PREFIX) or not notes.startswith(NOTE_KEY):
        return None
    month_text = notes[len(NOTE_KEY):len(NOTE_KEY) + 7]
    try:
        datetime.strptime(month_text, '%Y-%m')
        screen = SCREENS.get(int(name[len(PREFIX):].split('.', 1)[0]))
    except ValueError:
        return None
    if screen is None:
        return None
    return reverse(screen[0]) + (f'?month={month_text}' if screen[1] else '')


def _template(step):
    from worksessions.models import Frequency, SessionTemplate
    from .models import StaffProfile
    name = f'{PREFIX}{step["n"]}. {step["title"]}'
    t = SessionTemplate.objects.filter(name=name).first()
    if t is None:
        owner = StaffProfile.objects.filter(is_company_admin=True).order_by('pk').first() or StaffProfile.objects.order_by('pk').first()
        if owner is None:
            return None
        t = SessionTemplate.objects.create(name=name, description='A step of the month-end close checklist. Made by the close, not by a schedule; it finishes itself when the step is done.', owner=owner,
                                           department=StaffProfile.Role.ACCOUNTING, frequency=Frequency.MONTHLY, next_open_date=timezone.localdate(), is_active=False, link_url='')
    return t


def ensure_items(close_month, today=None):
    """One compliance item for each step of the month's close, due on the step's working day. Returns how many were made."""
    from worksessions.models import Session, SessionLine
    from .close_views import close_steps
    today = today or timezone.localdate()
    close_month = ledger.month_of(close_month)
    made = 0
    for step in close_steps(close_month, today):
        template = _template(step)
        if template is None:
            return made
        session = Session.objects.filter(template=template, period_key=step['due']).first()
        if session is not None:
            continue
        label = f'{step["title"]}: {close_month:%B %Y} close'
        if step.get('deadline'):
            label += f' (owners must be paid by {step["deadline"]:%b} {step["deadline"].day})'
        session = Session.objects.create(template=template, owner=template.owner, department=template.department, period_label=f'{close_month:%B %Y} close', period_key=step['due'],
                                         opens_at=step['due'], due_at=step['due'], notes=f'{NOTE_KEY}{close_month:%Y-%m}')
        SessionLine.objects.create(session=session, label=label)
        made += 1
    return made


def finish_session(session):
    """Mark an item's lines done and close it. True if it closed."""
    from worksessions.models import SessionLine
    from worksessions.services import lifecycle
    SessionLine.objects.filter(session=session, state=SessionLine.State.PENDING).update(state=SessionLine.State.DONE, completed_at=timezone.now())
    return lifecycle.close_if_resolved(session)


def finish_step(n, close_month, status):
    """The moment a step shows done on its own screen, close its item for that month, so the home screen does not keep it as work to do until the slow background check comes round.
    Takes the step's number and the status the screen just worked out; anything but 'done' does nothing. Returns how many items it closed."""
    from worksessions.models import Session
    if status != 'done':
        return 0
    close_month = ledger.month_of(close_month)
    sessions = Session.objects.filter(template__name__startswith=f'{PREFIX}{n}. ', status=Session.Status.OPEN, notes=f'{NOTE_KEY}{close_month:%Y-%m}').select_related('template')
    return sum(1 for session in sessions if finish_session(session))


def finish_done_items(close_month, steps):
    """For the checklist, which has just worked out every step: close the item of each step that is done."""
    return sum(finish_step(step['n'], close_month, step['status']) for step in steps)


def sync(today=None):
    """Make the items for this month's close once its first working day has come, and finish the ones whose steps are done. Returns (made, finished)."""
    from worksessions.models import Session
    from .close_views import close_steps
    today = today or timezone.localdate()
    made = 0
    if today >= workdays.workday_of_month(today.year, today.month, 1):
        try:
            made = ensure_items(ledger.previous_month(today), today)
        except Exception:
            logger.exception('Could not make the month-end close items')
    finished, by_month = 0, {}
    for session in Session.objects.filter(template__name__startswith=PREFIX, status=Session.Status.OPEN).select_related('template'):
        month_text = (session.notes or '').replace(NOTE_KEY, '').strip()
        if len(month_text) != 7:
            continue
        by_month.setdefault(month_text, []).append(session)
    for month_text, sessions in by_month.items():
        close_month = ledger.month_of(datetime.strptime(month_text, '%Y-%m').date())
        try:
            steps = {f'{PREFIX}{s["n"]}. {s["title"]}': s for s in close_steps(close_month, today)}
        except Exception:
            logger.exception('Could not read the month-end close steps for %s', month_text)
            continue
        for session in sessions:
            step = steps.get(session.template.name)
            if step is not None and step['status'] == 'done' and finish_session(session):
                finished += 1
    return made, finished
