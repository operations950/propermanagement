"""The budget project: the steps around one association's budget for a year, and the paper that goes with them.

The numbers live on the Budget and its four versions (budgeting.py). This module adds what happens around them: the seven steps and where each stands, the mailing list taken from the
owner records, the envelopes, the notice of the budget meeting and the affidavit of mailing (made with the legal forms), and what loading the approved budget for its year does.
"""
import csv
import io
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from legalforms import config as lf_config
from legalforms import services as lf_services
from legalforms.models import DocumentTemplate

from . import budgeting as bg
from . import ledger, owners
from . import standard_chart as sc
from .models import ZERO, BudgetProject, ChargeRule, JournalRule

CENT = Decimal('0.01')
PERIODS = {'monthly': 12, 'quarterly': 4, 'semiannual': 2, 'annual': 1}
MAIL_METHODS = ['mailed by first class mail', 'mailed by certified mail', 'delivered by hand', 'transmitted by email']
DEFAULT_PURPOSE = 'the operating expenses of the association and contributions to its reserves, as itemized in the enclosed proposed budget.'
DEFAULT_AFFIANT = 'Justin Browne'
DEFAULT_TITLE = 'Property Manager'


class ProjectError(Exception):
    pass


def get_project(bud):
    return BudgetProject.objects.filter(budget=bud).first() if bud else None


def ensure_project(bud):
    return BudgetProject.objects.get_or_create(budget=bud)[0]


# ---------------------------------------------------------------- the steps

def _titles(year):
    return [('draft', 'Management company draft'), ('board', 'Board draft'), ('mailing', 'Final draft for mailing'), ('mailed', 'Mailed to the owners'),
            ('meeting', 'The meeting'), ('locked', 'Final locked budget'), ('loaded', f'Loaded into the system for {year}')]


def _d(day):
    return f'{day:%b} {day.day}, {day.year}'


def step_list(prop, year, bud, have, project):
    """The seven steps, in order: [{'n', 'key', 'title', 'status': todo|doing|done, 'detail', 'enabled'}]. A step is enabled once the one before it is done, or once it has been reached;
    every step already done stays open, so nothing locks anyone out of going back."""
    f = {k: k in have for k in ('draft', 'board', 'meeting', 'approved')}
    p = project
    final, notice, mailed, held, loaded = (bool(p and p.final_at), bool(p and p.notice_doc_id), bool(p and p.mailed_on), bool(p and p.held_on), bool(p and p.loaded_at))
    anyafter = f['board'] or f['meeting'] or f['approved']
    legacy = f['approved'] and p is None          # approved on the budget screens before the steps were tracked: nothing here to load or to chase
    done = {
        'draft': anyafter,
        'board': final or notice or f['meeting'] or f['approved'],
        'mailing': notice or f['approved'],
        'mailed': mailed or f['approved'],
        'meeting': held or f['approved'],
        'locked': f['approved'],
        'loaded': loaded or legacy,
    }
    started = {
        'draft': f['draft'],
        'board': f['board'],
        'mailing': final or f['board'],
        'mailed': notice,
        'meeting': mailed or f['meeting'],
        'locked': f['meeting'],
        'loaded': f['approved'],
    }
    notes = {}
    notes['draft'] = {'todo': 'Not started.', 'doing': 'Made from the closed months. Change it until it is ready for the board.', 'done': 'Sent to the board.'}
    notes['board'] = {'todo': 'After the draft is sent to the board.', 'doing': "Put the board's changes in. When it is right, go straight on to the packet.",
                      'done': (f'Final for mailing {_d(p.final_at.date())}.' if final else 'Went on to the meeting.')}
    notes['mailing'] = {'todo': 'After the Board draft is final.', 'doing': 'Make the notice, then print the packet.',
                        'done': ('Notice made ' + _d(p.notice_doc.created_at.date()) + '.') if notice else 'Not recorded here.'}
    notes['mailed'] = {'todo': 'After the notice is made.', 'doing': 'Record when and how it went out. That makes the affidavit.',
                       'done': (f'Mailed {_d(p.mailed_on)}{" by " + p.mailed_by if p.mailed_by else ""}, {p.mailed_count if p.mailed_count is not None else "?"} owners'
                                + (f', {(p.meeting_date - p.mailed_on).days} days before the meeting.' if p.meeting_date else '.')) if mailed else 'Not recorded here.'}
    notes['meeting'] = {'todo': 'After it is mailed.', 'doing': 'Prepare the meeting version, run the meeting, then mark it over.',
                        'done': (f'Held {_d(p.held_on)}.') if held else 'Held.'}
    notes['locked'] = {'todo': 'After the meeting.', 'doing': 'Approve and lock the budget the meeting agreed on.', 'done': 'Approved and locked.'}
    notes['loaded'] = {'todo': 'After the budget is locked.', 'doing': 'Put the approved budget to work for the new year.',
                       'done': (p.loaded_note or f'Loaded {_d(p.loaded_at.date())}.') if loaded else 'Approved before the steps were tracked here.'}
    out, prev_done = [], True
    for i, (key, title) in enumerate(_titles(year), 1):
        status = 'done' if done[key] else 'doing' if started[key] else 'todo'
        out.append({'n': i, 'key': key, 'title': title, 'status': status, 'detail': notes[key][status], 'enabled': prev_done or status != 'todo'})
        prev_done = prev_done and done[key]
    return out


