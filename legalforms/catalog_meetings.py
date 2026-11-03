"""The meeting, approval and welcome forms: notices of meetings, the affidavit of notice, the first notice of election, the
sign-in sheet, the approval or denial letter and the two welcome letters. Same shape as catalog.py (and merged into its
TEMPLATES / COMPUTE at the bottom of that file), so they are seeded, edited in Django admin and generated exactly like the
Notice of Late Assessment.

Voting is deliberately not in here: no ballot, proxy, tally or certified result. Notices are signed "The Board of
Directors": no director's name appears on any of them."""
from .catalog import F, short_date

CONDO = 'Condominium'
HOA = 'Homeowners association'
ASSOCIATION_TYPES = [CONDO, HOA]

LETTERHEAD = """<table class="lf-head"><tr>
<td><div>{{ association_name }}</div><div class="lf-small">c/o {{ company_name }}</div><div>{{ company_address1 }}</div><div>{{ company_city }}</div></td>
<td class="lf-logo"><img src="{{ logo_token }}" alt="{{ company_name }}"></td>
</tr></table>"""

BOARD_STATUTE = {CONDO: '§718.112(2)(c), Florida Statutes', HOA: '§720.303(2), Florida Statutes'}
ANNUAL_STATUTE = {CONDO: '§718.112(2)(d), Florida Statutes', HOA: '§720.306, Florida Statutes'}
STATUTE_WORD = {CONDO: 'the condominium property', HOA: 'the community'}

DEFAULT_AGENDA = """Call to order and roll call
Proof of notice of the meeting
Approval of the minutes of the previous meeting
Reports of officers and the managing agent
Financial report
Unfinished business
New business
Owner comments on agenda items
Adjournment"""

ANNUAL_AGENDA = """Call to order and roll call; determination of a quorum
Proof of notice of the meeting
Approval of the minutes of the previous annual meeting
Reports of officers and the managing agent
Election of directors
Unfinished business
New business
Owner comments on agenda items
Adjournment"""


def _lines(text):
    return [line.strip() for line in (text or '').splitlines() if line.strip()]


def _common(v):
    kind = v.get('association_type') or CONDO
    v['posting_place'] = STATUTE_WORD.get(kind, 'the association property')


# ------------------------------------------------------------------------------------------- notice of board meeting
BOARD_NOTICE_BODY = """<div class="lf-page">""" + LETTERHEAD + """<div>
<p>{{ letter_date_long }}</p>
<p><strong>NOTICE OF MEETING OF THE BOARD OF DIRECTORS (Pursuant to {{ statute_cite }})</strong></p>
<p><strong>{{ association_name }}</strong></p>
<p>To all members of {{ association_name }}:</p>
<p>Notice is given that a {{ meeting_kind|lower }} meeting of the Board of Directors will be held as follows:</p>
<table class="lf-kv lf-left">
<tr><td>Date:</td><td>{{ meeting_date_long }}</td></tr>
<tr><td>Time:</td><td>{{ meeting_time }}</td></tr>
<tr><td>Place:</td><td>{{ meeting_place }}</td></tr>
{% if video_link %}<tr><td>Join by video:</td><td>{{ video_link }}</td></tr>{% endif %}
</table>
<p>The agenda for the meeting is:</p>
<ol>{% for line in agenda_lines %}<li>{{ line }}</li>{% endfor %}</ol>
<p>Unit owners may attend the meeting and speak on all designated agenda items, as Florida law provides. This notice and the agenda are posted conspicuously on {{ posting_place }} at least 48 continuous hours before the meeting, except in an emergency.</p>
{% if special_item == 'Yes' %}<p>At this meeting the Board will consider {{ special_item_text|default:"a special assessment, the annual budget, or a rule affecting the use of units" }}. This notice is mailed, delivered or electronically transmitted to each unit owner, and posted, not less than 14 days before the meeting.</p>{% endif %}
<p>If you have any questions, please contact us at {{ contact_phone }} or {{ contact_email }}.</p>
<p>Posted on {{ posted_on_long }}.</p>
<p>Sincerely,<br>The Board of Directors</p>
</div></div>"""

