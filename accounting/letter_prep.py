"""Preparing the first late letter: the compliance work between "this owner is behind" and "the letter went into the mail".

Opening a case for an owner puts them on the *Preparing the letter* step and drafts their Notice of Late Assessment. Nothing counts as sent until all of this is done: the letter is read
through and made final, the mailing affidavit is prepared, the letter, the affidavit and the envelope are printed (a person marks each printed), and the date it was mailed is confirmed.
Confirming the mailing date records it on the letter (the date the 30 days count from) and moves the case to *Letter sent*.
"""
from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

from . import delinquency
from .models import CaseEvent, CasePrep, DelinquencyCase

Stage = DelinquencyCase.Stage
WAITING_DAYS = 7                       # a case that has sat on this step longer than this is flagged: the letters are waiting to be mailed
PRINTED = ('letter', 'affidavit', 'envelope')


class PrepError(ValueError):
    pass


# ---------------------------------------------------------------- the record

def prep_for(case):
    return CasePrep.objects.get_or_create(case=case)[0]


def _get(case):
    try:
        return case.prep
    except CasePrep.DoesNotExist:
        return None


def attach_letter(case, doc, user=None):
    """The draft late letter that goes with a case on the preparing step."""
    prep = prep_for(case)
    old = prep.letter_doc
    if old is not None and old.pk != doc.pk:
        if old.status != 'draft':
            return prep                                    # a letter already made final stays: this one is a further copy
        prep.letter_doc = None                             # a newer draft replaces an unfinished one
        prep.save(update_fields=['letter_doc'])
        old.delete()
    if prep.letter_doc_id is None:
        prep.letter_doc = doc
        prep.save(update_fields=['letter_doc'])
        delinquency.add_event(case, CaseEvent.Kind.NOTE, 'Late letter drafted. Read it through and make it final.', user, document=doc)
    return prep


# ---------------------------------------------------------------- the owner's mailing address

def mail_row(prop, case):
    """Where the owner's mail goes, as the budget mailing works it out: the seasonal address while it is in season, else the mailing address, else the unit itself.
    {'unit', 'owner', 'lines', 'source', 'note'}"""
    from . import budget_project as bp
    person = case.owner
    profile = getattr(person, 'owner_profile', None)
    name = ((profile.legal_name if profile else '') or person.name).strip()
    if bp._in_season(profile, timezone.localdate()):
        lines, source, note = bp.address_lines(profile.seasonal_address, prop), 'seasonal', 'Seasonal address'
    elif profile and profile.mailing_address.strip():
        lines, source, note = bp.address_lines(profile.mailing_address, prop), 'mailing', ''
    else:
        lines = bp._unit_address(prop, case.unit)
        source, note = ('unit', 'No mailing address on file: the unit address') if lines else ('none', 'No address at all')
    return {'unit': case.unit.label, 'owner': name, 'lines': lines, 'source': source, 'note': note}


# ---------------------------------------------------------------- where each case stands