def chip(status):
    return {'done': ('done', 'Done'), 'doing': ('attention', 'In progress'), 'todo': ('', 'Not started')}[status]


def summary(prop, year, bud, have, project):
    """For the budgets list: how many steps are done and the one to do next."""
    steps = step_list(prop, year, bud, have, project)
    nxt = next((s for s in steps if s['status'] != 'done'), None)
    return {'done': sum(1 for s in steps if s['status'] == 'done'), 'of': len(steps), 'next': nxt}


# ---------------------------------------------------------------- the meeting details

def meeting_ready(project):
    return bool(project and project.meeting_date and project.meeting_time and project.meeting_place)


def days_before(project):
    return (project.meeting_date - project.mailed_on).days if project and project.meeting_date and project.mailed_on else None


# ---------------------------------------------------------------- the mailing list

def address_lines(text):
    """An address as printed lines. One line with commas ("7581 Mackenzie Ct, Lake Worth, FL 33467") becomes the street, then the city, state and zip."""
    lines = [l.strip(' ,') for l in re.split(r'[\r\n]+', text or '') if l.strip(' ,')]
    if len(lines) == 1 and lines[0].count(',') >= 2:
        parts = [p.strip() for p in lines[0].split(',')]
        lines = [', '.join(parts[:-2]), ', '.join(parts[-2:])]
    return lines


def _unit_address(prop, unit):
    if not (prop.address or prop.street):
        return []
    street, rest = lf_services._address_parts(prop, unit)
    return [l for l in (street, rest) if l]


def _in_season(profile, on):
    return bool(profile and profile.seasonal_address.strip() and profile.seasonal_from and profile.seasonal_to and profile.seasonal_from <= on <= profile.seasonal_to)