BOARD_NOTICE_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('association_type', 'Kind of association', 'choice', choices=ASSOCIATION_TYPES, default=CONDO, remember=True, required=True, help='Decides which statute the notice cites.'),
    F('letter_date', 'Date of the notice', 'date', default='today', required=True),
    F('meeting_kind', 'Kind of meeting', 'choice', choices=['Regular', 'Special', 'Emergency'], default='Regular', required=True),
    F('meeting_date', 'Date of the meeting', 'date', prefill='meeting_date', required=True),
    F('meeting_time', 'Time of the meeting', prefill='meeting_time', required=True, help='For example 6:00 p.m.'),
    F('meeting_place', 'Place of the meeting', prefill='meeting_place', required=True),
    F('video_link', 'Video link (optional)', prefill='video_link'),
    F('agenda', 'Agenda, one item per line', 'textarea', default=DEFAULT_AGENDA, required=True),
    F('special_item', 'Will the Board consider a special assessment, the annual budget or a rule about the use of units?', 'yesno', default='No', required=True,
      help='If yes, the notice adds the 14-day mailed notice paragraph. Mail or deliver it to every owner as well as posting it.'),
    F('special_item_text', 'If yes: what exactly (optional)'),
    F('posted_on', 'Date posted', 'date', default='today', required=True),
    F('contact_phone', 'Phone to call', prefill='company_phone_display'),
    F('contact_email', 'Email to write', prefill='company_email'),
]


def compute_board_notice(v):
    _common(v)
    v['statute_cite'] = BOARD_STATUTE[v['association_type']]
    v['agenda_lines'] = _lines(v['agenda'])
    v['subject'] = f'{v["meeting_kind"]} board meeting — {short_date(v["meeting_date"])}'


# ------------------------------------------------------------------------------------- notice of members meeting
MEMBERS_NOTICE_BODY = """<div class="lf-page">""" + LETTERHEAD + """<div>
<p>{{ letter_date_long }}</p>
<p><strong>NOTICE OF {{ meeting_kind|upper }} MEETING OF THE MEMBERS</strong></p>
<p><strong>{{ association_name }}</strong></p>
<p>To all members of {{ association_name }}:</p>
<p>Notice is given that a {{ meeting_kind|lower }} meeting of the members of {{ association_name }} will be held as follows:</p>
<table class="lf-kv lf-left">
<tr><td>Date:</td><td>{{ meeting_date_long }}</td></tr>
<tr><td>Time:</td><td>{{ meeting_time }}</td></tr>
<tr><td>Place:</td><td>{{ meeting_place }}</td></tr>
{% if video_link %}<tr><td>Join by video:</td><td>{{ video_link }}</td></tr>{% endif %}
</table>
{% if reason %}<p>The purpose of the meeting is: {{ reason }}</p>{% endif %}
<p>The agenda for the meeting is:</p>
<ol>{% for line in agenda_lines %}<li>{{ line }}</li>{% endfor %}</ol>
<p>This notice is mailed, delivered or electronically transmitted to each unit owner, and posted conspicuously on {{ posting_place }}, not less than 14 days before the meeting, as the governing documents and Florida law require.</p>
<p>If you cannot attend, please contact us before the meeting at {{ contact_phone }} or {{ contact_email }}.</p>
<p>Sincerely,<br>The Board of Directors</p>
</div></div>"""

MEMBERS_NOTICE_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('association_type', 'Kind of association', 'choice', choices=ASSOCIATION_TYPES, default=CONDO, remember=True, required=True),
    F('letter_date', 'Date of the notice', 'date', default='today', required=True),
    F('meeting_kind', 'Kind of meeting', 'choice', choices=['Regular', 'Special'], default='Regular', required=True),
    F('meeting_date', 'Date of the meeting', 'date', prefill='meeting_date', required=True),
    F('meeting_time', 'Time of the meeting', prefill='meeting_time', required=True),
    F('meeting_place', 'Place of the meeting', prefill='meeting_place', required=True),
    F('video_link', 'Video link (optional)', prefill='video_link'),
    F('reason', 'Reason for the meeting (optional for a regular meeting)', 'textarea'),
    F('agenda', 'Agenda, one item per line', 'textarea', default=DEFAULT_AGENDA, required=True),
    F('contact_phone', 'Phone to call', prefill='company_phone_display'),
    F('contact_email', 'Email to write', prefill='company_email'),
]