def status(case):
    """What is done and what is left for one case on the preparing step:
    {'prep', 'letter_doc', 'letter_state' (none, draft, final), 'affidavit_doc', 'affidavit_date', 'printed': {letter, affidavit, envelope: datetime or None},
     'envelope_note', 'missing': [what is left before it can be mailed], 'can_mail'}"""
    prep = _get(case)
    letter = prep.letter_doc if prep else None
    affidavit = prep.affidavit_doc if prep else None
    state = 'none' if letter is None else ('final' if letter.status == 'final' else 'draft')
    printed = {'letter': prep.letter_printed_at if prep else None, 'affidavit': prep.affidavit_printed_at if prep else None, 'envelope': prep.envelope_printed_at if prep else None}
    row = mail_row(case.association, case)
    missing = []
    if letter is None:
        missing.append('draft the letter')
    elif state != 'final':
        missing.append('make the letter final')
    if affidavit is None:
        missing.append('prepare the affidavit')
    if state == 'final' and not printed['letter']:
        missing.append('print the letter')
    if affidavit is not None and not printed['affidavit']:
        missing.append('print the affidavit')
    if not printed['envelope']:
        missing.append('print the envelope')
    if row['source'] == 'none' or not row['lines']:
        missing.append('add the owner\'s mailing address')
    envelope_ok = bool(row['lines']) and row['source'] != 'none'
    # the three things to print, in the order they are listed: what each is, whether there is something to print, whether it can be marked printed yet, and why not
    lines = [
        {'what': 'letter', 'label': 'Letter', 'at': printed['letter'], 'can_print': letter is not None, 'can_mark': state == 'final', 'why': '' if state == 'final' else ('make it final first' if letter else 'no letter yet')},
        {'what': 'affidavit', 'label': 'Affidavit', 'at': printed['affidavit'], 'can_print': affidavit is not None, 'can_mark': affidavit is not None, 'why': '' if affidavit else 'prepare it first'},
        {'what': 'envelope', 'label': 'Envelope', 'at': printed['envelope'], 'can_print': envelope_ok, 'can_mark': envelope_ok, 'why': '' if envelope_ok else 'no mailing address'},
    ]
    return {'prep': prep, 'letter_doc': letter, 'letter_state': state, 'affidavit_doc': affidavit, 'affidavit_date': _affidavit_date(affidavit), 'printed': printed, 'print_lines': lines,
            'envelope_note': row['note'], 'envelope_ok': envelope_ok, 'missing': missing, 'can_mail': not missing,
            'letter_final_by': (prep.letter_final_by.get_full_name() or prep.letter_final_by.username) if prep and prep.letter_final_by_id else '',
            'letter_final_at': prep.letter_final_at if prep else None}


def _affidavit_date(doc):
    if doc is None:
        return None
    from datetime import date
    try:
        return date.fromisoformat(str((doc.values or {}).get('mailing_date')))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- drafting and finalizing

def make_draft(prop, unit, user, asof=None):
    """A draft Notice of Late Assessment for a unit, filled from the books (which opens the owner's case on the preparing step). Returns (document or None, problem or '')."""
    from legalforms import services
    from legalforms.models import DocumentTemplate
    template = DocumentTemplate.objects.filter(slug='nola', is_active=True).first()
    if template is None:
        return None, 'The Notice of Late Assessment form is not set up.'
    values = services.initial_values(template, prop, unit, user, overrides={'balance_as_of': asof.isoformat()} if asof else None)
    raw = {k: ('' if v is None else str(v)) for k, v in values.items()}
    doc, errors = services.save_document(template, prop, unit, user, raw)
    if doc:
        return doc, ''
    return None, f'Unit {unit.label}: ' + '; '.join(str(e) for e in (errors.values() if isinstance(errors, dict) else [errors]))


def draft_missing(case, user=None, asof=None):
    """Draft the letter for a case that has none yet."""
    prep = _get(case)
    if prep is not None and prep.letter_doc_id:
        raise PrepError('This case already has a letter.')
    doc, problem = make_draft(case.association, case.unit, user, asof)
    if doc is None:
        raise PrepError(problem)
    return doc


def finalize_letter(case, user=None):
    prep = _get(case)
    doc = prep.letter_doc if prep else None
    if doc is None:
        raise PrepError('There is no letter to make final yet.')
    if doc.status == 'final':
        return doc
    doc.finalize()
    prep.letter_final_at, prep.letter_final_by = timezone.now(), user
    prep.save(update_fields=['letter_final_at', 'letter_final_by'])
    delinquency.add_event(case, CaseEvent.Kind.NOTE, 'Late letter made final (approved).', user, document=doc)
    return doc


