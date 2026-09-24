"""The standardized forms, as data. seed_legal_templates copies them into DocumentTemplate rows (which an admin can
then edit); the per-form calculations live here in code because they are logic, not wording.

A field is {'key', 'label', 'type', 'required', 'help', 'default', 'prefill', 'remember', 'parent', 'choices'}:
  type      text | textarea | date | money | percent | integer | yesno | choice
  prefill   a token the program fills from its own records (services.PREFILL_TOKENS)
  default   a literal used when nothing else fills it (a date field's 'today' means today's date)
  remember  start from what was typed the last time this form was made for the same unit / association
  parent    for a companion form: the key in the parent document's values (or 'mailed_on') to start from
Every date gets `<key>_long` ("Tuesday, July 21, 2026") and `<key>_short` ("7/21/2026"); every money `<key>_fmt`
("$2,752.14") and `<key>_fmt0` ("$299" when whole); every percent `<key>_pct` ("1.5%").

The wording of the first three is the management company's own (from its real letters and certificate), with the
spelling corrected; the board approval certification is new. All of it should be reviewed by the association's attorney
before it is relied on — see each template's reviewed_note."""
from datetime import timedelta
from decimal import Decimal


def F(key, label, type='text', **kw):
    return {'key': key, 'label': label, 'type': type, **kw}


# --------------------------------------------------------------------------------------- notice of late assessment
NOLA_BODY = """<div class="lf-page">
<p>{{ owner_name }}<br>{{ owner_address|linebreaksbr }}</p>
<p>{{ letter_date_long }}</p>
<p><strong>NOTICE OF LATE ASSESSMENT (Pursuant to §718.121(5), Florida Statutes)</strong></p>
<p>Our records indicate that your assessment account with {{ association_name }} is past due. Pursuant to §718.121(5), Florida Statutes, this Notice of Late Assessment is provided to inform you of the amount currently due and to give you an opportunity to bring your account current.</p>
<p>Payment must be made in full within {{ response_days }} days of the date of this letter (no later than {{ deadline_short }}). If payment is not received by that date, the Association intends to proceed with further collection action against your property, which may include attorney involvement, recording of a lien, and foreclosure proceedings, as permitted by Florida law.</p>
<table class="lf-amounts">
<tr><td colspan="3" class="lf-center"><strong>Amounts due as of {{ letter_date_long }}</strong></td></tr>
<tr><td class="lf-r">Regular Assessments Due</td><td class="lf-r">{{ regular_assessments_fmt }}</td><td></td></tr>
<tr><td class="lf-r">Late Fees Due</td><td class="lf-r">{{ late_fees_fmt }}</td><td>Accumulated late fees at {{ late_fee_rate_pct }}</td></tr>
<tr><td class="lf-r">Interest Due</td><td class="lf-r">{{ interest_fmt }}</td><td>Accumulated interest at {{ interest_rate_pct }}</td></tr>
<tr><td class="lf-r"><strong>Total Amount Due</strong></td><td class="lf-r"><strong>{{ total_fmt }}</strong></td><td></td></tr>
</table>
<p><em>Interest will continue to accrue on all unpaid assessments at the rate of {{ interest_rate_pct }} per annum, and additional late fees may be imposed as authorized by the condominium documents and Florida law.</em></p>
<p>If you believe this balance is incorrect or have already made payment, please contact us at {{ contact_phone }} or {{ contact_email }}.</p>
<p>Sincerely,<br>The Board of Directors</p>
</div>"""

NOLA_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('owner_name', 'Owner(s)', prefill='owner_names', remember=True, required=True),
    F('owner_address', 'Mailing address (as furnished to the association)', 'textarea', prefill='owner_address', remember=True, required=True, help='One line per row, as it should print under the name.'),
    F('letter_date', 'Date of the letter', 'date', default='today', required=True),
    F('response_days', 'Days to pay', 'integer', default='30', required=True, help='The letter gives this many days from its date. Confirm the period the statute and the governing documents require before sending.'),
    F('regular_assessments', 'Regular assessments due', 'money', required=True),
    F('late_fees', 'Late fees due', 'money', default='0', required=True),
    F('late_fee_rate', 'Late fee rate (%)', 'percent', default='1.5', remember=True, required=True),
    F('interest', 'Interest due', 'money', default='0', required=True),
    F('interest_rate', 'Interest rate (% per year)', 'percent', default='5', remember=True, required=True),
    F('contact_phone', 'Phone to call', prefill='company_phone_display'),
    F('contact_email', 'Email to write', prefill='company_email'),
]


def compute_nola(v):
    v['total'] = v['regular_assessments'] + v['late_fees'] + v['interest']
    v['total_fmt'] = money(v['total'])
    v['deadline'] = v['letter_date'] + timedelta(days=v['response_days'])
    v['deadline_short'] = short_date(v['deadline'])
    v['subject'] = f'{v["owner_name"]}' + (f' — Unit {v["unit_label"]}' if v.get('unit_label') else '')


