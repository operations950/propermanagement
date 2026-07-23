"""Dependency-gating between task package steps generated for the same
PackageRun (one package, one property, one period) — see
TaskPackageTemplate.depends_on and Ticket.package_run."""
from ..models import TaskPackageTemplate, Ticket


def unblock_dependents(ticket):
    """Call whenever `ticket`'s status becomes dependency-satisfying (see
    Ticket.DEPENDENCY_SATISFYING_STATUSES) — finds sibling tickets in the
    same package_run whose step depends on this one and releases them from
    Blocked. Each TaskPackageTemplate has at most one depends_on, so a
    dependent step needs no further check once its single prerequisite is
    satisfied."""
    if not ticket.package_run_id or not ticket.created_from_template_id:
        return
    if ticket.status not in Ticket.DEPENDENCY_SATISFYING_STATUSES:
        return

    this_step = TaskPackageTemplate.objects.filter(
        package=ticket.package_run.package_id, template=ticket.created_from_template_id,
    ).first()
    if not this_step:
        return

    dependent_template_ids = TaskPackageTemplate.objects.filter(
        depends_on=this_step,
    ).values_list('template_id', flat=True)
    if not dependent_template_ids:
        return

    blocked_siblings = Ticket.objects.filter(
        package_run_id=ticket.package_run_id,
        created_from_template_id__in=list(dependent_template_ids),
        status=Ticket.Status.BLOCKED,
    )
    for sibling in blocked_siblings:
        sibling.status = Ticket.Status.ASSIGNED if sibling.assigned_staff_id else Ticket.Status.OPEN
        sibling.save(update_fields=['status', 'updated_at'])
