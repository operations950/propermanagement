"""The owner portal for commercial buildings: the month's financial package, releasing it, and what an owner may see of the tickets.

A commercial building's month is not the rental statement in core/owner_portal.py. It is the same monthly financial package an association's board gets (accounting/package.py:
cover, profit and loss, balance sheet, budget vs actual, owner aging, then the bank statements) as ONE PDF, frozen when the month is released, with income, expenses and net income
beside it. "Closed" for a building means the books' month is locked (accounting.MonthLock). Releasing, emailing the owners and re-issuing a month that was reopened and closed again
work the same way as for a rental: this module reuses the sign-in, the owner contacts, the release table (MonthRelease) and the emails from core/owner_portal.py.

Tickets: an owner sees a ticket only when staff released it (Ticket.owner_visible, private by default) and reads only the updates marked "show to owner" (TicketStatusNote.show_to_owner).
"""
import logging
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from . import ledger, owner_portal
from .models import Contact, MonthRelease, Property

logger = logging.getLogger(__name__)
ZERO, CENT = Decimal('0.00'), Decimal('0.01')
LABELS = (('income', 'Income'), ('expenses', 'Expenses'), ('net', 'Net income'))


# ---------------------------------------------------------------------------------------------------------------- closed months

def closed_months(prop):
    from accounting.models import MonthLock
    return sorted(MonthLock.objects.filter(association=prop).values_list('month', flat=True))


def is_closed(prop, month):
    from accounting.models import MonthLock
    return MonthLock.objects.filter(association=prop, month=ledger.month_of(month)).exists()


def snapshot(prop, month):
    """What the owner is shown for the month beside the PDF: income, expenses and net income, in plain strings (so they can be stored and compared later)."""
    from accounting import ledger as books
    month = ledger.month_of(month)
    if not is_closed(prop, month):
        raise owner_portal.NotClosed(f'{prop.name} {month:%B %Y} is not closed.')
    inc = books.income_statement(prop, month, books.month_end(month), 'accrual')
    return {'kind': 'package', 'month': month.isoformat(), 'basis': 'accrual',
            'figures': {'income': str(inc['total_income'].quantize(CENT)), 'expenses': str(inc['total_expense'].quantize(CENT)), 'net': str(inc['net'].quantize(CENT))}}


def package_pdf(prop, month):
    """(file name, PDF bytes) of the month's whole financial package."""
    from accounting import package
    month = ledger.month_of(month)
    return package.file_name(prop, month), package.build(prop, month, 'accrual')


def package_pdf_name(prop, month):
    from accounting import package
    return package.file_name(prop, ledger.month_of(month)).replace('"', '')


def is_package(release):
    return (release.snapshot or {}).get('kind') == 'package'


def display(release):
    """A stored commercial month for a template: the figures as Decimals."""
    return {'month': release.month, 'figures': owner_portal.figures_of(release.snapshot)}


# ---------------------------------------------------------------------------------------------------------------------- release

def release_plan(through):
    """Which months would be released for each commercial building the portal is open for by 'release through <month>', and what stops it. From a building's first locked month to
    the one named, every month must be locked: a gap blocks the release, because the owners would see a hole."""
    through = ledger.month_of(through)
    plan, blockers = [], []
    for prop in owner_portal.open_commercial_properties():
        locked = closed_months(prop)
        if not locked:
            blockers.append(f'{prop.name} has no closed months yet.')
            continue
        months = owner_portal._month_range(locked[0], through) if through >= locked[0] else []
        gaps = [m for m in months if m not in set(locked)]
        if gaps:
            shown = ', '.join(f'{m:%b %Y}' for m in gaps[:4]) + ('…' if len(gaps) > 4 else '')
            blockers.append(f'{prop.name}: {shown} not closed yet.')
            continue
        done = set(MonthRelease.objects.filter(property=prop).values_list('month', flat=True))
        plan.append((prop, [m for m in months if m not in done]))
    return plan, blockers


def status():
    """For the Owner portal admin page: one row per commercial building the portal is open for: its owners, the months released, and the closed months not released yet."""
    rows = []
    for prop in owner_portal.open_commercial_properties():
        locked = closed_months(prop)
        released = set(MonthRelease.objects.filter(property=prop).values_list('month', flat=True))
        rows.append({'property': prop, 'closed': locked, 'released': sorted(released), 'waiting': [m for m in locked if m not in released], 'owners': owner_portal.owner_contacts(prop),
                     'no_email': list(Contact.objects.filter(contact_type=Contact.ContactType.OWNER, properties=prop, email='').order_by('name'))})
    return rows


