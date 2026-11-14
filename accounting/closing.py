"""The month-end close for an association: one sheet of things to be done, each with a chip, and the month locked at the end.

The company checklist has one step, "Close associations"; it lists every association we keep the books for, and each opens this sheet.
"""
from datetime import date

from django.urls import reverse

from . import bankrec, charges, ledger, owner_collections, payables, scheduled
from .models import AssociationProfile, ChargeRule, JournalRule, MonthLock


def associations():
    """The associations whose books we keep, by name."""
    return list(AssociationProfile.objects.filter(we_do_financials=True, property__is_active=True).select_related('property').order_by('property__name'))


def _item(key, title, icon, status, detail, url, blocks=True):
    return {'key': key, 'title': title, 'icon': icon, 'status': status, 'detail': detail, 'url': url, 'blocks': blocks}


def close_sheet(association, month_first, today=None):
    """The steps of the month, in the order they are done: look at what posts by itself (owner charges, recurring bills, recurring journal entries), then the bank reconciliations (where the
    owner payments are matched to cash), then the checks that follow it, the late fees and interest, and the owner collections. Only the bank reconciliations stop the month from closing."""
    from django.utils import timezone
    today = today or timezone.localdate()
    pk, month_param = association.pk, f'{month_first:%Y-%m}'
    profile = ledger.profile_for(association)
    month_end = ledger.month_end(month_first)
    recurring_url = reverse('accounting:recurring', args=[pk])
    auto_off = ' Automatic posting is off for this association: switch it on there.' if not profile.auto_post else ''
    items = []

    # 1. This month's owner charges: they post by themselves a few days early; this step only checks, and posts any that did not.
    rules = list(ChargeRule.objects.filter(association=association, is_active=True))
    due_rows, pending = [], 0
    for r in rules:
        for d in charges.rule_due_dates(r, month_end):
            if month_first <= d <= month_end:
                st = charges.rule_status(r, month_end)
                due_rows.append((r, d, st))
                pending += 1 if st['pending'] else 0
    charges_url = recurring_url + f'?tab=charges&month={month_param}'
    if pending:
        items.append(_item('charges', 'Owner charges posted', 'receipt', 'attention', f'{pending} charge{"s" if pending != 1 else ""} not posted yet: post {"them" if pending != 1 else "it"} there.' + auto_off, charges_url, blocks=False))
    else:
        items.append(_item('charges', 'Owner charges posted', 'receipt', 'done', ('Posted to every unit.' if due_rows else 'Nothing was due this month.' if rules else 'No recurring charges are set up.') + auto_off, charges_url, blocks=False))

    # 2. Recurring bills: the fixed ones are checked; the ones whose amount changes are settled in the bank reconciliation and are kept apart.
    rec = [r for r in payables.recurring_status(association, month_first) if r['due']]
    fixed, variable = [r for r in rec if r['fixed']], [r for r in rec if not r['fixed']]
    fixed_missing = [r for r in fixed if not r['posted']]
    bills_url = recurring_url + f'?tab=bills&month={month_param}'
    variable_note = f' {len(variable)} variable bill{"s" if len(variable) != 1 else ""} (the amount changes) will be settled in the bank reconciliation.' if variable else ''
    if fixed_missing:
        items.append(_item('recurring', 'Recurring bills', 'repeat', 'attention', f'{len(fixed) - len(fixed_missing)} of {len(fixed)} fixed bills posted; not posted yet: ' + ', '.join(r['rec'].description for r in fixed_missing) + '.' + variable_note + auto_off,
                           bills_url, blocks=False))
    else:
        items.append(_item('recurring', 'Recurring bills', 'repeat', 'done', ('All fixed bills posted.' if fixed else 'No fixed recurring bills this month.') + variable_note, bills_url, blocks=False))

    # 3. Recurring journal entries (the reserve contribution is one of them).
    entries_url = recurring_url + f'?tab=entries&month={month_param}'
    entry_bits, entry_open = [], 0
    for rule in JournalRule.objects.filter(association=association, is_active=True):
        st = scheduled.status(association, rule, month_first, today)
        if st['state'] == 'posted':
            entry_bits.append(f'{rule.name}: posted {st["entry"].date:%b} {st["entry"].date.day}, J-{st["entry"].number}.')
            continue
        entry_open += 1
        if st['state'] == 'no_budget':
            entry_bits.append(f'{rule.name}: no {st["split"]["year"]} budget with reserve lines yet: approve the budget first.')
        else:
            how = 'It posts itself on its day.' if (rule.auto_post and profile.auto_post) else 'Automatic posting is off for it.'
            entry_bits.append(f'{rule.name}: not posted yet, ${st["split"]["total"]:,.2f} dated {st["day"]:%b} {st["day"].day}. {how}')
    items.append(_item('entries', 'Recurring journal entries', 'piggy-bank', 'attention' if entry_open else 'done', ' '.join(entry_bits) if entry_bits else 'No recurring journal entries are set up.', entries_url, blocks=False))

    # 4. The bank reconciliations: owner payments are matched to cash here.
    for bank in association.bank_accounts.filter(is_active=True):
        s = bankrec.summary(bank, month_first)
        detail = {'done': 'Reconciled and closed.', 'ready': 'Everything matches. Close the reconciliation.', 'not_started': 'Upload the bank statement.',
                  'attention': f'{s["need_count"]} thing{"s" if s["need_count"] != 1 else ""} to settle.'}[s['status']]
        status = {'done': 'done', 'ready': 'attention', 'not_started': 'not_started', 'attention': 'attention'}[s['status']]
        items.append(_item(f'bank{bank.pk}', f'{bank.label} bank reconciliation', 'landmark', status, detail, reverse('accounting:bank', args=[pk, bank.pk, month_param])))

    # 5. Reserve funds equal reserve cash.
    tie = scheduled.reserve_tie(association, month_end)
    if tie['has_reserve_bank']:
        bs_url = reverse('accounting:report_bs', args=[pk]) + f'?end={month_end:%Y-%m-%d}'
        if tie['diff'] == 0:
            items.append(_item('reserve_tie', 'Reserve funds equal reserve cash', 'scale', 'done', f'Both are ${tie["cash"]:,.2f}.', bs_url, blocks=False))
        else:
            side = 'more' if tie['diff'] > 0 else 'less'
            items.append(_item('reserve_tie', 'Reserve funds equal reserve cash', 'scale', 'attention', f'The reserve cash (${tie["cash"]:,.2f}) is ${abs(tie["diff"]):,.2f} {side} than the reserve funds (${tie["funds"]:,.2f}). A reserve project paid from operating, or a transfer not yet booked, is the usual reason.',
                               bs_url, blocks=False))

    # 6. The variable recurring bills got settled in the reconciliation.
    if variable:
        unsettled = [r for r in variable if not r['posted']]
        items.append(_item('variable_bills', 'Variable recurring bills settled', 'repeat', 'attention' if unsettled else 'done',
                           ('Not settled yet: ' + ', '.join(r['rec'].description for r in unsettled) + '. Each is made from its line on the bank statement during the reconciliation.') if unsettled else f'All {len(variable)} were settled in the bank reconciliation.',
                           bills_url, blocks=False))

    # 7. Charge the late fees and interest.
    late_url = reverse('accounting:late_fees', args=[pk])
    if profile.late_fee_is_set() or profile.interest_is_set():
        fee_units = {c.unit_id for c, fee, due in charges._late_fee_rows(association, today)}
        interest_units = {c.unit_id for c, mf, me, amount in charges._interest_rows(association, today)}
        ready = len(fee_units | interest_units)
        items.append(_item('late_fees', 'Charge late fees and interest', 'alarm-clock', 'attention' if ready else 'done',
                           f'{ready} owner{"s" if ready != 1 else ""} with late fees or interest ready to charge.' if ready else 'Nothing to charge.', late_url, blocks=False))
    else:
        items.append(_item('late_fees', 'Charge late fees and interest', 'alarm-clock', 'attention', 'The late fee and interest rules are not set up yet.', reverse('accounting:settings', args=[pk]) + '#late', blocks=False))

    # 8. Owner collections: right after the late fees is the best time to write the late letters.
    look = owner_collections.attention(association, today)
    items.append(_item('collections', 'Owner collections', 'scale', 'attention' if look['count'] else 'done',
                       (f'{look["count"]} owner{"s" if look["count"] != 1 else ""} need attention: ' + '; '.join(look['bits']) + '.') if look['count'] else ('Nobody needs attention.' if look['open'] else 'Nobody is behind.'),
                       reverse('accounting:collections', args=[pk]), blocks=False))

    if profile.sunbiz_alerts:
        more = len(profile.sunbiz_alerts) - 1
        items.append(_item('state', 'Official record', 'building', 'attention', profile.sunbiz_alerts[0] + (f' (and {more} more)' if more else ''), reverse('accounting:settings', args=[pk]) + '#official', blocks=False))

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
