"""The month-end close for an association: one sheet of things to be done, each with a chip, and the month locked at the end.

The company checklist has one step, "Close associations"; it lists every association we keep the books for, and each opens this sheet.
"""
from datetime import date

from django.urls import reverse

from core.models import Property

from . import bankrec, board_send, charges, labels, ledger, owner_collections, payables, reserve_check, scheduled
from .models import AssociationProfile, ChargeRule, CloseStepDone, JournalRule, MonthLock


def associations(commercial=True):
    """The properties whose books we keep, by name: the associations and (unless `commercial` is False) the commercial buildings."""
    qs = (AssociationProfile.objects.filter(we_do_financials=True, property__is_active=True)
          .exclude(property__is_general=True).exclude(property__name__icontains='(general)'))     # a placeholder ('Commercial (general)', 'Short-Term Rentals (general)') is not a property with books
    if not commercial:
        qs = qs.filter(property__property_type=Property.Type.ASSOCIATION)
    return list(qs.select_related('property').order_by('property__name'))


def _item(key, title, icon, status, detail, url, blocks=True):
    return {'key': key, 'title': title, 'icon': icon, 'status': status, 'detail': detail, 'url': url, 'blocks': blocks}


def close_sheet(association, month_first, today=None):
    """The steps of the month, in the order they are done: look at what posts by itself (owner charges, recurring bills, recurring journal entries), then the bank reconciliations (where the
    owner payments are matched to cash), then the checks that follow it, the late fees and interest, and the owner collections. Only the bank reconciliations stop the month from closing. After the month is closed the last step is sending the financials to the board."""
    from django.utils import timezone
    today = today or timezone.localdate()
    pk, month_param = association.pk, f'{month_first:%Y-%m}'
    profile = ledger.profile_for(association)
    month_end = ledger.month_end(month_first)
    recurring_url = reverse('accounting:recurring', args=[pk])
    auto_off = ' Automatic posting is off for this association: switch it on there.' if not profile.auto_post else ''
    items = []

    lab = labels.labels_for(association)
    commercial = labels.is_commercial(association)
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
    rent_note = ''
    if commercial:
        # the rent is posted from the leases (commercial/rent.py), ahead of the first of the month; this step only checks that every lease has its charge
        from commercial import rent as commercial_rent
        rs = commercial_rent.month_status(association, month_first)
        pending += rs['missing']
        if rs['leases']:
            rent_note = f'Rent posted for {rs["posted"]} of {rs["leases"]} lease{"s" if rs["leases"] != 1 else ""}. '
        due_rows = due_rows or ([None] * rs['leases'])
    if pending:
        items.append(_item('charges', lab['charges_step'], 'receipt', 'attention', rent_note + f'{pending} charge{"s" if pending != 1 else ""} not posted yet: post {"them" if pending != 1 else "it"} there.' + auto_off, charges_url, blocks=False))
    else:
        items.append(_item('charges', lab['charges_step'], 'receipt', 'done', (rent_note or ('Posted to every unit.' if due_rows else 'Nothing was due this month.' if rules else 'No recurring charges are set up.')) + auto_off, charges_url, blocks=False))

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

    # 5. Reserve funds equal reserve cash. The card only says where it stands; the numbers are on its own screen.
    reserve = reserve_check.check(association, month_first)
    if reserve['has_reserve_bank']:
        reserve_url = reverse('accounting:reserve_check', args=[pk, month_param])
        note = 'Matches.' if reserve['diff'] == 0 else ('Difference acknowledged.' if reserve['acknowledged'] else 'Differs: look at it.')
        items.append(_item('reserve_tie', 'Reserve funds equal reserve cash', 'scale', reserve['status'], note, reserve_url, blocks=False))

    # 6. The variable recurring bills got settled in the reconciliation.
    if variable:
        unsettled = [r for r in variable if not r['posted']]
        items.append(_item('variable_bills', 'Variable recurring bills settled', 'repeat', 'attention' if unsettled else 'done',
                           ('Not settled yet: ' + ', '.join(r['rec'].description for r in unsettled) + '. Each is made from its line on the bank statement during the reconciliation.') if unsettled else f'All {len(variable)} were settled in the bank reconciliation.',
                           bills_url, blocks=False))

    # 7. Charge the late fees and interest.
    late_as_of = min(month_end, today)             # the screen judges who was late on the last day of the month being closed
    late_url = reverse('accounting:late_fees', args=[pk]) + f'?as_of={late_as_of:%Y-%m-%d}&month={month_param}'
    if commercial and not (profile.late_fee_is_set() or profile.interest_is_set()):
        pass                                    # no late fee terms set for this property: nothing to charge
    elif profile.late_fee_is_set() or profile.interest_is_set():
        # Done when a person marked the step done for this month (some owners are passed over on purpose), or when nobody is late and nothing is ready.
        from .views_collections import late_fee_data
        rows = late_fee_data(association, late_as_of)
        ready = sum(1 for r in rows if r['ready'])
        marked = CloseStepDone.objects.filter(association=association, month=month_first, key='late_fees').select_related('marked_by').first()
        who = lab['holder_lower']
        if marked:
            detail = f'Marked done on {marked.marked_at:%b} {marked.marked_at.day}.'
            status = 'done'
        elif ready:
            detail = f'{ready} {who}{"s" if ready != 1 else ""} with late fees or interest ready to charge as of {late_as_of:%b} {late_as_of.day}.'
            status = 'attention'
        elif rows:
            detail = f'{len(rows)} {who}{"s" if len(rows) != 1 else ""} late as of {late_as_of:%b} {late_as_of.day}, all charged. Mark the step done when you have looked.'
            status = 'attention'
        else:
            detail, status = 'Nobody is late. Nothing to charge.', 'done'
        items.append(_item('late_fees', 'Charge late fees and interest', 'alarm-clock', status, detail, late_url, blocks=False))
    else:
        items.append(_item('late_fees', 'Charge late fees and interest', 'alarm-clock', 'attention', 'The late fee and interest rules are not set up yet.', reverse('accounting:settings', args=[pk]) + '#late', blocks=False))

    # 7b. A commercial building: last year's CAM reconciliation, in the first months of the year, until it is approved.
    if commercial:
        from commercial import cam as commercial_cam
        cam_note = commercial_cam.close_note(association, month_first)
        if cam_note:
            items.append(_item('cam', 'CAM reconciliation', 'calculator', 'attention', cam_note, reverse('commercial:cam_reconcile', args=[pk]) + f'?year={month_first.year - 1}', blocks=False))

    # 8. Owner collections: right after the late fees is the best time to write the late letters.
    if not commercial:                           # a commercial property has no owner collections for now
        look = owner_collections.attention(association, today)
        marked_c = CloseStepDone.objects.filter(association=association, month=month_first, key='collections').select_related('marked_by').first()
        if marked_c:
            c_status, c_detail = 'done', f'Marked done on {marked_c.marked_at:%b} {marked_c.marked_at.day}.' + (f' {look["count"]} owner{"s" if look["count"] != 1 else ""} still need{"s" if look["count"] == 1 else ""} a look.' if look['count'] else '')
        else:
            c_status = 'attention' if look['count'] else 'done'
            c_detail = (f'{look["count"]} owner{"s" if look["count"] != 1 else ""} need attention: ' + '; '.join(look['bits']) + '. Mark the step done when you have looked.') if look['count'] else ('Nobody needs attention.' if look['open'] else 'Nobody is behind.')
        items.append(_item('collections', 'Owner collections', 'scale', c_status, c_detail, reverse('accounting:collections', args=[pk]) + f'?month={month_param}', blocks=False))

    # The official record: SunBiz cannot be read by a program, so a person checks it by hand (the settings card has the Open on SunBiz button and I checked it).
    official_url = reverse('accounting:settings', args=[pk]) + '#official'
    if profile.sunbiz_alerts:
        more = len(profile.sunbiz_alerts) - 1
        items.append(_item('state', 'Official record', 'building', 'attention', profile.sunbiz_alerts[0] + (f' (and {more} more)' if more else ''), official_url, blocks=False))
    elif profile.legal_name or profile.state_doc_number:
        if profile.sunbiz_needs_check():
            items.append(_item('state', 'Official record', 'building', 'attention', 'Check it by hand: open SunBiz, look at the record and press I checked it.' + (f' Last checked {profile.sunbiz_checked_on:%b} {profile.sunbiz_checked_on.day}.' if profile.sunbiz_checked_on else ' It has not been checked yet.'), official_url, blocks=False))
        else:
            items.append(_item('state', 'Official record', 'building', 'done', f'Checked {profile.sunbiz_checked_on:%b} {profile.sunbiz_checked_on.day}.', official_url, blocks=False))

    lock = MonthLock.objects.filter(association=association, month=month_first).select_related('locked_by').first()
    locked = lock is not None
    blocking = [i for i in items if i['blocks'] and i['status'] != 'done']
    if locked:
        who = (lock.locked_by.get_full_name() or lock.locked_by.username) if lock.locked_by else ''
        lock_detail = f'Approved{" by " + who if who else ""} on {lock.locked_at:%b} {lock.locked_at.day} and the month is closed.'
    else:
        lock_detail = 'Finish the items above first.' if blocking else 'Everything is done. Review the financial package and approve it to close the month.'
    items.append(_item('lock', 'Review and close the month', 'lock', 'done' if locked else ('attention' if not blocking else 'not_started'), lock_detail,
                       reverse('accounting:close_review', args=[pk, month_param])))
    # The last step: the financials go to the board. It comes after the lock, so it can never stop the month from closing.
    sent_status, sent_detail = board_send.status(association, month_first)
    items.append(_item('send_board', lab['package_step'], 'send', sent_status, sent_detail, reverse('accounting:send_board', args=[pk, month_param]), blocks=False))
    return {'items': items, 'locked': locked, 'sent': sent_status == 'done', 'can_lock': not blocking and not locked, 'blocking': len(blocking)}


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
    """One chip for the list of associations: done (month closed and the financials sent to the board), not_started (nothing yet) or attention, with a short note."""
    sheet = close_sheet(association, month_first)
    if sheet['locked']:
        return ('done', 'Closed and sent to the owners' if labels.is_commercial(association) else 'Closed and sent to the board') if sheet['sent'] else ('attention', 'Closed, financials not sent')
    items = [i for i in sheet['items'] if i['blocks']]
    done = sum(1 for i in items if i['status'] == 'done')
    if done == 0 and all(i['status'] == 'not_started' for i in items):
        return 'not_started', 'Not started'
    return 'attention', f'{done} of {len(items)} done'


def checklist_step(month_first, kind='associations'):
    """(status, detail) for the company checklist's "Close associations" step (kind 'associations') or its "Close commercial properties" step (kind 'commercial')."""
    commercial = kind == 'commercial'
    rows = [a for a in associations() if (a.property.property_type == Property.Type.COMMERCIAL) == commercial]
    noun = 'commercial properties' if commercial else 'associations'
    if not rows:
        return 'not_started', f'No {"commercial property" if commercial else "association"} is set up for financials yet.'
    closed = sum(1 for a in rows if association_status(a.property, month_first)[0] == 'done')
    if closed == len(rows):
        return 'done', f'All {len(rows)} {noun} are closed.'
    if closed == 0 and all(association_status(a.property, month_first)[0] == 'not_started' for a in rows):
        return 'not_started', f'{len(rows)} {noun} to close.'
    return 'attention', f'{closed} of {len(rows)} closed'
