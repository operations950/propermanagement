"""Owners who are behind: the list, the late letter that opens a case, the case's steps, and the 30-day review that comes back as a compliance item.

A case opens the moment the first late letter (the Notice of Late Assessment, made from the legal forms) is created for a unit. From then on it is reviewed every 30 days: each review
is a line on a compliance item (worksessions), so it turns up on the daily screen. Reviewing a case, from here or by ticking its line there, moves it on 30 days.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from . import charges, ledger, owners
from .models import ZERO, CaseEvent, Charge, DelinquencyCase

logger = logging.getLogger(__name__)
REVIEW_DAYS = 30
Kind = Charge.Kind
ASSESSMENTS = (Kind.ASSESSMENT, Kind.SPECIAL, Kind.OPENING)


# ---------------------------------------------------------------- what an owner owes

def breakdown(unit, owner, asof=None):
    """{'assessments', 'late_fees', 'interest', 'other', 'total', 'oldest_due'}: what is still unpaid, split by kind. With `asof`, as the account stood that day (charges due by then, less
    the payments applied by then)."""
    out = {'assessments': ZERO, 'late_fees': ZERO, 'interest': ZERO, 'other': ZERO, 'oldest_due': None}
    qs = Charge.objects.filter(unit=unit, owner=owner, is_void=False).prefetch_related('applications')
    if asof:
        qs = qs.filter(due_date__lte=asof)
    for c in qs:
        paid = sum((a.amount for a in c.applications.all() if not asof or a.date <= asof), ZERO)
        bal = c.amount - paid
        if bal <= 0:
            continue
        key = 'late_fees' if c.kind == Kind.LATE_FEE else 'interest' if c.kind == Kind.INTEREST else 'assessments' if c.kind in ASSESSMENTS else 'other'
        out[key] += bal
        if c.kind in ASSESSMENTS and (out['oldest_due'] is None or c.due_date < out['oldest_due']):
            out['oldest_due'] = c.due_date
    out['total'] = out['assessments'] + out['late_fees'] + out['interest'] + out['other']
    return out


def open_case_for(association, unit):
    return DelinquencyCase.objects.filter(association=association, unit=unit, resolved_on__isnull=True).select_related('owner').first()


def case_icons(case):
    """What to draw beside the owner's name: a letter (how many), a call, the scales (the attorney), a payment plan, resolved."""
    if case is None:
        return {}
    kinds = [e.kind for e in case.events.all()]
    return {'letters': kinds.count('letter'), 'calls': kinds.count('call'), 'attorney': case.stage == DelinquencyCase.Stage.ATTORNEY or 'attorney' in kinds,
            'plan': case.stage == DelinquencyCase.Stage.PLAN, 'resolved': case.resolved_on is not None}


# ---------------------------------------------------------------- the list

def delinquent_rows(prop, today, threshold):
    """One row for each owner who owes more than `threshold`: what they owe by kind, how late, the late fee and interest that are ready to be charged, and their case if they have one."""
    fees = {}
    for charge, fee, due in charges._late_fee_rows(prop, today):
        fees[charge.unit_id] = fees.get(charge.unit_id, ZERO) + fee
    interest = {}
    for charge, mf, me, amount in charges._interest_rows(prop, today):
        interest[charge.unit_id] = interest.get(charge.unit_id, ZERO) + amount
    cases = {c.unit_id: c for c in DelinquencyCase.objects.filter(association=prop, resolved_on__isnull=True).select_related('owner', 'unit').prefetch_related('events')}
    rows = []
    for acct in charges.accounts(prop):
        if not acct['current'] or acct['balance'] <= 0:
            continue
        b = breakdown(acct['unit'], acct['owner'])
        if b['total'] <= threshold:
            continue
        case = cases.get(acct['unit'].pk)
        rows.append({'unit': acct['unit'], 'owner': acct['owner'], 'b': b, 'days_late': (today - b['oldest_due']).days if b['oldest_due'] else 0, 'fee_due': fees.get(acct['unit'].pk, ZERO),
                     'interest_due': interest.get(acct['unit'].pk, ZERO), 'case': case, 'icons': case_icons(case), 'needs_letter': case is None})
    rows.sort(key=lambda r: (-r['days_late'], -r['b']['total']))
    return rows


