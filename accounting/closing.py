"""The month-end close for an association: one sheet of things to be done, each with a chip, and the month locked at the end.

The company checklist has one step, "Close associations"; it lists every association we keep the books for, and each opens this sheet.
"""
from datetime import date

from django.urls import reverse

from . import bankrec, charges, ledger, payables
from .models import AssociationProfile, Bill, ChargeRule, MonthLock


def associations():
    """The associations whose books we keep, by name."""
    return list(AssociationProfile.objects.filter(we_do_financials=True, property__is_active=True).select_related('property').order_by('property__name'))


def _item(key, title, icon, status, detail, url, blocks=True):
    return {'key': key, 'title': title, 'icon': icon, 'status': status, 'detail': detail, 'url': url, 'blocks': blocks}


def close_sheet(association, month_first, today=None):
    from django.utils import timezone
    today = today or timezone.localdate()
    pk, month_param = association.pk, f'{month_first:%Y-%m}'
    items = []

    for bank in association.bank_accounts.filter(is_active=True):
        s = bankrec.summary(bank, month_first)
        detail = {'done': 'Reconciled and closed.', 'ready': 'Everything matches. Close the reconciliation.', 'not_started': 'Upload the bank statement.',
                  'attention': f'{s["need_count"]} thing{"s" if s["need_count"] != 1 else ""} to settle.'}[s['status']]
        status = {'done': 'done', 'ready': 'attention', 'not_started': 'not_started', 'attention': 'attention'}[s['status']]
        items.append(_item(f'bank{bank.pk}', f'{bank.name} bank reconciliation', 'landmark', status, detail, reverse('accounting:bank', args=[pk, bank.pk, month_param])))

    rules = list(ChargeRule.objects.filter(association=association, is_active=True))
    due_rows, pending = [], 0
    for r in rules:
        for d in charges.rule_due_dates(r, ledger.month_end(month_first)):
            if month_first <= d <= ledger.month_end(month_first):
                st = charges.rule_status(r, ledger.month_end(month_first))
                due_rows.append((r, d, st))
                pending += 1 if st['pending'] else 0
    if due_rows:
        items.append(_item('charges', 'Owner charges posted', 'receipt', 'attention' if pending else 'done',
                           f'{pending} charge{"s" if pending != 1 else ""} not posted yet.' if pending else 'Posted to every unit.', reverse('accounting:charges', args=[pk])))
    else:
        items.append(_item('charges', 'Owner charges posted', 'receipt', 'done', 'Nothing was due this month.' if rules else 'No recurring charges are set up.', reverse('accounting:charges', args=[pk]), blocks=False))

    rec = [r for r in payables.recurring_status(association, month_first) if r['due']]
    missing = [r for r in rec if not r['posted']]
    if rec:
        waiting_statement = [r for r in missing if not r['fixed']]
        status = 'done' if not missing else 'attention'
        detail = 'All posted.' if not missing else f'{len(rec) - len(missing)} of {len(rec)} posted' + (f'; {len(waiting_statement)} waiting on the bank statement.' if waiting_statement else '.')
        items.append(_item('recurring', 'Recurring bills', 'repeat', status, detail, reverse('accounting:recurring', args=[pk])))
    else:
        items.append(_item('recurring', 'Recurring bills', 'repeat', 'done', 'None due this month.', reverse('accounting:recurring', args=[pk]), blocks=False))

    drafts = Bill.objects.filter(association=association, status__in=[Bill.Status.DRAFT, Bill.Status.AWAITING_BOARD]).count()
    unpaid = len(payables.bills_to_pay(association))
    items.append(_item('bills', 'Bills waiting', 'file-text', 'attention' if drafts else 'done',
                       (f'{drafts} to review or waiting for the board. ' if drafts else '') + (f'{unpaid} ready to pay.' if unpaid else 'Nothing to pay.' if not drafts else ''),
                       reverse('accounting:bills', args=[pk]), blocks=False))

    profile = ledger.profile_for(association)
    if profile.late_fee_is_set():
        eligible = _late_fee_candidates(association, ledger.month_end(month_first))
        items.append(_item('late', 'Late fees', 'alarm-clock', 'attention' if eligible else 'done', f'{eligible} charge{"s" if eligible != 1 else ""} past the grace period.' if eligible else 'None owed.',
                           reverse('accounting:charges', args=[pk]), blocks=False))
    else:
        items.append(_item('late', 'Late fees', 'alarm-clock', 'attention', 'The rule is not set up yet.', reverse('accounting:settings', args=[pk]), blocks=False))

    locked = MonthLock.objects.filter(association=association, month=month_first).exists()
    blocking = [i for i in items if i['blocks'] and i['status'] != 'done']
    items.append(_item('lock', 'Review and close the month', 'lock', 'done' if locked else ('attention' if not blocking else 'not_started'),
                       'The month is closed.' if locked else ('Finish the items above first.' if blocking else 'Everything is done. Close the month.'), reverse('accounting:reports', args=[pk])))
    return {'items': items, 'locked': locked, 'can_lock': not blocking and not locked, 'blocking': len(blocking)}


def _late_fee_candidates(association, as_of):
    from datetime import timedelta
    from .models import Charge
    profile = ledger.profile_for(association)
    grace = profile.late_fee_grace_days or 0
    n = 0
    for c in Charge.objects.filter(association=association, is_void=False, kind__in=['assessment', 'special'], due_date__lt=as_of - timedelta(days=grace)).prefetch_related('applications'):
        if c.balance > 0 and not Charge.objects.filter(parent=c, kind='late_fee').exists():
            n += 1
    return n


def association_status(association, month_first):
    """One chip for the list of associations: done (month closed), not_started (nothing yet) or attention, with a short note."""
    sheet = close_sheet(association, month_first)
    if sheet['locked']:
        return 'done', 'Closed'
    items = [i for i in sheet['items'] if i['blocks']]
    done = sum(1 for i in items if i['status'] == 'done')
    if done == 0 and all(i['status'] == 'not_started' for i in items):
        return 'not_started', 'Not started'
    return 'attention', f'{done} of {len(items)} done'


def checklist_step(month_first):
    """(status, detail) for the company checklist's "Close associations" step."""
    rows = associations()
    if not rows:
        return 'not_started', 'No association is set up for financials yet.'
    closed = sum(1 for a in rows if association_status(a.property, month_first)[0] == 'done')
    if closed == len(rows):
        return 'done', f'All {len(rows)} associations are closed.'
    if closed == 0 and all(association_status(a.property, month_first)[0] == 'not_started' for a in rows):
        return 'not_started', f'{len(rows)} associations to close.'
    return 'attention', f'{closed} of {len(rows)} closed'