def reopen_letter(case, user=None):
    """Take the letter back to a draft so it can be changed (only before it has been mailed). The marks that it was printed are cleared: the text may change."""
    prep = _get(case)
    doc = prep.letter_doc if prep else None
    if doc is None or doc.status != 'final':
        raise PrepError('The letter is not final.')
    if doc.mailed_on:
        raise PrepError('The letter was mailed: make a revised copy instead.')
    doc.status, doc.finalized_at = 'draft', None
    doc.save(update_fields=['status', 'finalized_at'])
    prep.letter_final_at = prep.letter_final_by = prep.letter_printed_at = prep.letter_printed_by = None
    prep.save(update_fields=['letter_final_at', 'letter_final_by', 'letter_printed_at', 'letter_printed_by'])
    delinquency.add_event(case, CaseEvent.Kind.NOTE, 'Late letter reopened as a draft.', user, document=doc)
    return doc


def prepare_affidavit(case, user=None, day=None, replace=False):
    """The mailing affidavit that goes with the letter, for the day it will be mailed (today unless another is given). It swears to that day, so the mailing date confirmed later must
    be the same. `replace` makes it again for another day (the old draft is deleted)."""
    from legalforms import services
    from legalforms.models import DocumentTemplate
    prep = _get(case)
    letter = prep.letter_doc if prep else None
    if letter is None:
        raise PrepError('Draft the letter first.')
    if prep.affidavit_doc_id and not replace:
        raise PrepError('The affidavit is already prepared.')
    template = DocumentTemplate.objects.filter(slug='nola-affidavit', is_active=True).first()
    if template is None:
        raise PrepError('The mailing affidavit form is not set up.')
    old = prep.affidavit_doc
    if old is not None:
        if old.status == 'final':
            raise PrepError('The affidavit is final: make a revised copy of it instead.')
        prep.affidavit_doc = None
        prep.affidavit_printed_at = prep.affidavit_printed_by = None
        prep.save(update_fields=['affidavit_doc', 'affidavit_printed_at', 'affidavit_printed_by'])
        old.delete()
    day = day or timezone.localdate()
    values = services.initial_values(template, case.association, case.unit, user, parent=letter, overrides={'mailing_date': day.isoformat()})
    raw = {k: ('' if v is None else str(v)) for k, v in values.items()}
    doc, errors = services.save_document(template, case.association, case.unit, user, raw, parent=letter)
    if doc is None:
        raise PrepError('; '.join(str(e) for e in (errors.values() if isinstance(errors, dict) else [errors])))
    prep.affidavit_doc = doc
    prep.save(update_fields=['affidavit_doc'])
    delinquency.add_event(case, CaseEvent.Kind.NOTE, f'Mailing affidavit prepared for {day:%b} {day.day}.', user, document=doc)
    return doc


# ---------------------------------------------------------------- printed

def mark_printed(case, what, user=None):
    """A person says the letter, the affidavit or the envelope was printed. The letter must be final first (a draft is not what is mailed)."""
    prep = _get(case)
    if what not in PRINTED:
        raise PrepError('Choose what was printed.')
    if what == 'letter' and not (prep and prep.letter_doc_id and prep.letter_doc.status == 'final'):
        raise PrepError('Make the letter final before it is marked printed.')
    if what == 'affidavit' and not (prep and prep.affidavit_doc_id):
        raise PrepError('Prepare the affidavit first.')
    if what == 'envelope' and not mail_row(case.association, case)['lines']:
        raise PrepError('There is no mailing address for this owner.')
    prep = prep_for(case)
    setattr(prep, f'{what}_printed_at', timezone.now())
    setattr(prep, f'{what}_printed_by', user)
    prep.save(update_fields=[f'{what}_printed_at', f'{what}_printed_by'])


def unmark_printed(case, what):
    prep = _get(case)
    if prep is None or what not in PRINTED:
        raise PrepError('Choose what to unmark.')
    setattr(prep, f'{what}_printed_at', None)
    setattr(prep, f'{what}_printed_by', None)
    prep.save(update_fields=[f'{what}_printed_at', f'{what}_printed_by'])