def open_cases(prop, today):
    """Every open case with what it owes now and whether a review is due."""
    out = []
    for case in DelinquencyCase.objects.filter(association=prop, resolved_on__isnull=True).select_related('unit', 'owner').prefetch_related('events').order_by('next_review', 'unit__label'):
        b = breakdown(case.unit, case.owner)
        out.append({'case': case, 'b': b, 'icons': case_icons(case), 'review_due': bool(case.next_review and case.next_review <= today), 'paid': b['total'] <= 0})
    return out


# ---------------------------------------------------------------- a case

def add_event(case, kind, summary, user=None, document=None, on=None):
    return CaseEvent.objects.create(case=case, kind=kind, date=on or timezone.localdate(), summary=summary[:500], document=document, by=user)


def open_case(prop, unit, owner, user=None, on=None):
    """The unit's open case, or a new one (the first late letter opens it): reviewed in 30 days."""
    case = open_case_for(prop, unit)
    if case:
        return case
    today = on or timezone.localdate()
    case = DelinquencyCase.objects.create(association=prop, unit=unit, owner=owner, opened_on=today, next_review=today + timedelta(days=REVIEW_DAYS))
    add_event(case, CaseEvent.Kind.STAGE, 'Case opened with the first late letter.', user, on=today)
    ensure_review_line(case)
    return case


def letter_made(doc, user=None):
    """A Notice of Late Assessment was made for a unit: that opens its case (or adds the letter to the case it has)."""
    unit = doc.unit
    if unit is None:
        return None
    from .models import AssociationProfile
    if not AssociationProfile.objects.filter(property=doc.property, we_do_financials=True).exists():
        return None
    owner = owners.billed_owner(unit)
    if owner is None:
        return None
    case = open_case(doc.property, unit, owner, user)
    if not CaseEvent.objects.filter(case=case, document=doc).exists():
        b = breakdown(unit, owner)
        add_event(case, CaseEvent.Kind.LETTER, f'Late letter made (draft). The account owes ${b["total"]:,.2f}.', user, document=doc)
    return case


def set_stage(case, stage, user=None, note='', attorney=''):
    if stage not in DelinquencyCase.Stage.values or stage == DelinquencyCase.Stage.RESOLVED:
        raise ValueError('Choose a step.')
    case.stage = stage
    if attorney:
        case.attorney_name = attorney.strip()[:120]
    case.save(update_fields=['stage', 'attorney_name'])
    label = DelinquencyCase.Stage(stage).label
    kind = CaseEvent.Kind.ATTORNEY if stage == DelinquencyCase.Stage.ATTORNEY else CaseEvent.Kind.PLAN if stage == DelinquencyCase.Stage.PLAN else CaseEvent.Kind.STAGE
    add_event(case, kind, f'Now: {label}.' + (f' {attorney.strip()}.' if attorney.strip() else '') + (f' {note.strip()}' if note.strip() else ''), user)
    review(case, user, note='', quiet=True)


def review(case, user=None, note='', quiet=False):
    """Someone looked at the case: it comes back in 30 days."""
    today = timezone.localdate()
    if not quiet:
        add_event(case, CaseEvent.Kind.REVIEW, note.strip() or 'Reviewed.', user)
    _close_line(case)
    case.next_review = today + timedelta(days=REVIEW_DAYS)
    case.save(update_fields=['next_review'])
    ensure_review_line(case)


def set_review_date(case, day, user=None):
    _close_line(case)
    case.next_review = day
    case.save(update_fields=['next_review'])
    add_event(case, CaseEvent.Kind.NOTE, f'Next review set for {day:%b} {day.day}, {day.year}.', user)
    ensure_review_line(case)


def resolve(case, user=None, note=''):
    today = timezone.localdate()
    _close_line(case)
    case.stage, case.resolved_on, case.next_review = DelinquencyCase.Stage.RESOLVED, today, None
    case.save(update_fields=['stage', 'resolved_on', 'next_review'])
    add_event(case, CaseEvent.Kind.RESOLVED, note.strip() or 'Resolved.', user)


# ---------------------------------------------------------------- the compliance item

def review_owner(prop):
    """Who the reviews are assigned to: the association's chosen person, else the first company admin."""
    from core.models import StaffProfile
    profile = ledger.profile_for(prop)
    return profile.compliance_owner or StaffProfile.objects.filter(is_company_admin=True, user__is_active=True).order_by('pk').first()


