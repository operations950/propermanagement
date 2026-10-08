"""The owner portal's rules (no screens here): who may sign up, how logins and emailed codes work, what an owner may see, releasing a month to
owners, and what happens when a released month is corrected.

Access follows data, every time: an owner sees a rental when they have an owner Contact (same email address) linked to it AND the rental's
`owner_portal_open` is on. A month is visible only once it has been released (a MonthRelease holding exactly what they see). Staff pages are walled off
from owner logins by core.middleware.OwnerWallMiddleware."""
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from . import ledger
from .models import Contact, MonthClose, MonthRelease, OwnerAccount, OwnerCode, Property

logger = logging.getLogger(__name__)

CODE_MINUTES = 15            # how long an emailed code works
MAX_CODE_TRIES = 5           # wrong codes allowed before it is dead
LOCK_AFTER = 5               # wrong passwords in a row before a short lockout
LOCK_MINUTES = 15
SESSION_SECONDS = 8 * 60 * 60
USERNAME_PREFIX = 'ownerportal-'
ZERO = Decimal('0.00')
CENT = Decimal('0.01')


class NotClosed(Exception):
    """A month cannot be shown to owners until every set of books for it is closed."""


class ReleaseBlocked(Exception):
    def __init__(self, reasons):
        super().__init__('; '.join(reasons))
        self.reasons = reasons


def norm_email(email):
    return (email or '').strip().lower()


def is_owner_user(user):
    """A portal login (by its username alone, with no query: this runs on every request)."""
    return bool(getattr(user, 'is_authenticated', False)) and user.username.startswith(USERNAME_PREFIX)


def portal_url():
    return f'{settings.SITE_BASE_URL}/owner/'


# ------------------------------------------------------------------------------------------------------------- what an owner may see

def open_properties():
    return Property.objects.filter(owner_portal_open=True, is_active=True, property_type=Property.Type.SHORT_TERM_RENTAL, is_general=False).order_by('name')


def owner_contacts(prop):
    """The owner contacts of a rental that have an email address (every one of them gets access and the emails)."""
    return list(Contact.objects.filter(contact_type=Contact.ContactType.OWNER, properties=prop).exclude(email='').order_by('name'))


def visible_properties(email):
    """The rentals this email address is an owner of, with the portal open."""
    email = norm_email(email)
    if not email:
        return []
    return list(open_properties().filter(contacts__contact_type=Contact.ContactType.OWNER, contacts__email__iexact=email).distinct())


def is_released(prop, month):
    """Has this rental's month been released to owners (and is the portal open for it)?"""
    return bool(prop.owner_portal_open) and MonthRelease.objects.filter(property=prop, month=ledger.month_of(month)).exists()


def released_months(prop):
    return list(MonthRelease.objects.filter(property=prop).order_by('month'))


# ---------------------------------------------------------------------------------------------------------------------------- sending

def send_email(to, subject, body):
    """True if the mail went out. Uses the same sender as follow-ups, which refuses (rather than pretends) when no real email delivery is connected."""
    from messaging.services import _send_email
    try:
        _send_email(subject, body, settings.DEFAULT_FROM_EMAIL, [to])
        return True
    except Exception:
        logger.exception('Owner portal email to %s failed', to)
        return False


# --------------------------------------------------------------------------------------------------------- sign-up, codes, login

def _code_hash(email, code):
    return hmac.new(settings.SECRET_KEY.encode(), f'{norm_email(email)}:{code}'.encode(), hashlib.sha256).hexdigest()