def mailing_rows(prop, on=None):
    """One row for every unit: the billed owner's name as on the deed (else the name on the contact) and where the notice goes: the seasonal address while it is in season, else the mailing
    address, else the unit itself (flagged: nothing was on file). A unit with no owner has no address to send to. [{'unit', 'owner', 'lines', 'source', 'note'}]"""
    on = on or timezone.localdate()
    rows = []
    units = sorted(owners.active_units(prop), key=lambda u: [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', u.label)])
    for unit in units:
        person = owners.billed_owner(unit)
        if person is None:
            rows.append({'unit': unit.label, 'owner': '', 'lines': [], 'source': 'noowner', 'note': 'No owner on record'})
            continue
        profile = getattr(person, 'owner_profile', None)
        name = ((profile.legal_name if profile else '') or person.name).strip()
        if _in_season(profile, on):
            lines, source, note = address_lines(profile.seasonal_address), 'seasonal', 'Seasonal address'
        elif profile and profile.mailing_address.strip():
            lines, source, note = address_lines(profile.mailing_address), 'mailing', ''
        else:
            lines = _unit_address(prop, unit)
            source, note = ('unit', 'No mailing address on file: the unit address') if lines else ('none', 'No address at all')
        rows.append({'unit': unit.label, 'owner': name, 'lines': lines, 'source': source, 'note': note})
    return rows


def deliverable(rows):
    return [r for r in rows if r['lines'] and r['source'] not in ('none', 'noowner')]


def flagged(rows):
    return [r for r in rows if r['source'] in ('unit', 'none', 'noowner')]


def mailing_csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Unit', 'Owner', 'Address 1', 'Address 2', 'Address 3', 'Where from'])
    for r in rows:
        lines = (r['lines'] + ['', '', ''])[:3]
        w.writerow([r['unit'], r['owner']] + lines + [r['note'] or ('Mailing address' if r['source'] == 'mailing' else '')])
    return out.getvalue().encode('utf-8-sig')


def mailing_list_pdf(prop, year, rows, title=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    base = ParagraphStyle('ml', fontName='Helvetica', fontSize=9, leading=11)
    head = ParagraphStyle('mlh', parent=base, fontName='Helvetica-Bold', fontSize=13, leading=16, spaceAfter=2)
    sub = ParagraphStyle('mls', parent=base, textColor=colors.HexColor('#556070'), spaceAfter=8)
    buf = io.BytesIO()

    def footer(c, d):
        c.setFont('Helvetica', 8)
        c.setFillColor(colors.HexColor('#556070'))
        c.drawString(0.75 * inch, 0.5 * inch, f'{prop.name}  |  Mailing list, {year} budget notice')
        c.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f'Page {d.page}')

    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.75 * inch, bottomMargin=0.8 * inch, title='Mailing list', author='Proper Realty Co.')
    n, ok = len(rows), len(deliverable(rows))
    data = [['Unit', 'Owner', 'Mailed to', 'Note']]
    style = [('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8.5), ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eef1f6')), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
             ('LINEBELOW', (0, 0), (-1, -1), 0.25, colors.HexColor('#cfd6e2')), ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
             ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4)]
    for i, r in enumerate(rows, 1):
        data.append([Paragraph(r['unit'], base), Paragraph(r['owner'] or '-', base), Paragraph('<br/>'.join(r['lines']) or '-', base), Paragraph(r['note'], base)])
        if r['source'] in ('unit', 'none', 'noowner'):
            style.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#fff4d6')))
    table = Table(data, colWidths=[0.7 * inch, 1.9 * inch, 2.6 * inch, 1.8 * inch], repeatRows=1)
    table.setStyle(TableStyle(style))
    flows = [Paragraph(title or f'Mailing list: {year} budget meeting notice', head), Paragraph(f'{prop.name}. {ok} of {n} units have an owner and an address.', sub), table]
    doc.build(flows, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# ---------------------------------------------------------------- envelopes

def default_return_lines(prop):
    co = lf_config.company()
    return [prop.name, f'c/o {co["company_name"]}', co['company_address1'], co['company_city']]


def return_lines(prop, project):
    own = [l.strip() for l in ((project.return_address if project else '') or '').splitlines() if l.strip()]
    return own or default_return_lines(prop)


def envelopes_pdf(prop, rows, project=None, size='no10', test=False):
    """One envelope per owner, as a PDF whose pages are the paper: size 10 (9.5 x 4.125 in) or a full letter page (8.5 x 11 in, the address in the middle of the sheet, for a large
    envelope). `test` makes just one, so a sheet can be tried in the printer first."""
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    full = size == 'full'
    page = (8.5 * inch, 11 * inch) if full else (9.5 * inch, 4.125 * inch)
    ret = return_lines(prop, project)
    todo = deliverable(rows)
    if test:
        todo = todo[:1] or [{'owner': 'Sample Owner', 'lines': ['123 Sample Street', 'City, ST 12345']}]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=page)
    c.setTitle(f'Envelopes - {prop.name}')
    c.setAuthor('Proper Realty Co.')
    for r in todo:
        # the return address: the top left corner
        x0, top = (0.75 * inch, page[1] - 0.8 * inch) if full else (0.5 * inch, page[1] - 0.5 * inch)
        fs, lead = (11, 13) if full else (9, 10.5)
        c.setFont('Helvetica', fs)
        c.setFillColor(colors.black)
        for i, line in enumerate(ret):
            c.drawString(x0, top - i * lead, line)
        # the address: where the post office reads it
        ax, ay = (3.4 * inch, page[1] - 5.2 * inch) if full else (4.0 * inch, 2.15 * inch)
        fs, lead = (14, 18) if full else (12, 15)
        block = [r['owner']] + list(r['lines'])
        for i, line in enumerate(block):
            c.setFont('Helvetica-Bold' if i == 0 else 'Helvetica', fs)
            c.drawString(ax, ay - i * lead, line)
        if test:
            c.setFont('Helvetica', 7)
            c.setFillColor(colors.HexColor('#8a93a3'))
            c.drawString(0.3 * inch, 0.25 * inch, 'TEST PAGE: check where the address lands, then print them all')
        c.showPage()
    c.save()
    return buf.getvalue()