# ----------------------------------------------------------------------------------- affidavit of mailing (goes with it)
AFFIDAVIT_BODY = """<div class="lf-page">
<p class="lf-center">AFFIDAVIT OF MAILING<br>NOTICE OF LATE ASSESSMENT</p>
<table class="lf-plain"><tr><td>STATE OF FLORIDA</td><td>)</td></tr><tr><td>COUNTY OF {{ county|upper }}</td><td>)</td></tr></table>
<p>I, {{ affiant_name }}, the {{ affiant_title }} of {{ association_name }} (the "Association"), hereby states and affirms as follows. In accordance with Section 718.121(5), Florida Statutes, I caused to be placed in the United States first class mail, postage paid, on {{ mailing_date_short }}, the Notice of Late Assessment to the parcel owner(s), {{ owner_name }}, at the address last furnished to {{ association_name }}, as such address appears in the books of the Association.</p>
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

AFFIDAVIT_FIELDS = [
    F('association_name', 'Association', parent='association_name', prefill='association_name', required=True),
    F('county', 'County', prefill='county', required=True),
    F('owner_name', 'Parcel owner(s) the notice was mailed to', parent='owner_name', prefill='owner_names', required=True),
    F('mailing_date', 'Date it was mailed', 'date', parent='mailed_on', required=True, help='Swear only to the day it really went into the mail. It starts from the mailing date recorded on the notice.'),
    F('affiant_name', 'Person swearing (who mailed it)', prefill='manager_name', required=True),
    F('affiant_title', 'Their title', prefill='manager_title', default='Property Manager', required=True),
    F('notary_name', 'Notary (optional — prints under the signature line)', remember=True),
    F('notary_commission_expires', 'Notary commission expires (optional)', remember=True),
]


def compute_affidavit(v):
    d = v['mailing_date']
    v['notary_day'] = d.day
    v['notary_month_year'] = f'{d:%B}, {d.year}'
    v['subject'] = v['owner_name']


# ------------------------------------------------------------------------------------------------- estoppel certificate
ESTOPPEL_BODY = """<div class="lf-page">
<table class="lf-head"><tr>
<td><div>{{ association_name }}</div><div class="lf-small">c/o {{ company_name }}</div><div>{{ company_address1 }}</div><div>{{ company_city }}</div></td>
<td class="lf-logo"><img src="{{ logo_token }}" alt="{{ company_name }}"></td>
</tr></table>
<p><strong><u>Estoppel Certificate – {{ issue_date_short }}</u></strong></p>
<p>To Whom It May Concern,</p>
<p>This Estoppel Certificate is issued in accordance with {{ statute_ref }}, in response to the written/electronic request dated {{ request_date_short }} received from {{ requestor_name }} on behalf of {{ on_behalf_of }}.</p>
<p class="lf-h">GENERAL INFORMATION</p>
<table class="lf-kv">
<tr><td>Date of Issuance:</td><td>{{ issue_date_short }}</td></tr>
<tr><td>Name(s) of Parcel Owner(s):</td><td>{{ owner_name }}</td></tr>
<tr><td>Parcel Designation and Address:</td><td>{{ parcel_address }}{% if parcel_id %} | {{ parcel_id }}{% endif %}</td></tr>
<tr><td>Parking or Garage Space Number:</td><td>{{ parking_space }}</td></tr>
<tr><td>Fee for the Preparation and Delivery:</td><td>{{ fee_fmt0 }}</td></tr>
<tr><td>Name of the Requestor:</td><td>{{ requestor_name }}</td></tr>
</table>
<p class="lf-h">ASSESSMENT INFORMATION:</p>
<table class="lf-kv">
<tr><td>Regular Periodic Assessment:</td><td>{{ regular_assessment_fmt }} per {{ frequency_unit }}</td></tr>
<tr><td>Paid Through Date:</td><td>{{ paid_through_short }}</td></tr>
<tr><td>Next Installment Due:</td><td>{{ next_installment_short }}</td></tr>
<tr><td>Itemized List of Current Assessments:</td><td>{{ itemized|linebreaksbr }}</td></tr>
</table>
<p class="lf-h">OTHER INFORMATION:</p>
<table class="lf-kv">
<tr><td>Capital Contribution, Resale, Transfer, or Other Fee Due:</td><td>{{ capital_fee_due }}{% if capital_fee_due == 'Yes' and capital_fee_detail %} — {{ capital_fee_detail }}{% endif %}</td></tr>
<tr><td>Is there any open violation of rule or regulation noticed to the parcel</td><td>{{ open_violation }}</td></tr>
<tr><td>Do the rules and regulations of the association applicable to the parcel</td><td>{{ rules_apply }}</td></tr>
<tr><td>Is there a right of first refusal provided to the members or the</td><td>{{ right_of_first_refusal }}</td></tr>
<tr><td>Insurance Contact:</td><td>{{ insurance_contact }}</td></tr>
</table>
<p>This Estoppel Certificate is valid until {{ valid_until_short }}. No fee is charged for attorney information if the account is delinquent and has been turned over to an attorney for collection.</p>
<p>For further inquiries, please contact the designated person/entity at the provided contact information.</p>
<table class="lf-sign"><tr>
<td>Sincerely,<br>{{ signer_name }}<br>{{ signer_title }}</td>
<td>{{ company_name }}<br>{{ company_address1 }}<br>{{ company_city }}<br>{{ company_phone }}<br>{{ company_email }}</td>
</tr></table>
</div>"""

ESTOPPEL_FIELDS = [
    F('association_name', 'Association (full legal name)', prefill='association_name', required=True),
    F('statute_ref', 'Issued under', default='Florida Statutes Chapter 718.116', required=True, help='Chapter 718.116 for a condominium; a homeowners\' association is under a different chapter.'),
    F('issue_date', 'Date of issuance', 'date', default='today', required=True),
    F('request_date', 'Date of the written/electronic request', 'date', required=True),
    F('requestor_name', 'Name of the requestor', required=True),
    F('on_behalf_of', 'On behalf of', remember=False, required=True),
    F('owner_name', 'Parcel owner(s)', prefill='owner_names', remember=True, required=True),
    F('parcel_address', 'Parcel address', prefill='unit_address', required=True),
    F('parcel_id', 'Parcel / folio number', remember=True),
    F('parking_space', 'Parking or garage space number', remember=True),
    F('fee', 'Fee for preparation and delivery', 'money', default='299', remember=True, required=True),
    F('regular_assessment', 'Regular periodic assessment', 'money', remember=True, required=True),
    F('assessment_frequency', 'How often', 'choice', choices=['Monthly', 'Quarterly', 'Semi-annually', 'Annually'], default='Monthly', remember=True, required=True),
    F('paid_through', 'Paid through', 'date', required=True),
    F('next_installment', 'Next installment due', 'date', required=True),
    F('itemized', 'Itemized list of current assessments', 'textarea', default='N/A'),
    F('capital_fee_due', 'Capital contribution, resale, transfer or other fee due?', 'yesno', default='No', required=True),
    F('capital_fee_detail', 'If yes: what and how much'),
    F('open_violation', 'Any open rule/regulation violation noticed to the parcel?', 'yesno', default='No', required=True),
    F('rules_apply', 'Do the association\'s rules and regulations apply to the parcel?', 'yesno', default='Yes', required=True),
    F('right_of_first_refusal', 'Right of first refusal?', 'yesno', default='No', required=True),
    F('insurance_contact', 'Insurance contact', remember=True),
    F('valid_days', 'Valid for (days)', 'integer', default='30', required=True),
    F('signer_name', 'Signed by', prefill='manager_name', required=True),
    F('signer_title', 'Title', prefill='manager_title', default='Property Manager', required=True),
]

FREQUENCY_UNIT = {'Monthly': 'month', 'Quarterly': 'quarter', 'Semi-annually': 'half-year', 'Annually': 'year'}


def compute_estoppel(v):
    v['valid_until'] = v['issue_date'] + timedelta(days=v['valid_days'])
    v['valid_until_short'] = short_date(v['valid_until'])
    v['frequency_unit'] = FREQUENCY_UNIT.get(v['assessment_frequency'], v['assessment_frequency'].lower())
    v['subject'] = f'{v["owner_name"]}' + (f' — Unit {v["unit_label"]}' if v.get('unit_label') else '')


# ------------------------------------------------------------------------------------------- board approval certification
APPROVAL_BODY = """<div class="lf-page">
<p class="lf-center"><strong>CERTIFICATE OF BOARD APPROVAL</strong><br>{{ association_name }}</p>
<p>I, {{ certifier_name }}, {{ certifier_title }} of {{ association_name }} (the "Association"), hereby certify that the Board of Directors of the Association, {{ action_phrase }} {{ approval_date_long }}, reviewed the application received {{ application_date_long }} and <strong>approved</strong> the following {{ applicant_word }} for Unit {{ unit_label }}:</p>
<p class="lf-center"><strong>{{ applicant_names }}</strong><br>{{ unit_address }}</p>
{% if is_tenant %}<p>Approved lease term: {{ lease_start_long }} through {{ lease_end_long }}.</p>{% endif %}
{% if conditions %}<p>Conditions of approval: {{ conditions|linebreaksbr }}</p>{% endif %}
<p>This approval was given in accordance with the Association's governing documents and applicable Florida law, and is effective as of {{ approval_date_long }}.</p>
<p>Dated: {{ issue_date_long }}</p>
<div class="lf-sig"><div class="lf-line"></div><div>{{ certifier_name }}</div><div>{{ certifier_title }}</div></div>
</div>"""

APPROVAL_FIELDS = [
    F('association_name', 'Association', prefill='association_name', required=True),
    F('applicant_kind', 'Approved as', 'choice', choices=['Purchaser', 'Tenant'], default='Purchaser', required=True),
    F('applicant_names', 'Name(s) of the purchaser / tenant', required=True),
    F('unit_label', 'Unit', prefill='unit_label', required=True),
    F('unit_address', 'Unit address', prefill='unit_address', required=True),
    F('application_date', 'Date the application was received', 'date', required=True),
    F('action', 'The Board acted', 'choice', choices=['at a meeting held on', 'by written action taken on'], default='at a meeting held on', required=True),
    F('approval_date', 'Date of the Board\'s approval', 'date', required=True),
    F('lease_start', 'Lease begins (tenants)', 'date'),
    F('lease_end', 'Lease ends (tenants)', 'date'),
    F('conditions', 'Conditions of approval (if any)', 'textarea'),
    F('issue_date', 'Date of this certificate', 'date', default='today', required=True),
    F('certifier_name', 'Certified by', prefill='manager_name', required=True),
    F('certifier_title', 'Title', prefill='manager_title', default='Property Manager, on behalf of the Board of Directors', required=True),
]


def compute_approval(v):
    v['is_tenant'] = v['applicant_kind'] == 'Tenant'
    v['applicant_word'] = 'tenant(s)' if v['is_tenant'] else 'purchaser(s)'
    v['action_phrase'] = v['action']
    if v['is_tenant'] and not (v.get('lease_start') and v.get('lease_end')):
        raise ValueError('A tenant approval needs the lease start and end dates.')
    v['subject'] = f'{v["applicant_names"]} — Unit {v["unit_label"]}'


# ------------------------------------------------------------------------------------------------------- helpers + list
def money(d):
    return f'${d:,.2f}'


def short_date(d):
    return f'{d.month}/{d.day}/{d.year}'


COMPUTE = {
    'nola': compute_nola, 'nola-affidavit': compute_affidavit, 'estoppel': compute_estoppel, 'board-approval': compute_approval,
}

TEMPLATES = [
    {
        'slug': 'nola', 'name': 'Notice of Late Assessment', 'category': 'Collections', 'order': 10,
        'statute': '§718.121(5), Florida Statutes',
        'description': 'The letter that gives an owner an opportunity to bring a past-due assessment account current before the association takes further collection action.',
        'body': NOLA_BODY, 'fields': NOLA_FIELDS,
        'reviewed_note': 'Wording is the management company\'s own letter. Confirm with the association\'s attorney: the statute cited, the response period (30 days here — the seeded delinquency process defaults to 45), and that the late fee and interest rates match the governing documents.',
    },
    {
        'slug': 'nola-affidavit', 'name': 'Affidavit of Mailing — Notice of Late Assessment', 'category': 'Collections', 'order': 11, 'companion_of': 'nola',
        'statute': '§718.121(5), Florida Statutes',
        'description': 'Sworn statement, signed before a notary, that the Notice of Late Assessment was mailed to the owner on a stated date.',
        'body': AFFIDAVIT_BODY, 'fields': AFFIDAVIT_FIELDS,
        'reviewed_note': 'Wording is the management company\'s own affidavit, spelling corrected. It is signed under oath: the mailing date must be the day the notice really went into the mail.',
    },
    {
        'slug': 'estoppel', 'name': 'Estoppel Certificate', 'category': 'Sales and transfers', 'order': 20,
        'statute': 'Chapter 718.116, Florida Statutes',
        'description': 'The certificate an association issues, on a title company\'s or owner\'s request, stating what is owed on a unit and other required facts.',
        'body': ESTOPPEL_BODY, 'fields': ESTOPPEL_FIELDS,
        'reviewed_note': 'Wording is the management company\'s own certificate. Three labels in the sample under "Other Information" were cut off at the end ("…noticed to the parcel", "…applicable to the parcel", "…members or the") and are reproduced exactly as they were; complete them from the source form. Confirm the fee, delivery deadline and validity period against the statute in force.',
    },
    {
        'slug': 'board-approval', 'name': 'Certificate of Board Approval', 'category': 'Sales and transfers', 'order': 30,
        'statute': '',
        'description': 'Shows that the association\'s Board approved a new purchaser or tenant — something to hand the buyer, tenant, landlord or title company.',
        'body': APPROVAL_BODY, 'fields': APPROVAL_FIELDS,
        'reviewed_note': 'New wording written for this system (no sample was provided). Have the association\'s attorney or Board confirm it says what their governing documents require, and replace it with the association\'s own form if it has one.',
    },
]
