from django.db import migrations


def dedupe(apps, schema_editor):
    """Defensive backfill before the next migration tightens BizTask with a
    real UniqueConstraint on (recurring_rule, due_date) — see
    portfolio/services/generation.py's docstring for why. Local dev data
    has no duplicates as of this writing, but this environment has no
    production DB access to confirm the same is true there, and this
    codebase has already been bitten once by adding a constraint on top of
    unverified production data (see tickets/migrations' matching comment
    on ticket_exactly_one_assignee). For any (recurring_rule, due_date)
    group with more than one row: keep the earliest-created row as-is, and
    null out recurring_rule on the rest — converting them into one-off
    tasks rather than deleting anything a human may have already acted on
    (notes, status, amount all survive). due_date stays untouched since
    it's part of the constraint driving this, not the ambiguous part.

    Split into its own migration/transaction, not combined with the
    AddConstraint that follows — this codebase has already hit a real
    Postgres "pending trigger events" crash from a DML+DDL combo in one
    transaction (see supplies/migrations/0008 and 0009's split)."""
    BizTask = apps.get_model('portfolio', 'BizTask')
    from django.db.models import Count

    dupe_keys = (
        BizTask.objects.filter(recurring_rule__isnull=False)
        .values('recurring_rule_id', 'due_date')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
    )
    for key in dupe_keys:
        rows = list(
            BizTask.objects.filter(recurring_rule_id=key['recurring_rule_id'], due_date=key['due_date'])
            .order_by('created_at', 'pk')
        )
        for extra in rows[1:]:
            extra.recurring_rule = None
            extra.save(update_fields=['recurring_rule'])


class Migration(migrations.Migration):

    dependencies = [
        ('portfolio', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(dedupe, migrations.RunPython.noop),
    ]