def compute_members_notice(v):
    _common(v)
    v['agenda_lines'] = _lines(v['agenda'])
    v['subject'] = f'{v["meeting_kind"]} members meeting — {short_date(v["meeting_date"])}'


# -------------------------------------------------------------------------------- first notice of election
FIRST_ELECTION_BODY = """<div class="lf-page">""" + LETTERHEAD + """<div>
<p>{{ letter_date_long }}</p>
<p><strong>FIRST NOTICE OF ELECTION AND CALL FOR CANDIDATES (Pursuant to {{ statute_cite }})</strong></p>
<p><strong>{{ association_name }}</strong></p>
<p>To all members of {{ association_name }}:</p>
<p>The annual meeting of the members of {{ association_name }} will be held on {{ meeting_date_long }}. At that meeting {{ seats_phrase }} on the Board of Directors will be filled by election.</p>
<p>Any member eligible to serve who wishes to be considered as a candidate must give written notice of intent to be a candidate to the Association no later than <strong>{{ candidate_deadline_long }}</strong>, delivered to: {{ submit_to }}. Please include your name, your unit and a way to reach you. An eligible candidate may also submit a short information sheet, which the Association will send with the notice of the meeting, as the governing documents and Florida law provide.</p>
<p>The notice of the annual meeting and the agenda will follow. If you have questions, please contact us at {{ contact_phone }} or {{ contact_email }}.</p>
<p>Sincerely,<br>The Board of Directors</p>
</div></div>"""

FIRST_ELECTION_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('association_type', 'Kind of association', 'choice', choices=ASSOCIATION_TYPES, default=CONDO, remember=True, required=True),
    F('letter_date', 'Date of the notice', 'date', default='today', required=True, help='Condominium: not less than 60 days before the election.'),
    F('meeting_date', 'Date of the annual meeting and election', 'date', prefill='meeting_date', required=True),
    F('seats_open', 'Seats to be filled', 'integer', default='3', required=True),
    F('candidate_deadline', 'Last day to give notice of candidacy', 'date', required=True, help='Condominium: at least 40 days before the election.'),
    F('submit_to', 'Where to deliver it', default='Proper Realty Co., 1045 E Atlantic Ave, Suite 309, Delray Beach, FL 33483, or admin@proper-realty.com', remember=True, required=True),
    F('contact_phone', 'Phone to call', prefill='company_phone_display'),
    F('contact_email', 'Email to write', prefill='company_email'),
]


def compute_first_election(v):
    n = v['seats_open']
    v['seats_phrase'] = f'{n} seat' + ('' if n == 1 else 's')
    v['statute_cite'] = ANNUAL_STATUTE[v['association_type']]
    v['subject'] = f'First notice of election — {short_date(v["meeting_date"])}'


# --------------------------------------------------------------------------- notice of annual meeting and agenda
ANNUAL_NOTICE_BODY = """<div class="lf-page">""" + LETTERHEAD + """<div>
<p>{{ letter_date_long }}</p>
<p><strong>NOTICE OF ANNUAL MEETING OF THE MEMBERS AND ELECTION OF DIRECTORS (Pursuant to {{ statute_cite }})</strong></p>
<p><strong>{{ association_name }}</strong></p>
<p>To all members of {{ association_name }}:</p>
<p>Notice is given that the annual meeting of the members of {{ association_name }} will be held as follows:</p>
<table class="lf-kv lf-left">
<tr><td>Date:</td><td>{{ meeting_date_long }}</td></tr>
<tr><td>Time:</td><td>{{ meeting_time }}</td></tr>
<tr><td>Place:</td><td>{{ meeting_place }}</td></tr>
{% if video_link %}<tr><td>Join by video:</td><td>{{ video_link }}</td></tr>{% endif %}
</table>
{% if candidate_lines %}<p>The candidates for the Board of Directors are:</p>
<ul>{% for line in candidate_lines %}<li>{{ line }}</li>{% endfor %}</ul>{% endif %}
{% if voting_note %}<p>{{ voting_note }}</p>{% endif %}
<p>The agenda for the meeting is:</p>
<ol>{% for line in agenda_lines %}<li>{{ line }}</li>{% endfor %}</ol>
<p>This notice and the agenda are mailed, delivered or electronically transmitted to each member, and posted conspicuously on {{ posting_place }}, not less than 14 days before the meeting, as the governing documents and Florida law require.</p>
<p>If you have questions, please contact us at {{ contact_phone }} or {{ contact_email }}.</p>
<p>Sincerely,<br>The Board of Directors</p>
</div></div>"""