def start_code(email, password, purpose):
    """Begin a sign-up or password reset: if this email is an owner of an open rental (and, for a sign-up, has no login yet; for a reset, has one), keep
    the scrambled password and email a 6-digit code. Returns None if nothing was sent for a reason that must not be revealed to the person at the screen
    (not an owner, no login yet...), True if the code went out, False if it could not be emailed."""
    email = norm_email(email)
    has_login = OwnerAccount.objects.filter(email=email).exists()
    if not visible_properties(email) or (purpose == OwnerCode.Purpose.SIGNUP and has_login) or (purpose == OwnerCode.Purpose.RESET and not has_login):
        return None
    code = f'{secrets.randbelow(10 ** 6):06d}'
    OwnerCode.objects.filter(email=email, purpose=purpose, used_at__isnull=True).delete()
    row = OwnerCode.objects.create(email=email, purpose=purpose, code_hash=_code_hash(email, code), password_hash=make_password(password),
                                   expires_at=timezone.now() + timezone.timedelta(minutes=CODE_MINUTES))
    sent = send_email(email, f'Your Proper Realty owner portal code: {code}',
                      f'Your code is {code}\n\nType it on the owner portal page to finish {"creating your login" if purpose == OwnerCode.Purpose.SIGNUP else "changing your password"}. '
                      f'It works for {CODE_MINUTES} minutes.\n\nIf you did not ask for this, ignore this email: nothing happens without the code.\n')
    if not sent:
        row.delete()
    return sent


def verify_code(email, purpose, code):
    """Check a typed code. Returns (status, OwnerAccount|None); status is 'ok', 'bad' (wrong, expired, used up or never asked for: one answer for all, so
    nothing is revealed) or 'locked' is folded into 'bad'. On 'ok' the login is created (sign-up) or its password replaced (reset)."""
    email = norm_email(email)
    row = OwnerCode.objects.filter(email=email, purpose=purpose, used_at__isnull=True).order_by('-created_at').first()
    if row is None or row.expires_at < timezone.now() or row.attempts >= MAX_CODE_TRIES:
        return 'bad', None
    row.attempts += 1
    row.save(update_fields=['attempts'])
    if not hmac.compare_digest(row.code_hash, _code_hash(email, (code or '').strip())):
        return 'bad', None
    if not visible_properties(email):
        return 'bad', None
    with transaction.atomic():
        row.used_at = timezone.now()
        row.save(update_fields=['used_at'])
        User = get_user_model()
        account = OwnerAccount.objects.select_related('user').filter(email=email).first()
        if purpose == OwnerCode.Purpose.SIGNUP:
            if account is not None:
                return 'bad', None
            user = User(username=f'{USERNAME_PREFIX}{uuid.uuid4().hex[:20]}', is_staff=False, is_superuser=False)
            user.password = row.password_hash
            user.save()
            account = OwnerAccount.objects.create(user=user, email=email)
        else:
            if account is None:
                return 'bad', None
            account.user.password = row.password_hash
            account.user.save(update_fields=['password'])
            account.failed_attempts, account.locked_until = 0, None
            account.save(update_fields=['failed_attempts', 'locked_until'])
    return 'ok', account


def authenticate_owner(email, password):
    """(user, None) on success, else (None, reason) with reason 'invalid' | 'locked' | 'disabled'. Five wrong passwords in a row lock the login for
    15 minutes, however many addresses the guesses come from."""
    email = norm_email(email)
    account = OwnerAccount.objects.select_related('user').filter(email=email).first()
    if account is None:
        make_password(password or '')          # take as long as a real check, so a missing login is not told apart by speed
        return None, 'invalid'
    now = timezone.now()
    if account.locked_until and account.locked_until > now:
        return None, 'locked'
    if not account.is_active:
        return None, 'disabled'
    if not check_password(password or '', account.user.password):
        account.failed_attempts += 1
        if account.failed_attempts >= LOCK_AFTER:
            account.failed_attempts, account.locked_until = 0, now + timezone.timedelta(minutes=LOCK_MINUTES)
        account.save(update_fields=['failed_attempts', 'locked_until'])
        return None, 'invalid'
    account.failed_attempts, account.locked_until, account.last_login_at = 0, None, now
    account.save(update_fields=['failed_attempts', 'locked_until', 'last_login_at'])
    return account.user, None


