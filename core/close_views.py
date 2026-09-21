"""The month-end close screens (admin only): one overview across every rental for a
month, a coding + reconciliation screen for one set of books' month, and — for a
property kept unit by unit — a consolidated page that adds its units up. The rules
live in core/ledger.py and core/recon.py."""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import ledger, recon
from .models import ClosedMonthChange, FinancialsSettings, LedgerLine, Property, QuickBooksToken, Unit
from .views import _is_admin

CATEGORY_LABELS = dict(LedgerLine.Category.choices)


def _parse_month(raw, default):
    try:
        return datetime.strptime(raw or '', '%Y-%m').date().replace(day=1)
    except ValueError:
        return default


def _sync_note(request):
    """Run a full sync and say what happened."""
    done, error = ledger.sync_all()
    if error:
        messages.error(request, f'Synced {done} rental{"" if done == 1 else "s"}. Problems: {error}')
    else:
        messages.success(request, f'Pulled the latest transactions from QuickBooks for {done} rental{"" if done == 1 else "s"}.')


def _has_warning(row):
    return any(i['level'] == 'warn' for i in row['checks'])


def _close_ready_rows(rows, month, user):
    """Closes every ready row that has no warning; returns (closed, skipped for a warning)."""
    closed, skipped = [], 0
    for row in rows:
        if row['state'] != 'ready':
            continue
        if _has_warning(row):
            skipped += 1        # a warning needs a person's eye: close that one from its own page
            continue
        ledger.close_month(row['book'], month, user)
        closed.append(row)
    return closed, skipped


def _closed_message(closed, skipped, month):
    units = sum(1 for r in closed if r['unit'] is not None)
    noun = 'unit' if units == len(closed) and units else ('rental or unit' if units else 'rental')
    n = len(closed)
    return f'Closed {n} {noun}{"" if n == 1 else "s"} for {month:%B %Y}.' + (f' {skipped} more have warnings to look at first.' if skipped else '')


@login_required
@user_passes_test(_is_admin)
def close_overview(request):
    today = timezone.localdate()
    default_month = ledger.previous_month(today)
    month = _parse_month(request.GET.get('month') or request.POST.get('month'), default_month)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'sync':
            _sync_note(request)
        elif action == 'set_start':
            start = _parse_month(request.POST.get('books_start'), None)
            if start is None:
                messages.error(request, 'Choose the first month to manage.')
            else:
                settings_row = FinancialsSettings.get()
                settings_row.books_start = start
                settings_row.save(update_fields=['books_start'])
                messages.success(request, f'Books start in {start:%B %Y}. Sync to pull in transactions from then.')
        elif action == 'close_ready':
            rows = [r for g in ledger.overview(month) for r in g['rows']]
            closed, skipped = _close_ready_rows(rows, month, request.user)
            messages.success(request, _closed_message(closed, skipped, month))
        return redirect(f'{reverse("close_overview")}?month={month:%Y-%m}')

    groups = ledger.overview(month)
    rows = [r for g in groups for r in g['rows']]
    token = QuickBooksToken.objects.first()
    return render(request, 'core/close_overview.html', {
        'month': month, 'previous': ledger.previous_month(month), 'next': ledger.next_month(month), 'groups': groups,
        'connected': token is not None, 'synced_at': token.ledger_synced_at if token else None, 'sync_error': token.ledger_sync_error if token else '',
        'books_start': ledger.books_start(), 'books_start_saved': FinancialsSettings.get().books_start is not None,
        'counts': {s: sum(1 for r in rows if r['state'] == s) for s in ('closed', 'ready', 'open', 'needs_accounts')},
        'ready_clean': sum(1 for r in rows if r['state'] == 'ready' and not _has_warning(r)),
        'month_over': today >= ledger.next_month(month), 'is_current': month == ledger.month_of(today),
    })


def _page_url(month, prop, unit=None):
    if unit is None:
        return reverse('close_property', args=[month.strftime('%Y-%m'), prop.pk])
    return reverse('close_unit', args=[month.strftime('%Y-%m'), prop.pk, unit.pk])


def _consolidated_page(request, prop, month):
    """A unit-level property's month: its units added up, with a way in to each unit."""
    here = _page_url(month, prop)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'sync':
                token = QuickBooksToken.objects.first()
                if token is None:
                    messages.error(request, 'QuickBooks is not connected.')
                else:
                    ledger.sync_property(token, prop)
                    messages.success(request, f'Pulled the latest from QuickBooks for every unit of {prop.name}.')
            elif action == 'close_ready':
                rows = [ledger.status_row(b, month) for b in ledger.books_for(prop, month)]
                closed, skipped = _close_ready_rows(rows, month, request.user)
                messages.success(request, _closed_message(closed, skipped, month))
        except (ledger.LedgerSyncError, ledger.CloseError) as exc:
            messages.error(request, str(exc))
        return redirect(here)
    rows = [ledger.status_row(b, month) for b in ledger.books_for(prop, month)]
    consolidated = ledger.consolidate(rows)
    return render(request, 'core/close_consolidated.html', {
        'property': prop, 'month': month, 'previous': ledger.previous_month(month), 'next': ledger.next_month(month),
        'rows': rows, 'consolidated': consolidated,
        'prev_url': _page_url(ledger.previous_month(month), prop), 'next_url': _page_url(ledger.next_month(month), prop),
        'ready_clean': sum(1 for r in rows if r['state'] == 'ready' and not _has_warning(r)),
    })