ANNUAL_NOTICE_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('association_type', 'Kind of association', 'choice', choices=ASSOCIATION_TYPES, default=CONDO, remember=True, required=True),
    F('letter_date', 'Date of the notice', 'date', default='today', required=True, help='Condominium: mailed 14 to 34 days before the election (the second notice).'),
    F('meeting_date', 'Date of the annual meeting', 'date', prefill='meeting_date', required=True),
    F('meeting_time', 'Time of the meeting', prefill='meeting_time', required=True),
    F('meeting_place', 'Place of the meeting', prefill='meeting_place', required=True),
    F('video_link', 'Video link (optional)', prefill='video_link'),
    F('candidates', 'Candidates, one per line (optional)', 'textarea', parent='candidates'),
    F('voting_note', 'A line about how voting will be handled (optional)', 'textarea', help='Ballots and proxies are prepared outside the system; say here how members receive and return them.'),
    F('agenda', 'Agenda, one item per line', 'textarea', default=ANNUAL_AGENDA, required=True),
    F('contact_phone', 'Phone to call', prefill='company_phone_display'),
    F('contact_email', 'Email to write', prefill='company_email'),
]


def compute_annual_notice(v):
    _common(v)
    v['statute_cite'] = ANNUAL_STATUTE[v['association_type']]
    v['agenda_lines'] = _lines(v['agenda'])
    v['candidate_lines'] = _lines(v.get('candidates'))
    v['subject'] = f'Annual meeting — {short_date(v["meeting_date"])}'


# ----------------------------------------------------------------------------------------- affidavit of notice
NOTICE_AFFIDAVIT_BODY = """<div class="lf-page">
<p class="lf-center">AFFIDAVIT OF NOTICE<br>{{ notice_title|upper }}</p>
<table class="lf-plain"><tr><td>STATE OF FLORIDA</td><td>)</td></tr><tr><td>COUNTY OF {{ county|upper }}</td><td>)</td></tr></table>
<p>I, {{ affiant_name }}, the {{ affiant_title }} of {{ association_name }} (the "Association"), hereby state and affirm as follows. In accordance with {{ statute_cite }}, I caused the Notice of {{ notice_kind }} to be held on {{ meeting_date_short }} to be posted conspicuously on {{ posting_place }} on {{ date_posted_short }}{% if time_posted %} at {{ time_posted }}{% endif %}{{ mailed_clause }}.</p>
<div class="lf-sig"><div class="lf-line"></div><div>{{ affiant_name }}, as {{ affiant_title }}</div></div>
<p>Sworn to (or affirmed) and subscribed before me by means of physical presence notarization this {{ notary_day }} day of {{ notary_month_year }}, by {{ affiant_name }} as {{ affiant_title }} of {{ association_name }}.</p>
<div class="lf-notary">
<div class="lf-left">[Notary Seal]</div>
<div class="lf-right">
<div class="lf-line"></div>
{% if notary_name %}<div>{{ notary_name }}</div>{% endif %}
{% if notary_commission_expires %}<div>My Commission Expires: {{ notary_commission_expires }}</div>{% endif %}
<div class="lf-tick"><span class="lf-blank"></span> Personally Known, OR</div>
<div class="lf-tick"><span class="lf-blank"></span> Produced Identification</div>
<div class="lf-idrow">Type of Identification Produced: <span class="lf-line-inline"></span></div>
<div class="lf-idrow">Driver's License No.: <span class="lf-line-inline"></span></div>
</div>
</div>
</div>"""

