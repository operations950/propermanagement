"""Everything that runs on a deploy before the web server starts, in one process.

The Procfile used to chain twenty-two separate `python manage.py ...` commands with `&&`; every one of them started Python and Django again (about a second and a half each), which
was most of the half-minute a deploy took before the site answered. This runs the same commands, in the same order, with the same arguments, in one process.

What stays exactly as it was: the order; a command that fails (any exception, or an exit with a non-zero code) stops the whole chain at that command and this exits non-zero, so the
server is not started, just as `&&` did; each command's own output; and the system checks, which `manage.py` runs before every command, are run once here before the first one
(`migrate` was the first command, and it ran them, the database ones included, before doing anything).
"""
import os

from django.core.management import call_command
from django.core.management.base import BaseCommand

# (command, options) in the order the Procfile ran them. `collectstatic` is last because it was last.
STEPS = (
    ('migrate', {'interactive': False}),
    ('bootstrap_admin', {}),
    ('grant_company_admin', {}),
    ('grant_portfolio_access', {}),
    ('load_initial_properties', {}),
    ('seed_property_attributes', {}),
    ('seed_baseline_process_templates', {}),
    ('fix_delinquency_wait_period', {}),
    ('reshape_compliance_rules', {}),
    ('seed_checklist_templates', {}),
    ('seed_legal_templates', {}),
    ('load_template_library', {}),
    ('seed_daily_upload_slots', {}),
    ('seed_standard_supply_kit', {}),
    ('seed_portfolio_businesses', {}),
    ('backfill_staff_contacts', {}),
    ('backfill_vendor_property_links', {}),
    ('backfill_ticket_assignment', {}),
    ('backfill_dedupe_thread_tickets', {}),
    ('recheck_negative_deposit_category', {'apply': True}),
    ('remove_auto_created_tickets', {}),
    ('collectstatic', {'interactive': False}),
)


class Command(BaseCommand):
    help = 'Runs the deploy-time commands one after another in this process; stops at the first one that fails (see the Procfile).'

    # manage.py checks the project before every command; here that is done once, by hand, below.
    requires_system_checks = []

    def handle(self, *args, **options):
        call_command('check', databases=['default'])
        for name, opts in STEPS:
            call_command(name, **opts)
        self.media_cleanup()

    def media_cleanup(self):
        """A one-time switch for freeing the files volume when there is no shell on the server: set the variable MEDIA_CLEANUP to `report` (list what is on the volume, change nothing), `apply`
        (delete what Cloudinary already holds, copying up what it does not) or `apply_orphans` (also delete files no database row names), redeploy, read the log, then remove the variable.
        Never stops the site from starting, whatever it finds."""
        mode = os.environ.get('MEDIA_CLEANUP', '').strip().lower()
        if mode not in ('report', 'apply', 'apply_orphans'):
            return
        try:
            self.stdout.write(f'MEDIA_CLEANUP={mode}: cleaning the files volume')
            call_command('cleanup_local_media', apply=mode != 'report', orphans=mode == 'apply_orphans')
        except BaseException as exc:                       # a failed clean-up must never keep the site down
            self.stderr.write(f'MEDIA_CLEANUP failed: {exc!r}')