def merge_pdfs(parts):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for data in parts:
        if data:
            for page in PdfReader(io.BytesIO(data)).pages:
                w.add_page(page)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


# ---------------------------------------------------------------- the notice and the affidavit

def template(slug):
    t = DocumentTemplate.objects.filter(slug=slug).first()
    if t is None:                       # a deploy seeds them; this only matters on a database that has not been
        from django.core.management import call_command
        call_command('seed_legal_templates', verbosity=0)
        t = DocumentTemplate.objects.get(slug=slug)
    return t


def proposed_totals(prop, version):
    rows = bg.line_rows(version, bg.info_for(version))
    return bg.totals(rows, bg.unit_count(prop))


def assessment_summary(prop, year, t):
    """What the budget means for each unit, in words for the notice."""
    groups, basis = bg.unit_groups(prop)
    n = bg.unit_count(prop) or 1
    needed, prior = Decimal(str(t['needed'])), Decimal(str(t['needed_prior']))
    if basis == 'equal':
        new, old = (needed / n / 12).quantize(CENT, ROUND_HALF_UP), (prior / n / 12).quantize(CENT, ROUND_HALF_UP)
        text = f'The regular assessment for each unit is ${new:,.2f} a month (${new * 12:,.2f} a year)'
        return text + (f', compared with ${old:,.2f} a month in {year - 1}.' if old else '.')
    parts = [f'{g["label"]}: ${(needed * Decimal(str(g["fraction"])) / 12).quantize(CENT, ROUND_HALF_UP):,.2f} a month' for g in groups[:8]]
    return 'Regular assessments are divided by each unit\'s share of the common expenses: ' + '; '.join(parts) + ('; and others as shown on the budget.' if len(groups) > 8 else '.')


def last_association_type(prop):
    t = template('budget-meeting-notice')
    return lf_services._remembered(t, prop, None, 'association_type') or 'Condominium'


def make_notice(prop, bud, project, user, have, association_type=None):
    """Make the notice of the budget meeting from the Board draft and the meeting details. The wording is frozen when it is made; changing the budget or the meeting afterwards means making a new one."""
    if 'board' not in have:
        raise ProjectError('There is no Board draft yet.')
    if not meeting_ready(project):
        raise ProjectError('Enter the date, time and place of the meeting first.')
    t = proposed_totals(prop, have['board'])
    needed = Decimal(str(t['needed'])).quantize(CENT, ROUND_HALF_UP)
    if needed <= 0:
        raise ProjectError('The proposed budget needs no assessments, so there is nothing to put in the notice. Check the Board draft.')
    co = lf_config.company()
    raw = {'association_name': prop.name, 'association_type': association_type or last_association_type(prop), 'letter_date': timezone.localdate().isoformat(), 'budget_year': str(bud.year),
           'meeting_date': project.meeting_date.isoformat(), 'meeting_time': project.meeting_time, 'meeting_place': project.meeting_place, 'video_link': '',
           'total_assessments': f'{needed:.2f}', 'assessment_summary': assessment_summary(prop, bud.year, t), 'assessment_purpose': DEFAULT_PURPOSE,
           'contact_phone': co['company_phone_display'], 'contact_email': co['company_email']}
    doc, errors = lf_services.save_document(template('budget-meeting-notice'), prop, None, user, raw)
    if errors:
        raise ProjectError(' '.join(f'{k}: {v}' for k, v in errors.items()))
    doc.finalize()
    project.notice_doc, project.packet_total = doc, needed
    if project.final_at is None:                  # making the notice is the Board draft going final for mailing
        project.final_at = timezone.now()
    project.save(update_fields=['notice_doc', 'packet_total', 'final_at'])
    return doc