NOTICE_KINDS = ['Meeting of the Board of Directors', 'Meeting of the Members', 'Annual Meeting of the Members', 'Special Assessment Meeting of the Board of Directors']

NOTICE_AFFIDAVIT_FIELDS = [
    F('association_name', 'Association', parent='association_name', prefill='association_name', required=True),
    F('association_type', 'Kind of association', 'choice', choices=ASSOCIATION_TYPES, default=CONDO, parent='association_type', remember=True, required=True),
    F('county', 'County', prefill='county', required=True),
    F('notice_kind', 'The notice posted', 'choice', choices=NOTICE_KINDS, default=NOTICE_KINDS[0], required=True),
    F('meeting_date', 'Date of the meeting', 'date', parent='meeting_date', prefill='meeting_date', required=True),
    F('date_posted', 'Date it was posted', 'date', parent='posted_on', required=True, help='Swear only to the day it really went up.'),
    F('time_posted', 'Time it was posted (optional)'),
    F('mailed', 'Was it also mailed, delivered or emailed to the owners?', 'yesno', default='No', required=True),
    F('date_mailed', 'If yes: date', 'date', parent='mailed_on'),
    F('mail_method', 'If yes: how', 'choice', choices=['mailed by first class mail', 'delivered by hand', 'transmitted by email'], default='mailed by first class mail'),
    F('affiant_name', 'Person swearing (who posted it)', default='Justin Browne', required=True),
    F('affiant_title', 'Their title', default='Property Manager', required=True),
    F('notary_name', 'Notary (optional — prints under the signature line)', remember=True),
    F('notary_commission_expires', 'Notary commission expires (optional)', remember=True),
]


def compute_notice_affidavit(v):
    _common(v)
    kind = v['association_type']
    v['statute_cite'] = ANNUAL_STATUTE[kind] if v['notice_kind'].startswith('Annual') else BOARD_STATUTE[kind]
    v['notice_title'] = 'Notice of ' + v['notice_kind']
    v['meeting_date_short'] = short_date(v['meeting_date'])
    v['date_posted_short'] = short_date(v['date_posted'])
    v['mailed_clause'] = ''
    if v['mailed'] == 'Yes':
        if not v.get('date_mailed'):
            raise ValueError('Enter the date it was mailed, delivered or emailed.')
        v['mailed_clause'] = f', and caused it to be {v["mail_method"]} to each owner on {short_date(v["date_mailed"])}'
    d = v['date_posted']
    v['notary_day'] = d.day
    v['notary_month_year'] = f'{d:%B}, {d.year}'
    v['subject'] = f'{v["notice_kind"]} — {v["meeting_date_short"]}'


# ----------------------------------------------------------------------------------------------- sign-in sheet
SIGN_IN_BODY = """<div class="lf-page">""" + LETTERHEAD + """<div>
<p><strong>SIGN-IN SHEET</strong><br>{{ meeting_title }}<br>{{ meeting_date_long }}</p>
<table class="lf-grid">
<tr><th>Unit</th><th>Owner(s)</th><th>Signature</th><th>Present in person, by proxy, or by video</th></tr>
{% for row in roster_rows %}<tr><td>{{ row.0 }}</td><td>{{ row.1 }}</td><td></td><td></td></tr>{% endfor %}
</table>
<p>Total present: ____________ &nbsp;&nbsp; Quorum {{ quorum_line }}</p>
</div></div>"""

SIGN_IN_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('meeting_title', 'Meeting', default='Annual Meeting of the Members', required=True),
    F('meeting_date', 'Date of the meeting', 'date', parent='meeting_date', prefill='meeting_date', required=True),
    F('roster', 'Units and owners, one per line as: Unit | Owner(s)', 'textarea', prefill='owner_roster', required=True, help='Filled from the association records. Add or fix lines as needed.'),
    F('quorum_text', 'Quorum needed (optional, e.g. 30% of the units)'),
]


