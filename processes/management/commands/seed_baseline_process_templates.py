"""Idempotent: seeds 3 of the 15 baseline process templates the user
specified, chosen to collectively exercise all 17 step types end to end
before the remaining 12 are added in a follow-up pass (see this feature's
plan doc). get_or_create by name — safe to run on every deploy, and never
touches a template that already exists (so in-progress edits from the
builder UI are never clobbered by a redeploy)."""
from django.core.management.base import BaseCommand

from core.models import StaffProfile
from processes.models import ProcessTemplate, ProcessTemplateStep, StepType

TEMPLATES = [
    {
        'name': 'Short-Term Rental Cleaning',
        'category': 'Short-Term Rental',
        'description': 'Cleaner turnover checklist between reservations, with a secure link for '
                        'contracted (non-staff) cleaners and manager review before turnover.',
        'steps': [
            ('Select property', StepType.RECORD_SELECTOR, {'config': {'target': 'property'}}),
            (
                'Assign the cleaner and deadline', StepType.TASK_ASSIGNMENT,
                {'config': {'default_role': StaffProfile.Role.CLEANER, 'default_days_until_due': 1}},
            ),
            ('Send a secure mobile process link', StepType.CHECKBOX, {}),
            (
                'Complete the room-by-room cleaning checklist', StepType.CHECKLIST,
                {'assignee_role': 'external', 'config': {'items': ['Living room', 'Kitchen', 'Bathroom(s)', 'Bedroom(s)']}},
            ),
            (
                'Confirm linens, toiletries, kitchen items, and supplies', StepType.CHECKLIST,
                {'assignee_role': 'external', 'config': {'items': ['Linens', 'Toiletries', 'Kitchen items', 'Supplies restocked']}},
            ),
            ('Upload required completion photos', StepType.PHOTO_VIDEO_UPLOAD, {'assignee_role': 'external'}),
            (
                'Identify damage or excessive cleaning', StepType.DROPDOWN_MULTISELECT,
                {
                    'assignee_role': 'external', 'is_required': False,
                    'config': {'multi': True, 'options': ['Furniture damage', 'Wall damage', 'Excessive cleaning needed', 'Missing items']},
                },
            ),
            ('Document any damage or issues', StepType.LONG_TEXT, {'assignee_role': 'external', 'is_required': False}),
            ('Enter additional charges (if applicable)', StepType.NUMBER_CURRENCY, {'assignee_role': 'external', 'is_required': False, 'config': {'is_currency': True}}),
            ('Create maintenance or replacement tasks', StepType.TASK_ASSIGNMENT, {'is_required': False, 'config': {'default_role': StaffProfile.Role.MAINTENANCE}}),
            (
                'Confirm locks, doors, windows, lights, and thermostat', StepType.CHECKLIST,
                {'assignee_role': 'external', 'config': {'items': ['Locks', 'Doors', 'Windows', 'Lights', 'Thermostat']}},
            ),
            ('Confirm the property is guest-ready', StepType.CHECKBOX, {'assignee_role': 'external'}),
            (
                'Manager review — approve turnover', StepType.APPROVAL_DECISION,
                {
                    'assignee_role': StaffProfile.Role.PROPERTY_MANAGER,
                    'config': {'routes': [{'label': 'Approved — ready', 'action': 'continue', 'target_step_key': ''},
                                           {'label': 'Needs rework', 'action': 'continue', 'target_step_key': ''}]},
                },
            ),
        ],
    },
    {
        'name': 'Association Delinquency and Collection',
        'category': 'Association',
        'description': 'Tracks a delinquent owner account from balance confirmation through notice, cure '
                        'period, and resolution or escalation. Verify interest/fee rates and cure-period '
                        'length against the association\'s governing documents and Florida Statutes '
                        'Ch. 718/720 before relying on the defaults seeded here.',
        'steps': [
            ('Select the association', StepType.RECORD_SELECTOR, {'config': {'target': 'property'}}),
            ('Enter unit number', StepType.SHORT_TEXT, {}),
            ('Select the owner', StepType.RECORD_SELECTOR, {'config': {'target': 'contact'}}),
            ('Confirm the balance due', StepType.NUMBER_CURRENCY, {'config': {'is_currency': True}}),
            ('Enter late fee amount', StepType.NUMBER_CURRENCY, {'config': {'is_currency': True}}),
            (
                'Calculate total amount due', StepType.CALCULATION_FORMULA,
                {'config': {'formula': '{confirm-the-balance-due} + {enter-late-fee-amount}', 'output_label': 'Total due'}},
            ),
            ('Verify eligibility for collection action', StepType.CHECKBOX, {}),
            ('Prepare and upload the required notice', StepType.DOCUMENT_UPLOAD, {'assignee_role': StaffProfile.Role.PROPERTY_MANAGER}),
            (
                'Wait through the cure period', StepType.WAIT_TIMER,
                {'help_text': 'Default 45 days — confirm against the association\'s bylaws.', 'config': {'wait_mode': 'duration', 'duration_days': 45}},
            ),
            (
                'Route: paid, payment plan, continue collection, or escalate', StepType.APPROVAL_DECISION,
                {
                    'config': {'routes': [
                        {'label': 'Paid', 'action': 'continue', 'target_step_key': ''},
                        {'label': 'Payment plan', 'action': 'continue', 'target_step_key': ''},
                        {'label': 'Continue collection', 'action': 'continue', 'target_step_key': ''},
                        {'label': 'Escalate to attorney', 'action': 'continue', 'target_step_key': ''},
                    ]},
                },
            ),
            ('Refer to attorney/collection provider (if required)', StepType.EMAIL_TEXT_ACTION, {'is_required': False}),
            ('Upload correspondence and legal records', StepType.DOCUMENT_UPLOAD, {'is_required': False}),
            ('Update outstanding balance', StepType.NUMBER_CURRENCY, {'config': {'is_currency': True}}),
            ('Confirm resolution and close', StepType.CHECKBOX, {}),
        ],
    },
    {
        'name': 'Long-Term Tenant Move-In',
        'category': 'Long-Term Rental',
        'description': 'Onboards a new long-term tenant: lease paperwork, deposits, inspection, and '
                        'tenant acknowledgment via a secure link.',
        'steps': [
            ('Select the tenant', StepType.RECORD_SELECTOR, {'config': {'target': 'contact'}}),
            ('Select the property', StepType.RECORD_SELECTOR, {'config': {'target': 'property'}}),
            ('Upload the executed lease and addenda', StepType.DOCUMENT_UPLOAD, {}),
            ('Confirm deposit amount received', StepType.NUMBER_CURRENCY, {'config': {'is_currency': True}}),
            (
                'Confirm insurance, utilities, ID, and required documents', StepType.CHECKLIST,
                {'config': {'items': ["Renter's insurance", 'Utilities transferred', 'Photo ID on file', 'Required documents signed']}},
            ),
            ('Assign key/access/welcome-package preparation', StepType.TASK_ASSIGNMENT, {'config': {'default_role': StaffProfile.Role.PROPERTY_MANAGER}}),
            ('Enter the move-in date', StepType.DATE_TIME, {}),
            ('Schedule the move-in inspection', StepType.CALENDAR_EVENT, {'config': {'add_meet': False}}),
            ('Upload condition photos', StepType.PHOTO_VIDEO_UPLOAD, {}),
            ('Note any deficiencies', StepType.LONG_TEXT, {'is_required': False}),
            ('Obtain tenant acknowledgment and signature', StepType.DIGITAL_SIGNATURE, {'assignee_role': 'external'}),
            ('Send the move-in package and contact instructions', StepType.EMAIL_TEXT_ACTION, {}),
            ('Activate the tenant and lease, complete move-in', StepType.CHECKBOX, {}),
        ],
    },
]


class Command(BaseCommand):
    help = 'Seeds the 3 baseline process templates chosen to prove out all 17 step types.'

    def handle(self, *args, **options):
        for spec in TEMPLATES:
            template, created = ProcessTemplate.objects.get_or_create(
                name=spec['name'], defaults={'category': spec['category'], 'description': spec['description']},
            )
            if not created:
                self.stdout.write(f'"{spec["name"]}" already exists — leaving steps untouched.')
                continue
            for sequence_order, (label, step_type, extra) in enumerate(spec['steps'], start=1):
                ProcessTemplateStep.objects.create(
                    process_template=template, label=label, step_type=step_type, sequence_order=sequence_order, **extra,
                )
            self.stdout.write(self.style.SUCCESS(f'Created "{spec["name"]}" with {len(spec["steps"])} step(s).'))