def _template():
    from worksessions.models import Frequency, SessionTemplate
    from core.models import StaffProfile
    t = SessionTemplate.objects.filter(name='Delinquency case reviews').first()
    if t is None:
        owner = StaffProfile.objects.filter(is_company_admin=True).order_by('pk').first() or StaffProfile.objects.order_by('pk').first()
        if owner is None:
            return None
        t = SessionTemplate.objects.create(name='Delinquency case reviews', description='Each open delinquency case comes back every 30 days: look at it and move it on. Opened by the cases, not by a schedule.',
                                           owner=owner, department=StaffProfile.Role.ACCOUNTING, frequency=Frequency.MONTHLY, next_open_date=timezone.localdate(), is_active=False)
    return t


def ensure_review_line(case):
    """A line on the compliance item for the day this case is next due (one item a day, a line for each case due then). Never raises: the case matters more than the reminder."""
    try:
        from worksessions.models import Session, SessionLine
        if case.resolved_on or not case.next_review:
            return None
        line = case.review_line
        if line is not None and line.state == SessionLine.State.PENDING and line.session.due_at == case.next_review:
            return line
        template, owner = _template(), review_owner(case.association)
        if template is None:
            return None
        day = case.next_review
        session, _ = Session.objects.get_or_create(template=template, period_key=day, defaults={
            'owner': owner, 'department': template.department, 'period_label': f'{day:%b} {day.day}', 'opens_at': day, 'due_at': day})
        if session.status == Session.Status.SUBMITTED:
            session.status = Session.Status.OPEN
            session.save(update_fields=['status'])
        line = SessionLine.objects.create(session=session, label=f'{case.association.name}, unit {case.unit.label} ({case.owner.name}): review the delinquency case', property=case.association, unit=case.unit,
                                          display_order=session.lines.count())
        case.review_line = line
        case.save(update_fields=['review_line'])
        return line
    except Exception:
        logger.exception('Could not put the delinquency review on the compliance screen')
        return None


def _close_line(case):
    try:
        from worksessions.models import SessionLine
        line = case.review_line
        if line is not None and line.state == SessionLine.State.PENDING:
            line.state, line.completed_at = SessionLine.State.DONE, timezone.now()
            line.save(update_fields=['state', 'completed_at'])
            from worksessions.services import lifecycle
            lifecycle.close_if_resolved(line.session)
    except Exception:
        logger.exception('Could not close the delinquency review line')


def sync_reviews(today=None):
    """A line ticked done on the compliance screen is a review: move that case on 30 days. Returns how many."""
    from worksessions.models import SessionLine
    n = 0
    for case in DelinquencyCase.objects.filter(resolved_on__isnull=True, review_line__isnull=False).select_related('review_line'):
        if case.review_line.state in SessionLine.RESOLVED_STATES:
            add_event(case, CaseEvent.Kind.REVIEW, 'Reviewed from the compliance screen.')
            case.next_review = (today or timezone.localdate()) + timedelta(days=REVIEW_DAYS)
            case.save(update_fields=['next_review'])
            case.review_line = None
            ensure_review_line(case)
            n += 1
    return n


# ---------------------------------------------------------------- the late letter

def nola_values(prop, unit, values):
    """What the Notice of Late Assessment starts with for a unit: the amounts owed as of its balance date, from the books, and the rates the association charges."""
    from .models import AssociationProfile
    profile = AssociationProfile.objects.filter(property=prop, we_do_financials=True).first()
    owner = owners.billed_owner(unit) if unit else None
    if profile is None or owner is None:
        return {}
    from datetime import date
    try:
        asof = date.fromisoformat(str(values.get('balance_as_of')))
    except (TypeError, ValueError):
        asof = None
    b = breakdown(unit, owner, asof)
    out = {'regular_assessments': f'{b["assessments"] + b["other"]:.2f}', 'late_fees': f'{b["late_fees"]:.2f}', 'interest': f'{b["interest"]:.2f}'}
    if profile.late_fee_percent:
        out['late_fee_rate'] = str(charges._plain(profile.late_fee_percent))
    if profile.interest_rate:
        out['interest_rate'] = str(charges._plain(profile.interest_rate))
    return out