# ----------------------------------------------------------------------------------------------------- a month's statement

STATEMENT_KEYS = ('deposits', 'commission_due', 'net_income', 'owner_baseline_expense', 'expenses_reimbursable', 'expenses_direct', 'owner_due', 'owner_payment',
                  'taken_to_us', 'owner_payable', 'us_payable', 'owner_owes_us', 'commission_base')
LABELS = (
    ('deposits', 'Income deposits'), ('commission_due', 'Commission'), ('expenses_reimbursable', 'Reimbursable expenses'), ('expenses_direct', 'Expenses paid from trust'),
    ('owner_due', 'Owner payment for the month'), ('owner_payment', 'Paid to you this month'), ('taken_to_us', 'Paid to Proper Realty this month'),
    ('owner_payable', 'Owed to you at month end'), ('us_payable', 'Owed to Proper Realty at month end'), ('owner_owes_us', 'Owed to Proper Realty'),
)


def is_month_closed(prop, month):
    books = ledger.books_for(prop, month)
    return bool(books) and all(ledger.close_of(b, month) is not None for b in books)


def statement_data(prop, month, require_closed=True):
    """A rental's month as the owner sees it, in plain strings (so it can be stored): the figures and the transactions behind income, reimbursable
    expenses and expenses paid from trust. A rental kept unit by unit is its units added up. With require_closed (the default, and always for a release)
    every set of books must be closed, so the figures are the frozen ones."""
    month = ledger.month_of(month)
    parts, detail, closed = [], {'deposits': [], 'reimbursable': [], 'direct': []}, True
    for book in ledger.books_for(prop, month):
        close = ledger.close_of(book, month)
        if close is not None:
            parts.append(ledger.closed_summary(close))
        else:
            closed = False
            parts.append(ledger.totals(book, month))
        for group, rows in ledger._detail(book, month).items():
            detail[group] += rows
    if not parts or (require_closed and not closed):
        raise NotClosed(f'{prop.name} {month:%B %Y} is not closed.')
    t = parts[0] if len(parts) == 1 else ledger.sum_totals(parts)
    basis = parts[0].get('commission_basis') or prop.commission_basis
    return {
        'month': month.isoformat(), 'closed': closed, 'owner_collects': bool(t.get('owner_collects')), 'basis': basis, 'rate': str(t.get('commission_rate', ZERO)),
        'figures': {k: str(Decimal(t[k]).quantize(CENT)) for k in STATEMENT_KEYS if t.get(k) is not None},
        'detail': {g: sorted(({'date': r['date'].isoformat(), 'text': r['text'], 'amount': str(Decimal(r['amount']).quantize(CENT)), 'unit': r.get('unit', '')} for r in rows),
                             key=lambda r: (r['date'], r['text'])) for g, rows in detail.items()},
    }


def display(snapshot):
    """A stored month ready for a template: real dates and Decimals."""
    s = dict(snapshot)
    s['figures'] = figures_of(snapshot)
    s['month_date'] = date.fromisoformat(snapshot['month'])
    s['rate'] = Decimal(snapshot['rate'])
    s['detail'] = {g: [{'date': date.fromisoformat(r['date']), 'text': r['text'], 'amount': Decimal(r['amount']), 'unit': r.get('unit', '')} for r in rows]
                   for g, rows in snapshot['detail'].items()}
    return s


def figures_of(snapshot):
    return {k: Decimal(v) for k, v in snapshot['figures'].items()}


def diff_snapshots(old, new):
    """What changed between two versions of a month, for the owner: ([{label, was, now}], whether the individual transactions changed)."""
    a, b = figures_of(old), figures_of(new)
    changes = []
    for key, label in LABELS:
        if key in a or key in b:
            was, now = a.get(key, ZERO), b.get(key, ZERO)
            if abs(was - now) >= CENT:
                changes.append({'label': label, 'was': str(was), 'now': str(now)})
    def lines(s):
        return sorted((g, r['date'], r['amount']) for g, rows in s['detail'].items() for r in rows)
    return changes, lines(old) != lines(new)