def compute_sign_in(v):
    rows = []
    for line in _lines(v['roster']):
        unit, _, owners = line.partition('|')
        rows.append((unit.strip(), owners.strip()))
    v['roster_rows'] = rows
    v['quorum_line'] = f'needed: {v["quorum_text"]}' if v.get('quorum_text') else 'present: Yes / No'
    v['subject'] = f'{v["meeting_title"]} — {short_date(v["meeting_date"])}'


# ----------------------------------------------------------------------------------- approval or denial letter
DECISION_LETTER_BODY = """<div class="lf-page">""" + LETTERHEAD + """<div>
<p>{{ letter_date_long }}</p>
<p>{{ applicant_names }}{% if applicant_address %}<br>{{ applicant_address|linebreaksbr }}{% endif %}</p>
<p><strong>Re: {{ re_line }}</strong></p>
<p>Dear {{ greeting_names }}:</p>
{% if approved %}
<p>We are pleased to tell you that the Board of Directors of {{ association_name }} reviewed your application {{ for_phrase }} at {{ unit_address }} and, on {{ decision_date_long }}, approved it.</p>
{% if is_tenant and lease_start and lease_end %}<p>This approval is for a lease term of {{ lease_start_long }} through {{ lease_end_long }}.</p>{% endif %}
{% if conditions %}<p>The approval is given on these conditions: {{ conditions|linebreaksbr }}</p>{% endif %}
<p>This approval applies only to the persons named in your application. Any change in occupants, in the use of the unit, or in the term must be submitted to the Board for approval before it takes effect.</p>
{% else %}
<p>The Board of Directors of {{ association_name }} reviewed your application {{ for_phrase }} at {{ unit_address }}. After consideration, on {{ decision_date_long }}, the Board was unable to approve the application.</p>
<p>{{ reason_sentence }}</p>
<p>Because the application was not approved, you may not occupy the unit under this application. If you have questions about the decision, please contact us at {{ contact_phone }} or {{ contact_email }}.</p>
{% endif %}
{% if approved %}<p>If you have any questions, please contact us at {{ contact_phone }} or {{ contact_email }}.</p>{% endif %}
<p>Sincerely,<br>The Board of Directors</p>
</div></div>"""

DENIAL_REASONS = [
    'The application was incomplete.',
    'The required application fee or supporting documents were not received.',
    'The unit has unpaid assessments or charges owed to the Association.',
    "The information received did not meet the Association's written approval standards.",
    "The proposed lease term or number of occupants is not permitted by the Association's governing documents.",
    'Other (written below)',
]

DECISION_LETTER_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('applicant_kind', 'Application for', 'choice', choices=['Purchase of a unit (new owner)', 'Lease of a unit (tenant)'], default='Purchase of a unit (new owner)', required=True),
    F('decision', 'The Board\'s decision', 'choice', choices=['Approved', 'Not approved'], required=True),
    F('applicant_names', 'Applicant name(s)', prefill='run_person_name', required=True),
    F('applicant_address', 'Applicant mailing address (optional)', 'textarea'),
    F('unit_label', 'Unit', prefill='unit_label'),
    F('unit_address', 'Unit address', prefill='unit_address', required=True),
    F('decision_date', 'Date of the Board\'s decision', 'date', required=True),
    F('letter_date', 'Date of this letter', 'date', default='today', required=True),
    F('lease_start', 'If approved, a tenant: lease begins', 'date'),
    F('lease_end', 'If approved, a tenant: lease ends', 'date'),
    F('conditions', 'If approved: conditions (optional)', 'textarea'),
    F('denial_reason', 'If not approved: the reason the Board recorded', 'choice', choices=DENIAL_REASONS),
    F('denial_reason_detail', 'If not approved: more detail (required for "Other"; optional otherwise)', 'textarea'),
    F('contact_phone', 'Phone to call', prefill='company_phone_display'),
    F('contact_email', 'Email to write', prefill='company_email'),
]


