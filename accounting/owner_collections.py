"""Owner collections: every owner who is behind and where they stand, as a board of steps.

An owner is first *not in a case* (behind by more than the association's amount, no late letter yet). Opening a case moves them to *Preparing the letter*: the letter is drafted, read through
and made final, the mailing affidavit is prepared, the letter, affidavit and envelope are printed, and the mailing date is confirmed (see letter_prep.py). Only then is the case *Letter sent*.
A letter that went out more than 30 days ago means the owner can go to the lawyer, who sends one 45-day notice (of intent to lien) and then another (of intent to foreclose); 45 days after an owner enters one of those steps
it is time to move them on. After that the case is a lien or foreclosure with the lawyer, and then resolved. The screen shows who is on each step, who needs moving on, and moves them.

Posting the late fees and interest is a different job (the month-end close, and the Post late fees screen): this module only reads what is owed.
"""
from datetime import timedelta

from django.utils import timezone

from . import delinquency, ledger
from .models import ZERO, DelinquencyCase

Stage = DelinquencyCase.Stage
LETTER_DAYS, NOTICE_DAYS = delinquency.LETTER_DAYS, delinquency.NOTICE_DAYS
RESOLVED_SHOWN_DAYS = 180

# the tabs, in the order an owner moves through them: key, label, what the step is
TABS = [
    ('none', 'Not in a case', 'Behind by more than the amount set for this association and no case yet. Open a case: the late letter is drafted and the owner moves to Preparing the letter.'),
    (Stage.PREPARING, 'Preparing the letter', 'The late letter is drafted. Read it through and make it final, prepare the mailing affidavit, print the letter, the affidavit and the envelope, then confirm the day it was mailed: only then is it sent.'),
    (Stage.LETTER, 'Letter sent', 'A late letter has been mailed. Once it went out more than 30 days ago, the owner can go to the lawyer.'),
    (Stage.NOTICE1, 'Lawyer: notice 1', "The lawyer's first 45-day notice (of intent to lien). After 45 days, move the owner on."),
    (Stage.NOTICE2, 'Lawyer: notice 2', "The lawyer's second 45-day notice (of intent to foreclose). After 45 days, move the owner on."),
    (Stage.LIEN, 'Lien or foreclosure', 'With the lawyer: the lien or the foreclosure.'),
    (Stage.RESOLVED, 'Resolved', 'Cases that ended in the last six months.'),
]
MOVE_LABEL = {Stage.LETTER: 'Send to the lawyer', Stage.NOTICE1: 'Move to notice 2', Stage.NOTICE2: 'Move to lien or foreclosure'}


def letter_sent_on(case):
    """The day the owner's latest late letter went out: the day a made letter was mailed (a draft has not gone out), or the day a letter was noted by hand. None when none has gone out."""
    best = None
    for e in case.events.all():
        if e.kind != 'letter':
            continue
        day = e.document.mailed_on if e.document_id else e.date
        if day and (best is None or day > best):
            best = day
    return best


def default_asof(today):
    """The date the late-letter list is worked out for unless someone picks another: the last day of last month. The payments of this month are not entered until its end, so
    who is late is who was late when last month closed."""
    return today.replace(day=1) - timedelta(days=1)


def _dollars(amount):
    return f'${amount:,.0f}' if amount == amount.to_integral_value() else f'${amount:,.2f}'


def months_behind(b, today):
    """How many months the oldest unpaid assessment is overdue (a month is 30 days; at least one for anyone who owes)."""
    if not b['oldest_due']:
        return 1 if b['total'] > 0 else 0
    return max(1, round((today - b['oldest_due']).days / 30))


def case_row(case, today):
    """One open case as a row: what is owed, how long they have been on this step, the alert (if any) and what moving them on is called."""
    b = delinquency.breakdown(case.unit, case.owner)
    since = case.stage_since or case.opened_on
    days_in = max((today - since).days, 0)
    sent = letter_sent_on(case) if case.stage == Stage.LETTER else None
    has_letter = case.events.filter(kind='letter').exists() if case.stage == Stage.LETTER else False
    alert = ''
    prep = None
    if case.stage == Stage.PREPARING:
        from . import letter_prep
        prep = letter_prep.status(case)
        if days_in > letter_prep.WAITING_DAYS:
            alert = f'Waiting to be mailed for {days_in} days'
    elif case.stage == Stage.LETTER:
        if sent is not None and (today - sent).days > LETTER_DAYS:
            alert = f'Letter sent {(today - sent).days} days ago: can go to the lawyer'
        elif sent is None and has_letter:
            alert = 'The letter is still a draft: make it final and mail it'
    elif case.stage in (Stage.NOTICE1, Stage.NOTICE2) and days_in >= NOTICE_DAYS:
        alert = f'{days_in} days in this step: time to move on'
    review_due = bool(case.next_review and case.next_review <= today)
    return {'case': case, 'unit': case.unit, 'owner': case.owner, 'b': b, 'months': months_behind(b, today), 'days_late': (today - b['oldest_due']).days if b['oldest_due'] else 0, 'since': since, 'days_in': days_in, 'sent_on': sent,
            'days_since_letter': (today - sent).days if sent else None, 'alert': alert, 'review_due': review_due, 'paid': b['total'] <= 0, 'can_move': case.stage in MOVE_LABEL,
            'move_label': MOVE_LABEL.get(case.stage, ''), 'icons': delinquency.case_icons(case), 'needs': bool(alert) or review_due or b['total'] <= 0, 'prep': prep}