# ---------------------------------------------------------------------------------------------------------------------- release

def _month_range(first, last):
    out, m = [], first
    while m <= last:
        out.append(m)
        m = ledger.next_month(m)
    return out


def release_plan(through):
    """Which months would be released for each open rental by 'release through <month>', and what stops it. Every month from a rental's first closed month
    to the one named must be closed (all of its books); a gap blocks the release, because the owners would see a hole."""
    through = ledger.month_of(through)
    plan, blockers = [], []
    for prop in open_properties():
        first = MonthClose.objects.filter(property=prop).order_by('month').values_list('month', flat=True).first()
        if first is None:
            blockers.append(f'{prop.name} has no closed months yet.')
            continue
        months = _month_range(first, through) if through >= first else []
        gaps = [m for m in months if not is_month_closed(prop, m)]
        if gaps:
            shown = ', '.join(f'{m:%b %Y}' for m in gaps[:4]) + ('…' if len(gaps) > 4 else '')
            blockers.append(f'{prop.name}: {shown} not closed yet.')
            continue
        done = set(MonthRelease.objects.filter(property=prop).values_list('month', flat=True))
        plan.append((prop, [m for m in months if m not in done]))
    return plan, blockers


def release_status(month):
    """For the close screen: how the month stands with the owners."""
    month = ledger.month_of(month)
    props = list(open_properties())
    if not props:
        return {'open': 0}
    released = set(MonthRelease.objects.filter(month=month, property__in=props).values_list('property_id', flat=True))
    plan, blockers = release_plan(month)
    pending = [(p, ms) for p, ms in plan if ms]
    first = MonthRelease.objects.filter(month=month, property__in=props).order_by('released_at').first()
    return {
        'open': len(props), 'properties': props, 'released': len(released), 'all_released': len(released) == len(props), 'blockers': blockers, 'pending': pending,
        'pending_months': sorted({m for _, ms in pending for m in ms}), 'released_at': first.released_at if first else None,
        'released_by': first.released_by if first else None,
        'recipients': sorted({c.email.lower() for p, _ in pending for c in owner_contacts(p)}),
        'no_email': sorted({c.name for p, _ in pending for c in Contact.objects.filter(contact_type=Contact.ContactType.OWNER, properties=p, email='')}),
    }


def release_through(through, user):
    """Release every closed, unreleased month up to and including `through` for every open rental, then email each owner once. Returns a summary."""
    plan, blockers = release_plan(through)
    if blockers:
        raise ReleaseBlocked(blockers)
    now = timezone.now()
    newly = []
    with transaction.atomic():
        for prop, months in plan:
            for m in months:
                MonthRelease.objects.create(property=prop, month=m, released_at=now, released_by=user, snapshot=statement_data(prop, m))
            if months:
                newly.append((prop, months))
    emailed, failed = notify_release(newly)
    return {'properties': len(newly), 'months': sum(len(ms) for _, ms in newly), 'emailed': emailed, 'failed': failed,
            'no_email': sorted({c.name for p, _ in newly for c in Contact.objects.filter(contact_type=Contact.ContactType.OWNER, properties=p, email='')})}


def _span(months):
    months = sorted(months)
    if len(months) == 1:
        return f'{months[0]:%B %Y}'
    return f'{months[0]:%B} – {months[-1]:%B %Y}' if months[0].year == months[-1].year else f'{months[0]:%B %Y} – {months[-1]:%B %Y}'