def changed_since_notice(prop, project, have):
    """True when the Board draft needs a different amount than the notice was made with."""
    if not (project and project.notice_doc_id and project.packet_total is not None and 'board' in have):
        return False
    now = Decimal(str(proposed_totals(prop, have['board'])['needed'])).quantize(CENT, ROUND_HALF_UP)
    return now != project.packet_total


@transaction.atomic
def record_mailing(prop, bud, project, user, on, method, by, title, posted=None, count=None):
    """Record that the notice and the budget went out: the date, how, who sent it, and the mailing list exactly as it stood. Then make the affidavit of mailing."""
    if not project.notice_doc_id:
        raise ProjectError('Make the notice first.')
    if method not in MAIL_METHODS:
        raise ProjectError('Choose how it was sent.')
    rows = mailing_rows(prop, on)
    sent = len(deliverable(rows))
    n = count if count else sent
    if n <= 0:
        raise ProjectError('There is no owner with an address to send it to. Check the owners of this association first.')
    project.mailed_on, project.mailed_by, project.mail_method, project.mailed_count, project.mailing_snapshot = on, (by or DEFAULT_AFFIANT)[:120], method, n, rows
    notice = project.notice_doc
    notice.mailed_on, notice.mail_method = on, method
    notice.save(update_fields=['mailed_on', 'mail_method'])
    atype = (notice.values or {}).get('association_type') or last_association_type(prop)
    raw = {'association_name': prop.name, 'association_type': atype, 'county': lf_config.company()['default_county'], 'budget_year': str(bud.year), 'meeting_date': project.meeting_date.isoformat(),
           'date_mailed': on.isoformat(), 'mail_method': method, 'notice_count': str(n), 'date_posted': posted.isoformat() if posted else '', 'affiant_name': project.mailed_by,
           'affiant_title': (title or DEFAULT_TITLE)[:120], 'notary_name': '', 'notary_commission_expires': ''}
    doc, errors = lf_services.save_document(template('budget-mailing-affidavit'), prop, None, user, raw, parent=notice)
    if errors:
        raise ProjectError(' '.join(f'{k}: {v}' for k, v in errors.items()))
    doc.finalize()
    project.affidavit_doc = doc
    project.save()
    return doc


def rows_for_packet(prop, project):
    """The mailing list as it went out if it has gone out; else as it stands today."""
    if project and project.mailed_on and project.mailing_snapshot:
        return list(project.mailing_snapshot)
    return mailing_rows(prop)


# ---------------------------------------------------------------- loading the locked budget for its year