@login_required
@user_passes_test(_is_admin)
def close_property(request, month, pk, unit_pk=None):
    prop = get_object_or_404(Property.objects.select_related('qb_expense_account', 'qb_trust_account'), pk=pk, property_type=Property.Type.SHORT_TERM_RENTAL)
    month = _parse_month(month, None)
    if month is None:
        return redirect('close_overview')
    level = ledger.month_level(prop, month)
    if unit_pk is None and level == Property.FinancialsLevel.UNIT:
        return _consolidated_page(request, prop, month)
    unit = None
    if unit_pk is not None:
        unit = get_object_or_404(Unit.objects.select_related('qb_expense_account', 'qb_trust_account'), pk=unit_pk, property=prop)
        if level != Property.FinancialsLevel.UNIT:
            return redirect(_page_url(month, prop))
    book = ledger.Book(prop, unit)
    here = _page_url(month, prop, unit)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'save_coding':
                assignments = {}
                for key, value in request.POST.items():
                    if key.startswith('cat_') and key[4:].isdigit():
                        assignments[int(key[4:])] = value
                changed = ledger.code_lines(book, month, request.user, assignments)
                messages.success(request, f'Saved. {changed} line{"" if changed == 1 else "s"} re-coded; everything on this page is now marked reviewed.')
            elif action == 'accept_all':
                count = ledger.accept_all(book, month, request.user)
                messages.success(request, f'{count} line{"" if count == 1 else "s"} accepted as shown.')
            elif action == 'acknowledge_changes':
                ledger.acknowledge_changes(book, month)
                messages.success(request, 'Marked as looked at.')
            elif action == 'sync':
                token = QuickBooksToken.objects.first()
                if token is None:
                    messages.error(request, 'QuickBooks is not connected.')
                else:
                    summary = ledger.sync_book(token, book)
                    parts = [f'{c["new"]} new, {c["updated"]} changed, {c["removed"]} gone' for c in summary.values()]
                    messages.success(request, 'Pulled the latest from QuickBooks (' + '; '.join(parts) + ').')
            elif action == 'resolve_drift':
                ClosedMonthChange.objects.filter(month=month, pk=request.POST.get('change_id'), **book.scope()).update(resolved=True)
                messages.success(request, 'Marked as dealt with.')
            elif action == 'accept_recon':
                prior = request.POST.get('prior') == '1'
                recon.accept_item(book, month, request.user, request.POST.get('kind', ''), request.POST.get('key', ''), request.POST.get('note', ''), prior_period=prior)
                messages.success(request, 'Marked as from before the books.' if prior else 'Accepted as a reconciling item.')
            elif action == 'unaccept_recon':
                recon.unaccept_item(book, month, request.POST.get('kind', ''), request.POST.get('key', ''))
                messages.success(request, 'No longer accepted — it needs a fix or a fresh acceptance.')
            elif action == 'close':
                acknowledged = [k[4:] for k in request.POST if k.startswith('ack_')]
                ledger.close_month(book, month, request.user, acknowledged=acknowledged, note=request.POST.get('note', ''))
                messages.success(request, f'{book.name} is closed for {month:%B %Y}. Its transactions are locked.')
        except ledger.LedgerSyncError as exc:
            messages.error(request, str(exc))
        except ledger.CloseError as exc:
            messages.error(request, str(exc))
        return redirect(here)

    close = ledger.MonthClose.objects.filter(month=month, **book.scope()).first()
    lines = list(ledger.month_lines(book, month))
    removed = list(LedgerLine.objects.filter(month=month, status=LedgerLine.Status.REMOVED, **book.scope()))
    lines = sorted(lines, key=lambda l: (l.txn_date, l.role, l.txn_type, l.txn_id))
    for line in lines:
        line.options = [(c, CATEGORY_LABELS[c]) for c in ledger.ROLE_CATEGORIES[line.role]]
    rec = recon.from_close(close) if close else (recon.reconcile(book, month) if book.mapped else None)
    items = ledger.checks(book, month, rec=rec) if close is None else []
    return render(request, 'core/close_property.html', {
        'property': prop, 'book': book, 'unit': unit, 'title': book.name, 'month': month, 'close': close,
        'previous': ledger.previous_month(month), 'next': ledger.next_month(month),
        'prev_url': _page_url(ledger.previous_month(month), prop, unit), 'next_url': _page_url(ledger.next_month(month), prop, unit),
        'back_url': (_page_url(month, prop) if unit is not None else reverse('close_overview') + f'?month={month:%Y-%m}'),
        'back_label': (f'{prop.name} — all units' if unit is not None else 'Month-end close'),
        'lines': lines, 'all_categories': list(CATEGORY_LABELS.items()), 'removed': removed, 'checks': items, 'can_close': ledger.can_close(items),
        'warn_keys': [i['key'] for i in items if i['level'] == 'warn'],
        'totals': ledger.closed_summary(close) if close else ledger.totals(book, month, lines),
        'recon': rec,
        'drift': ClosedMonthChange.objects.filter(month=month, resolved=False, **book.scope()),
        'unreviewed': sum(1 for l in lines if not l.reviewed), 'changed': sum(1 for l in lines if l.changed_in_qb),
        'synced_at': book.ledger_synced_at,
    })