def compute_decision_letter(v):
    v['approved'] = v['decision'] == 'Approved'
    v['is_tenant'] = v['applicant_kind'].startswith('Lease')
    v['for_phrase'] = 'to lease a unit' if v['is_tenant'] else 'to purchase a unit'
    v['re_line'] = ('Approval of ' if v['approved'] else 'Application for ') + ('lease' if v['is_tenant'] else 'purchase') + f' — {v["unit_address"]}'
    v['greeting_names'] = v['applicant_names']
    if not v['approved']:
        reason = v.get('denial_reason') or ''
        detail = (v.get('denial_reason_detail') or '').strip()
        if not reason:
            raise ValueError('Choose the reason the Board recorded for not approving the application.')
        if reason.startswith('Other') and not detail:
            raise ValueError('Write the reason the Board recorded.')
        v['reason_sentence'] = detail if reason.startswith('Other') else (reason + (' ' + detail if detail else ''))
    v['subject'] = f'{v["applicant_names"]} — {v["decision"]}' + (f' — Unit {v["unit_label"]}' if v.get('unit_label') else '')


# ----------------------------------------------------------------------------------------------- welcome letters
WELCOME_OWNER_BODY = """<div class="lf-page">""" + LETTERHEAD + """<div>
<p>{{ letter_date_long }}</p>
<p>{{ recipient_names }}{% if recipient_address %}<br>{{ recipient_address|linebreaksbr }}{% endif %}</p>
<p><strong>Welcome to {{ association_name }}</strong></p>
<p>Dear {{ recipient_names }}:</p>
<p>Welcome to {{ association_name }}. {{ company_name }} is the managing agent for the Association, and we are glad to have you as a member of the community.</p>
<p>If you need anything, you can reach us at {{ contact_phone }} or {{ contact_email }}, or write to us at {{ company_address1 }}, {{ company_city }}.</p>
<p>Welcome again.</p>
<p>Sincerely,<br>{{ company_name }}<br>Managing agent for {{ association_name }}</p>
</div></div>"""

WELCOME_OWNER_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('recipient_names', 'Owner name(s)', prefill='person_name', required=True),
    F('recipient_address', 'Mailing address (optional)', 'textarea', prefill='owner_address'),
    F('letter_date', 'Date of the letter', 'date', default='today', required=True),
    F('contact_phone', 'Phone to call', prefill='company_phone_display'),
    F('contact_email', 'Email to write', prefill='company_email'),
]

WELCOME_TENANT_BODY = """<div class="lf-page">""" + LETTERHEAD + """<div>
<p>{{ letter_date_long }}</p>
<p>{{ recipient_names }}{% if recipient_address %}<br>{{ recipient_address|linebreaksbr }}{% endif %}</p>
<p><strong>Welcome to {{ association_name }}</strong></p>
<p>Dear {{ recipient_names }}:</p>
<p>Welcome to {{ association_name }}. {{ company_name }} is the managing agent for the Association, and we are glad to have you in the community.</p>
<p>If you need anything, you can reach us at {{ contact_phone }} or {{ contact_email }}, or write to us at {{ company_address1 }}, {{ company_city }}.</p>
<p>Welcome again.</p>
<p>Sincerely,<br>{{ company_name }}<br>Managing agent for {{ association_name }}</p>
</div></div>"""

WELCOME_TENANT_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('recipient_names', 'Tenant name(s)', prefill='run_person_name', required=True),
    F('recipient_address', 'Address (optional)', 'textarea', prefill='owner_address'),
    F('letter_date', 'Date of the letter', 'date', default='today', required=True),
    F('contact_phone', 'Phone to call', prefill='company_phone_display'),
    F('contact_email', 'Email to write', prefill='company_email'),
]


def compute_welcome(v):
    v['subject'] = v['recipient_names']


COMPUTE_MEETINGS = {
    'board-meeting-notice': compute_board_notice,
    'members-meeting-notice': compute_members_notice,
    'first-election-notice': compute_first_election,
    'annual-meeting-notice': compute_annual_notice,
    'notice-affidavit': compute_notice_affidavit,
    'sign-in-sheet': compute_sign_in,
    'decision-letter': compute_decision_letter,
    'welcome-letter-owner': compute_welcome,
    'welcome-letter-tenant': compute_welcome,
}