def release(through, user):
    """Release every closed, unreleased month up to and including `through` for each commercial building the portal is open for. A building with a gap blocks the whole release, as
    for a rental. The PDF package is made and kept now, so the owner always sees what was released. Each owner is emailed once. Returns a summary like release_through."""
    plan, blockers = release_plan(through)
    if blockers:
        raise owner_portal.ReleaseBlocked(blockers)
    now = timezone.now()
    newly = []
    for prop, months in plan:                # one month at a time: a package that cannot be made stops there, and nothing half-made is left behind
        for m in months:
            snap = snapshot(prop, m)
            name, data = package_pdf(prop, m)
            with transaction.atomic():
                row = MonthRelease(property=prop, month=m, released_at=now, released_by=user, snapshot=snap)
                row.package.save(name, ContentFile(data), save=False)
                row.save()
        if months:
            newly.append((prop, months))
    emailed, failed = owner_portal.notify_release(newly)
    return {'properties': len(newly), 'months': sum(len(ms) for _, ms in newly), 'emailed': emailed, 'failed': failed,
            'no_email': sorted({c.name for p, _ in newly for c in Contact.objects.filter(contact_type=Contact.ContactType.OWNER, properties=p, email='')})}


def diff(old, new):
    a, b = owner_portal.figures_of(old), owner_portal.figures_of(new)
    changes = []
    for key, label in LABELS:
        was, now = a.get(key, ZERO), b.get(key, ZERO)
        if abs(was - now) >= CENT:
            changes.append({'label': label, 'was': str(was), 'now': str(now)})
    return changes


def after_lock(prop, month):
    """Called when a commercial building's month is locked again. If that month was already released and its income, expenses or net income now differ, the stored package is replaced
    with a new one, the change is written to the release's history, and the owners are emailed. A month locked for the first time is not released by this."""
    month = ledger.month_of(month)
    row = MonthRelease.objects.filter(property=prop, month=month).first()
    if row is None or not is_package(row):
        return
    new = snapshot(prop, month)
    changes = diff(row.snapshot, new)
    if not changes:
        return
    name, data = package_pdf(prop, month)
    now = timezone.now()
    row.snapshot, row.revised_at = new, now
    row.revisions = list(row.revisions) + [{'at': now.isoformat(), 'changes': changes, 'lines_changed': False}]
    row.package.save(name, ContentFile(data), save=False)
    row.save()
    if Property.objects.filter(pk=prop.pk, owner_portal_open=True).exists():
        transaction.on_commit(lambda: owner_portal.notify_revision(prop, row.month, changes, False))


def connect():
    """When the books lock a month (accounting.MonthLock is saved), tell the portal: a month that was released and now differs is re-issued and the owners are told."""
    from django.db.models.signals import post_save
    from accounting.models import MonthLock

    def on_lock(sender, instance, created, **kwargs):
        if not created:
            return
        prop = instance.association
        if prop.property_type != Property.Type.COMMERCIAL or not prop.owner_portal_open:
            return

        def run():
            try:
                after_lock(prop, instance.month)
            except Exception:                    # closing a month must never fail because of the portal
                logger.exception('Owner portal could not refresh %s %s', prop.name, instance.month)
        transaction.on_commit(run)

    post_save.connect(on_lock, sender=MonthLock, dispatch_uid='owner_commercial_lock', weak=False)


# ------------------------------------------------------------------------------------------------------------------------- tickets

OWNER_STATUS = {'completed': 'Done', 'verified': 'Done', 'cancelled': 'Closed', 'skipped': 'Closed', 'not_applicable': 'Closed', 'deferred': 'Postponed', 'upcoming': 'Scheduled'}


def visible_tickets(prop):
    """The tickets of this property the owner may see: only those staff have released (Ticket.owner_visible), newest first."""
    from tickets.models import Ticket
    return Ticket.objects.filter(property=prop, owner_visible=True).order_by('-created_at')


def visible_notes(ticket):
    """The updates on a released ticket the owner may read: only those marked 'show to owner'."""
    return list(ticket.status_notes.filter(show_to_owner=True).order_by('created_at'))


def owner_status(ticket):
    """A ticket's status in the owner's plain words: Done, Closed, Postponed, Scheduled, or In progress for everything still being worked."""
    return OWNER_STATUS.get(ticket.status, 'In progress')


def finished(ticket):
    return owner_status(ticket) in ('Done', 'Closed')
