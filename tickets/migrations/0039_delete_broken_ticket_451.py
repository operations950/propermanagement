"""One-off production fix: permanently deletes ticket #451, per direct
user request — "There is a ticket in production, four fifty one that
needs to get deleted. We can't close it manually because it has a
process attached to it, but that process has an error."

Root cause traced in the actual code, not just inferred: ticket_detail's
Processes card renders each attached ProcessRunStep via
processes/_run_card.html's `{% include "processes/steps/"|add:step.
step_type|add:".html" %}` — a template path BUILT FROM THE STEP'S OWN
step_type VALUE. If that value doesn't match one of the 17 real files
under processes/templates/processes/steps/ (a stale value from before a
step-type rename, a bad manual data entry, or any other way a
CharField's `choices` can end up holding a value outside that list —
choices are validated by ModelForm.full_clean(), never by a bare
.save()), Django's {% include %} raises TemplateDoesNotExist rendering
that one step — which crashes the ENTIRE ticket_detail page for this
ticket, not just that step. That's the actual mechanism behind "we
can't close it manually" — the page needed to click Close (or Delete)
never loads in the first place. See tickets/services/process_gate.py
for the SEPARATE, unrelated gate that would otherwise block a normal
status-based Close (irrelevant here since the page itself can't even
be reached, and since this migration deletes the ticket outright rather
than going through that flow).

Deleting the Ticket row (Ticket.process_runs is a CASCADE FK) takes its
ProcessRun(s), their ProcessRunStep(s)/attachments, and every other
ticket-scoped row (closing notes, status notes, contacts, checklist
items, follow-up logs, attachments) with it in one transaction — the
same cleanup a normal admin-only ticket_delete already does, just
reached here directly at the DB layer since the UI path to click that
button is exactly what's broken.

Prints full diagnostics before deleting — which template(s) were
attached and each step's step_type/is_required/is_complete — as the
audit trail for what this actually removed and (for anyone curious
later) confirmation of which step_type value was the actual culprit.
Safe to re-run: a no-op if the ticket is already gone by the time this
deploys (e.g. someone found another way to remove it first)."""
from django.db import migrations


def delete_ticket(apps, schema_editor):
    Ticket = apps.get_model('tickets', 'Ticket')
    ProcessRun = apps.get_model('processes', 'ProcessRun')

    ticket = Ticket.objects.filter(pk=451).first()
    if ticket is None:
        print('Ticket #451 not found — already gone, nothing to do.')
        return

    print(f'Ticket #451: "{ticket.title}" (status={ticket.status})')
    runs = ProcessRun.objects.filter(ticket=ticket).prefetch_related('steps')
    if not runs:
        print('  No attached ProcessRun found (deleting anyway, per the direct request).')
    for run in runs:
        print(f'  ProcessRun: {run.process_template.name} (status={run.status})')
        for step in run.steps.all():
            print(f'    step_type={step.step_type!r} required={step.is_required} complete={step.is_complete}')

    ticket.delete()
    print('Ticket #451 deleted (cascaded to its process run(s) and every other ticket-scoped row).')


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0038_ticketattachment_visible_to_vendor'),
        ('processes', '0006_alter_processattachment_file_and_more'),
    ]

    operations = [
        migrations.RunPython(delete_ticket, migrations.RunPython.noop),
    ]
