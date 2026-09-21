"""The month-end close screens (admin only): one overview across every rental for a
month, and a coding screen for one rental's month. The rules live in core/ledger.py."""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import ledger
from .models import ClosedMonthChange, FinancialsSettings, LedgerLine, Property, QuickBooksToken
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
            closed, skipped = 0, 0
            for prop in ledger.rentals():
                row = ledger.status_row(prop, month)
                if row['state'] != 'ready':
                    continue
                if any(i['level'] == 'warn' for i in row['checks']):
                    skipped += 1        # a warning needs a person's eye: close that one from its own page
                    continue
                ledger.close_month(prop, month, request.user)
                closed += 1
            messages.success(request, f'Closed {closed} rental{"" if closed == 1 else "s"} for {month:%B %Y}.' + (f' {skipped} more have warnings to look at first.' if skipped else ''))
        return redirect(f'{reverse("close_overview")}?month={month:%Y-%m}')

    rows = [ledger.status_row(prop, month) for prop in ledger.rentals()]
    token = QuickBooksToken.objects.first()
    return render(request, 'core/close_overview.html', {
        'month': month, 'previous': ledger.previous_month(month), 'next': ledger.next_month(month), 'rows': rows,
        'connected': token is not None, 'synced_at': token.ledger_synced_at if token else None, 'sync_error': token.ledger_sync_error if token else '',
        'books_start': ledger.books_start(), 'books_start_saved': FinancialsSettings.get().books_start is not None,
        'counts': {s: sum(1 for r in rows if r['state'] == s) for s in ('closed', 'ready', 'open', 'needs_accounts')},
        'ready_clean': sum(1 for r in rows if r['state'] == 'ready' and not any(i['level'] == 'warn' for i in r['checks'])),
        'month_over': today >= ledger.next_month(month), 'is_current': month == ledger.month_of(today),
    })


@login_required
@user_passes_test(_is_admin)
def close_property(request, month, pk):
    prop = get_object_or_404(Property.objects.select_related('qb_expense_account', 'qb_trust_account'), pk=pk, property_type=Property.Type.SHORT_TERM_RENTAL)
    month = _parse_month(month, None)
    if month is None:
        return redirect('close_overview')
    here = f'{reverse("close_property", args=[month.strftime("%Y-%m"), prop.pk])}'
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'save_coding':
                assignments = {}
                for key, value in request.POST.items():
                    if key.startswith('cat_') and key[4:].isdigit():
                        assignments[int(key[4:])] = value
                changed = ledger.code_lines(prop, month, request.user, assignments)
                messages.success(request, f'Saved. {changed} line{"" if changed == 1 else "s"} re-coded; everything on this page is now marked reviewed.')
            elif action == 'accept_all':
                count = ledger.accept_all(prop, month, request.user)
                messages.success(request, f'{count} line{"" if count == 1 else "s"} accepted as shown.')
            elif action == 'acknowledge_changes':
                ledger.acknowledge_changes(prop, month)
                messages.success(request, 'Marked as looked at.')
            elif action == 'sync':
                token = QuickBooksToken.objects.first()
                if token is None:
                    messages.error(request, 'QuickBooks is not connected.')
                else:
                    summary = ledger.sync_property(token, prop)
                    parts = [f'{c["new"]} new, {c["updated"]} changed, {c["removed"]} gone' for c in summary.values()]
                    messages.success(request, 'Pulled the latest from QuickBooks (' + '; '.join(parts) + ').')
            elif action == 'resolve_drift':
                ClosedMonthChange.objects.filter(property=prop, month=month, pk=request.POST.get('change_id')).update(resolved=True)
                messages.success(request, 'Marked as dealt with.')
            elif action == 'close':
                acknowledged = [k[4:] for k in request.POST if k.startswith('ack_')]
                ledger.close_month(prop, month, request.user, acknowledged=acknowledged, note=request.POST.get('note', ''))
                messages.success(request, f'{prop.name} is closed for {month:%B %Y}. Its transactions are locked.')
        except ledger.LedgerSyncError as exc:
            messages.error(request, str(exc))
        except ledger.CloseError as exc:
            messages.error(request, str(exc))
        return redirect(here)

    close = ledger.MonthClose.objects.filter(property=prop, month=month).first()
    lines = list(ledger.month_lines(prop, month))
    removed = list(LedgerLine.objects.filter(property=prop, month=month, status=LedgerLine.Status.REMOVED))
    sections = []
    for role, title, account in ((LedgerLine.Role.TRUST, 'Owner trust account', prop.qb_trust_account), (LedgerLine.Role.EXPENSE, 'Reimbursable-expense account', prop.qb_expense_account)):
        mine = [l for l in lines if l.role == role]
        sections.append({
            'role': role, 'title': title, 'account': account, 'lines': mine,
            'options': [(c, CATEGORY_LABELS[c]) for c in ledger.ROLE_CATEGORIES[role]],
        })
    items = ledger.checks(prop, month) if close is None else []
    return render(request, 'core/close_property.html', {
        'property': prop, 'month': month, 'previous': ledger.previous_month(month), 'next': ledger.next_month(month), 'close': close,
        'sections': sections, 'removed': removed, 'checks': items, 'can_close': ledger.can_close(items),
        'warn_keys': [i['key'] for i in items if i['level'] == 'warn'],
        'totals': ledger.closed_summary(close) if close else ledger.totals(prop, month, lines),
        'drift': ClosedMonthChange.objects.filter(property=prop, month=month, resolved=False),
        'unreviewed': sum(1 for l in lines if not l.reviewed), 'changed': sum(1 for l in lines if l.changed_in_qb),
        'synced_at': prop.ledger_synced_at,
    })
