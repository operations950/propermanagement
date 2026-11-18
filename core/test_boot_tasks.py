import contextlib
import io
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import get_commands, load_command_class
from django.test import TestCase

from core.management.commands import boot_tasks

# The chain the Procfile ran before boot_tasks existed, in its order.
EXPECTED = [
    'migrate', 'bootstrap_admin', 'grant_company_admin', 'grant_portfolio_access', 'load_initial_properties', 'seed_property_attributes', 'seed_baseline_process_templates',
    'fix_delinquency_wait_period', 'reshape_compliance_rules', 'seed_checklist_templates', 'seed_legal_templates', 'load_template_library', 'seed_daily_upload_slots',
    'seed_standard_supply_kit', 'seed_portfolio_businesses', 'backfill_staff_contacts', 'backfill_vendor_property_links', 'backfill_ticket_assignment',
    'backfill_dedupe_thread_tickets', 'recheck_negative_deposit_category', 'remove_auto_created_tickets', 'collectstatic',
]


class BootTasksTests(TestCase):
    def test_the_same_commands_run_in_the_same_order(self):
        self.assertEqual([name for name, _ in boot_tasks.STEPS], EXPECTED)

    def test_each_command_exists_and_takes_the_options_it_is_given(self):
        commands = get_commands()
        for name, opts in boot_tasks.STEPS:
            self.assertIn(name, commands)
            parser = load_command_class(commands[name], name).create_parser('manage.py', name)
            known = {a.dest for a in parser._actions}
            self.assertTrue(set(opts) <= known, f'{name} does not take {set(opts) - known}')

    def test_the_old_options_are_kept(self):
        opts = dict(boot_tasks.STEPS)
        self.assertEqual(opts['migrate'], {'interactive': False})                    # migrate --noinput
        self.assertEqual(opts['collectstatic'], {'interactive': False})              # collectstatic --noinput
        self.assertEqual(opts['recheck_negative_deposit_category'], {'apply': True})  # --apply

    def test_a_failing_command_stops_the_chain_and_is_not_swallowed(self):
        calls = []

        def fake(name, **opts):
            calls.append(name)
            if name == 'seed_property_attributes':
                raise RuntimeError('boom')

        with mock.patch.object(boot_tasks, 'call_command', side_effect=fake):
            with self.assertRaises(RuntimeError):
                boot_tasks.Command().handle()
        self.assertEqual(calls, ['check', 'migrate', 'bootstrap_admin', 'grant_company_admin', 'grant_portfolio_access', 'load_initial_properties', 'seed_property_attributes'])

    def test_a_command_that_exits_with_a_code_stops_the_chain_too(self):
        def fake(name, **opts):
            if name == 'bootstrap_admin':
                raise SystemExit(3)

        with mock.patch.object(boot_tasks, 'call_command', side_effect=fake):
            with self.assertRaises(SystemExit) as caught:
                boot_tasks.Command().handle()
        self.assertEqual(caught.exception.code, 3)

    def test_the_checks_run_first_including_the_database_ones(self):
        with mock.patch.object(boot_tasks, 'call_command') as call:
            boot_tasks.Command().handle()
        self.assertEqual(call.call_args_list[0], mock.call('check', databases=['default']))
        self.assertEqual(len(call.call_args_list), len(EXPECTED) + 1)

    def test_the_procfile_starts_the_server_only_after_it(self):
        web = next(l for l in (Path(settings.BASE_DIR) / 'Procfile').read_text().splitlines() if l.startswith('web:'))
        self.assertEqual(web, 'web: python manage.py boot_tasks && gunicorn proptasks.wsgi --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 90 --log-file -')

    def test_the_whole_chain_runs_clean_in_one_process(self):
        steps = tuple(s for s in boot_tasks.STEPS if s[0] not in ('collectstatic', 'migrate'))            # collectstatic writes the static folder and migrate cannot run inside a test's transaction; the rest is the data work
        with mock.patch.object(boot_tasks, 'STEPS', steps), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            boot_tasks.Command().handle()