# ---------------------------------------------------------------- mailed

def mark_mailed(case, day, user=None):
    """The letter went into the mail on `day`: only when the letter is final and the letter, affidavit and envelope are marked printed. The mailing date is recorded on the letter (the 30
    days count from it), the affidavit is made final, and the case moves to *Letter sent*."""
    if case.stage != Stage.PREPARING:
        raise PrepError('This case is not on the preparing step.')
    st = status(case)
    if not st['can_mail']:
        raise PrepError('Not ready to mark mailed: ' + ', '.join(st['missing']) + '.')
    if day is None:
        raise PrepError('Enter the date the letter was mailed.')
    if day > timezone.localdate():
        raise PrepError('The mailing date cannot be in the future.')
    sworn = st['affidavit_date']
    if sworn is not None and sworn != day:
        raise PrepError(f'The affidavit swears to {sworn:%b} {sworn.day}, {sworn.year}. Mark it mailed on that day, or prepare the affidavit again for {day:%b} {day.day}.')
    letter, affidavit = st['letter_doc'], st['affidavit_doc']
    if affidavit.status != 'final':
        affidavit.finalize()
    letter.mailed_on = day
    letter.save(update_fields=['mailed_on'])
    delinquency.add_event(case, CaseEvent.Kind.LETTER, f'Late letter mailed {day:%b} {day.day}, {day.year}.', user, document=letter, on=day)
    delinquency.set_stage(case, Stage.LETTER, user)
    case.stage_since = day
    case.next_review = day + timedelta(days=delinquency.REVIEW_DAYS)
    case.save(update_fields=['stage_since', 'next_review'])
    delinquency._close_line(case)
    delinquency.ensure_review_line(case)
    return case


def cancel_case(case):
    """Remove a case opened by mistake while it is still on the preparing step, with its draft letter and affidavit (a final letter is kept as a document)."""
    if case.stage != Stage.PREPARING:
        raise PrepError('Only a case still preparing its letter can be cancelled.')
    prep = _get(case)
    docs = [d for d in ((prep.letter_doc, prep.affidavit_doc) if prep else ()) if d is not None and d.status == 'draft']
    delinquency._close_line(case)
    case.delete()
    for d in docs:
        d.delete()


# ---------------------------------------------------------------- printing

def _pdf_parts(cases, attr):
    from legalforms import pdf
    out = []
    for case in cases:
        prep = _get(case)
        doc = getattr(prep, attr) if prep else None
        if doc is not None:
            out.append(pdf.document_pdf(doc))
    return out


def letters_pdf(cases):
    from .budget_project import merge_pdfs
    parts = _pdf_parts(cases, 'letter_doc')
    if not parts:
        raise PrepError('There is no letter to print.')
    return merge_pdfs(parts)


def affidavits_pdf(cases):
    from .budget_project import merge_pdfs
    parts = _pdf_parts(cases, 'affidavit_doc')
    if not parts:
        raise PrepError('There is no affidavit to print.')
    return merge_pdfs(parts)


def envelopes_pdf(prop, cases):
    """A plain size-10 envelope for each owner whose address we have: the owner's mailing name and address, the return address of the management company (not the association)."""
    from legalforms import config
    from . import budget_project as bp
    co = config.company()
    ret = SimpleNamespace(return_address='\n'.join([co['company_name'], co['company_address1'], co['company_city']]))
    rows = [mail_row(prop, c) for c in cases]
    if not bp.deliverable(rows):
        raise PrepError('There is no mailing address to print an envelope for.')
    return bp.envelopes_pdf(prop, rows, project=ret, size='no10')


def waiting(case, today=None):
    """Days a case on the preparing step has been waiting (0 for any other step)."""
    if case.stage != Stage.PREPARING:
        return 0
    today = today or timezone.localdate()
    since = case.stage_since or case.opened_on
    return max((today - since).days, 0)