def notify_release(newly):
    """One email to each owner address, covering everything newly released for the rentals it owns. Returns (sent, [addresses that failed])."""
    by_email = {}
    for prop, months in newly:
        for c in owner_contacts(prop):
            by_email.setdefault(c.email.lower(), []).append((prop, months))
    sent, failed = 0, []
    for email, items in sorted(by_email.items()):
        every = sorted({m for _, ms in items for m in ms})
        lines = '\n'.join(f'  - {prop.name}: {_span(ms)}' for prop, ms in items)
        body = (f'Hello,\n\nYour financials are ready on the Proper Realty owner portal:\n\n{lines}\n\n'
                f'Go to {portal_url()}\n\nThe first time, choose "Create your login" and use this email address ({email}); '
                f'we will email you a 6-digit code to confirm it is you. After that, sign in with your email and password.\n')
        if send_email(email, f'Your {_span(every)} financials are ready', body):
            sent += 1
        else:
            failed.append(email)
    return sent, failed


def after_close(prop, month):
    """Called when a set of books is closed. If this completes a month that had already been released to owners and any figure (or transaction) differs
    from what they were shown, the stored version is replaced, the change is recorded on it, and the owners are emailed."""
    release = MonthRelease.objects.filter(property=prop, month=ledger.month_of(month)).first()
    if release is None or not is_month_closed(prop, month):
        return
    new = statement_data(prop, month)
    changes, lines_changed = diff_snapshots(release.snapshot, new)
    if not changes and not lines_changed:
        return
    now = timezone.now()
    release.snapshot = new
    release.revised_at = now
    release.revisions = list(release.revisions) + [{'at': now.isoformat(), 'changes': changes, 'lines_changed': lines_changed}]
    release.save(update_fields=['snapshot', 'revised_at', 'revisions'])
    if Property.objects.filter(pk=prop.pk, owner_portal_open=True).exists():          # asked of the database, not the copy in hand: it may be stale
        transaction.on_commit(lambda: notify_revision(prop, release.month, changes, lines_changed))


def notify_revision(prop, month, changes, lines_changed):
    lines = [f'  - {c["label"]}: ${Decimal(c["was"]):,.2f} -> ${Decimal(c["now"]):,.2f}' for c in changes]
    if lines_changed:
        lines.append('  - Some individual transactions were added, removed or changed.')
    body = (f'Hello,\n\nChanges were made to your {month:%B %Y} financials for {prop.name} after they were first released to you:\n\n' + '\n'.join(lines) +
            f'\n\nYour statement on the owner portal now shows the updated figures: {portal_url()}\n')
    for email in sorted({c.email.lower() for c in owner_contacts(prop)}):
        send_email(email, f'Changes were made to your {month:%B %Y} financials - {prop.name}', body)


# --------------------------------------------------------------------------------------------------------------------- the year view

def year_table(releases):
    """The released months of one year as columns: {'months': [...], 'rows': [{label, values, total, kind}], 'collects': bool}."""
    releases = sorted(releases, key=lambda r: r.month)
    snaps = [(r, figures_of(r.snapshot)) for r in releases]
    collects = bool(releases) and bool(releases[-1].snapshot.get('owner_collects'))
    if collects:
        spec = (('Expenses paid by Proper Realty', 'expenses_reimbursable', True), ('Commission', 'commission_due', True), ('Owed to Proper Realty', 'owner_owes_us', True))
    else:
        spec = (('Income deposits', 'deposits', True), ('Commission', 'commission_due', True), ('Reimbursable expenses', 'expenses_reimbursable', True),
                ('Expenses paid from trust', 'expenses_direct', True), ('Owner payment for the month', 'owner_due', True), ('Paid to you this month', 'owner_payment', True),
                ('Owed to you at month end', 'owner_payable', False), ('Owed to Proper Realty at month end', 'us_payable', False))
    rows = []
    for label, key, summed in spec:
        values = [f.get(key, ZERO) for _, f in snaps]
        rows.append({'label': label, 'values': values, 'total': sum(values, ZERO) if summed else None})
    return {'releases': releases, 'rows': rows, 'collects': collects}