def board(prop, today=None, asof=None):
    """{'tabs': [{'key', 'label', 'help', 'rows', 'count', 'alerts'}], 'attention': owners needing a look, 'threshold', 'asof'}. Every tab's alert count is what the tab badge shows.
    The first tab (owners with no case who need a late letter) is worked out as of `asof` (default: the end of last month); the tabs of cases already open show where each stands today."""
    today = today or timezone.localdate()
    default = default_asof(today)
    asof = min(asof, today) if asof else default
    threshold = ledger.profile_for(prop).delinquency_threshold
    open_cases = list(DelinquencyCase.objects.filter(association=prop, resolved_on__isnull=True).select_related('unit', 'owner').prefetch_related('events__document').order_by('stage_since', 'unit__label'))
    rows = {k: [] for k, _, _ in TABS}
    for r in delinquency.delinquent_as_of(prop, asof, threshold):
        if r['case'] is None:
            r['months'] = months_behind(r['b'], asof)
            r['alert'] = 'Behind and no late letter yet'
            rows['none'].append(r)
    for case in open_cases:
        if case.stage in rows:
            rows[case.stage].append(case_row(case, today))
    since = today - timedelta(days=RESOLVED_SHOWN_DAYS)
    for case in DelinquencyCase.objects.filter(association=prop, resolved_on__gte=since).select_related('unit', 'owner').order_by('-resolved_on'):
        rows[Stage.RESOLVED].append({'case': case, 'unit': case.unit, 'owner': case.owner, 'resolved_on': case.resolved_on, 'alert': '', 'needs': False})
    tabs = []
    for key, label, help_text in TABS:
        alerts = sum(1 for r in rows[key] if r.get('needs') or (key == 'none'))
        if key == 'none':
            help_text = (f'Owners who owed more than {_dollars(threshold)} on {asof:%b} {asof.day}, {asof.year} and have no late letter yet. Write the first late letter: it opens their case.')
        elif key != Stage.RESOLVED:
            help_text += ' The amounts owed here are as of today.'
        tabs.append({'key': str(key), 'label': label, 'help': help_text, 'rows': rows[key], 'count': len(rows[key]), 'alerts': alerts})
    attention = len(rows['none']) + sum(1 for k in (Stage.PREPARING, Stage.LETTER, Stage.NOTICE1, Stage.NOTICE2, Stage.LIEN) for r in rows[k] if r['needs'])
    return {'tabs': tabs, 'attention': attention, 'threshold': threshold, 'today': today, 'asof': asof, 'default_asof': default}


def attention(prop, today=None, asof=None):
    """For the close sheet and the list of associations: how many owners need a look, and why. {'count', 'bits': [...], 'open': open cases}. Counted as of the same date the screen
    opens on (the end of last month), so the number here is the number there."""
    data = board(prop, today, asof)
    by = {t['key']: t for t in data['tabs']}
    bits = []
    if by['none']['count']:
        bits.append(f'{by["none"]["count"]} need a late letter')
    n = sum(1 for r in by['preparing']['rows'] if r['alert'])
    if n:
        bits.append(f'{n} late letter{"s" if n != 1 else ""} waiting to be mailed')
    n = sum(1 for r in by['letter']['rows'] if r['alert'])
    if n:
        bits.append(f'{n} can go to the lawyer')
    n = sum(1 for k in ('notice1', 'notice2') for r in by[k]['rows'] if r['alert'])
    if n:
        bits.append(f'{n} lawyer notice{"s" if n != 1 else ""} to move on')
    n = sum(1 for k in ('preparing', 'letter', 'notice1', 'notice2', 'lien') for r in by[k]['rows'] if r['review_due'])
    if n:
        bits.append(f'{n} case{"s" if n != 1 else ""} to review')
    n = sum(1 for k in ('preparing', 'letter', 'notice1', 'notice2', 'lien') for r in by[k]['rows'] if r['paid'])
    if n:
        bits.append(f'{n} paid in full: resolve')
    open_n = sum(by[k]['count'] for k in ('preparing', 'letter', 'notice1', 'notice2', 'lien'))
    return {'count': data['attention'], 'bits': bits, 'open': open_n}