def load_plan(prop, bud, have, today=None):
    """What loading the approved budget for its year would do, in numbers: the budget lines (already in the books' budget since it was approved), the assessment each unit pays from the
    first day of the fiscal year, and the monthly reserve contribution. `problems` are reasons it cannot be loaded yet."""
    from . import scheduled
    today = today or timezone.localdate()
    year = bud.year
    plan = {'year': year, 'problems': [], 'groups': [], 'rule': None, 'reserve': None, 'old_rule': None, 'already': False}
    ver = have.get('approved')
    if ver is None:
        plan['problems'].append('The budget is not approved and locked yet.')
        return plan
    units = bg.unit_count(prop)
    if not units:
        plan['problems'].append('This association has no units yet, so there is nobody to assess.')
        return plan
    t = proposed_totals(prop, ver)
    needed = Decimal(str(t['needed'])).quantize(CENT, ROUND_HALF_UP)
    prior = Decimal(str(t['needed_prior']))
    profile = ledger.profile_for(prop)
    first_due = date(year, profile.fiscal_year_start_month or 1, 1)
    plan.update({'needed': needed, 'prior': prior, 'units': units, 'first_due': first_due, 'ends_on': first_due - timedelta(days=1), 'lines': ver.lines.filter(proposed__gt=0).count()})
    groups, basis = bg.unit_groups(prop)
    plan['basis'] = basis
    plan['groups'] = [{'label': g['label'], 'monthly': (needed * Decimal(str(g['fraction'])) / 12).quantize(CENT, ROUND_HALF_UP), 'annual': (needed * Decimal(str(g['fraction']))).quantize(CENT, ROUND_HALF_UP),
                       'old_monthly': (prior * Decimal(str(g['fraction'])) / 12).quantize(CENT, ROUND_HALF_UP)} for g in groups]
    old = ChargeRule.objects.filter(association=prop, is_active=True, is_special=False).order_by('first_due').last()
    plan['old_rule'] = old
    existing = ChargeRule.objects.filter(association=prop, is_active=True, is_special=False, first_due=first_due).first()
    if existing is not None:
        plan['already'] = True
        plan['rule'] = {'existing': existing}
    elif needed <= 0:
        plan['problems'].append('The approved budget needs no assessments, so there is no rule to set.')
    else:
        freq = old.frequency if old and old.frequency in PERIODS else ChargeRule.Frequency.MONTHLY
        ppy = PERIODS[freq]
        per_period = (needed / ppy).quantize(CENT, ROUND_HALF_UP)
        if basis == 'equal':
            method, amount = ChargeRule.Method.FLAT, (per_period / units).quantize(CENT, ROUND_HALF_UP)
            collected = amount * units * ppy
        else:
            method, amount, collected = ChargeRule.Method.SHARE, per_period, per_period * ppy
        plan['rule'] = {'frequency': freq, 'frequency_label': ChargeRule.Frequency(freq).label.lower(), 'method': method, 'amount': amount, 'per_period': per_period, 'collected': collected,
                        'difference': collected - needed, 'income_account': (old.income_account if old else None), 'name': old.name if old else 'Regular assessment',
                        'post_days_ahead': old.post_days_ahead if old else 10, 'ends_old': (old is not None and (old.end_date is None or old.end_date >= first_due))}
    split = scheduled.reserve_split(prop, first_due)
    has_rule = JournalRule.objects.filter(association=prop, kind=JournalRule.Kind.RESERVE).exists()
    plan['reserve'] = {'has_budget': split['has_budget'], 'total': split['total'], 'lines': split['lines'], 'has_rule': has_rule, 'will_create': split['has_budget'] and split['total'] > 0 and not has_rule}
    return plan


@transaction.atomic
def load_apply(prop, bud, have, project, user, do_assessment, do_reserve):
    """Do what the preview showed, for the parts that were ticked. Returns what was done, in words."""
    plan = load_plan(prop, bud, have)
    if plan['problems']:
        raise ProjectError(plan['problems'][0])
    did = []
    bg.write_budget_lines(bud, have['approved'])          # the approved amounts are the year's budget in the books (they were put there when it was approved; this makes sure)
    did.append(f'the {bud.year} budget is the one the books are compared against')
    rule = plan['rule']
    if do_assessment and rule and not plan['already']:
        old = plan['old_rule']
        if rule['ends_old']:
            old.end_date = plan['first_due'] - timedelta(days=1)
            old.save(update_fields=['end_date'])
        income = rule['income_account'] or ledger.account(prop, sc.INCOME_ASSESSMENT)
        ChargeRule.objects.create(association=prop, name=rule['name'], frequency=rule['frequency'], first_due=plan['first_due'], method=rule['method'], amount=rule['amount'],
                                  share_kind=owners.default_share_kind(prop) if rule['method'] == ChargeRule.Method.SHARE else None, income_account=income, post_days_ahead=rule['post_days_ahead'])
        per = f'${rule["amount"]:,.2f} a unit' if rule['method'] == ChargeRule.Method.FLAT else f'${rule["amount"]:,.2f} split by share'
        did.append(f'assessments from {plan["first_due"]:%b} {plan["first_due"].day}, {plan["first_due"].year}: {per}, {rule["frequency_label"]}')
    res = plan['reserve']
    if do_reserve and res['will_create']:
        JournalRule.objects.create(association=prop, kind=JournalRule.Kind.RESERVE, day_of_month=27)
        did.append(f'a reserve contribution of ${res["total"]:,.2f} a month on the 27th')
    project.loaded_at = timezone.now()
    project.loaded_note = ('Loaded: ' + '; '.join(did) + '.')[:300]
    project.save(update_fields=['loaded_at', 'loaded_note'])
    return did