TEMPLATES_MEETINGS = [
    {'slug': 'board-meeting-notice', 'name': 'Notice of Board Meeting', 'category': 'Meetings', 'order': 40, 'statute': '§718.112(2)(c) / §720.303(2), Florida Statutes',
     'description': 'The notice posted (and, for a special assessment, the budget or a rule, also mailed) before a meeting of the Board of Directors.',
     'body': BOARD_NOTICE_BODY, 'fields': BOARD_NOTICE_FIELDS, 'reviewed_note': ''},
    {'slug': 'notice-affidavit', 'name': 'Affidavit of Notice', 'category': 'Meetings', 'order': 41, 'companion_of': 'board-meeting-notice', 'statute': '§718.112(2), Florida Statutes',
     'description': 'Sworn statement, signed before a notary, of when and how the notice of a meeting was posted and delivered. Starts from the notice it goes with.',
     'body': NOTICE_AFFIDAVIT_BODY, 'fields': NOTICE_AFFIDAVIT_FIELDS, 'reviewed_note': ''},
    {'slug': 'members-meeting-notice', 'name': 'Notice of Members Meeting and Agenda', 'category': 'Meetings', 'order': 42, 'statute': '§718.112(2)(d) / §720.306, Florida Statutes',
     'description': 'Notice and agenda of a regular or special meeting of the members.',
     'body': MEMBERS_NOTICE_BODY, 'fields': MEMBERS_NOTICE_FIELDS, 'reviewed_note': ''},
    {'slug': 'first-election-notice', 'name': 'First Notice of Election and Call for Candidates', 'category': 'Meetings', 'order': 43, 'statute': '§718.112(2)(d), Florida Statutes',
     'description': 'The first notice of the election of directors, with the call for candidates.',
     'body': FIRST_ELECTION_BODY, 'fields': FIRST_ELECTION_FIELDS, 'reviewed_note': ''},
    {'slug': 'annual-meeting-notice', 'name': 'Notice of Annual Meeting and Agenda', 'category': 'Meetings', 'order': 44, 'statute': '§718.112(2)(d) / §720.306, Florida Statutes',
     'description': 'Notice and agenda of the annual meeting and election of directors. Ballots and proxies are prepared outside the system.',
     'body': ANNUAL_NOTICE_BODY, 'fields': ANNUAL_NOTICE_FIELDS, 'reviewed_note': ''},
    {'slug': 'sign-in-sheet', 'name': 'Sign-in Sheet', 'category': 'Meetings', 'order': 45, 'statute': '',
     'description': 'The attendance sheet for a meeting of the members, listing every unit and owner from the association records.',
     'body': SIGN_IN_BODY, 'fields': SIGN_IN_FIELDS, 'reviewed_note': ''},
    {'slug': 'decision-letter', 'name': 'Approval or Denial Letter', 'category': 'Sales and transfers', 'order': 31, 'statute': '',
     'description': 'Tells a new owner or a tenant that the Board approved or did not approve the application. Goes with the Certificate of Board Approval when approved.',
     'body': DECISION_LETTER_BODY, 'fields': DECISION_LETTER_FIELDS, 'reviewed_note': ''},
    {'slug': 'welcome-letter-owner', 'name': 'Welcome Letter (owner)', 'category': 'Sales and transfers', 'order': 32, 'statute': '',
     'description': 'A short hello, how to reach us, and welcome, for a new owner.',
     'body': WELCOME_OWNER_BODY, 'fields': WELCOME_OWNER_FIELDS, 'reviewed_note': ''},
    {'slug': 'welcome-letter-tenant', 'name': 'Welcome Letter (tenant)', 'category': 'Sales and transfers', 'order': 33, 'statute': '',
     'description': 'A short hello, how to reach us, and welcome, for a new tenant.',
     'body': WELCOME_TENANT_BODY, 'fields': WELCOME_TENANT_FIELDS, 'reviewed_note': ''},
]
