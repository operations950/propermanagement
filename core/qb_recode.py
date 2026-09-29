"""Recoding bank deposits in QuickBooks from the platform payout files.

The bank feed puts an Airbnb or VRBO deposit into "Uncategorized Airbnb Income" / "Uncategorized VRBO Income". The payout
file says what that deposit is made up of (a payout and its lines: each reservation, its pass-through tax, a resolution).
When a deposit's date and amount match one payout, its single uncategorized line is replaced by one line per payout line,
each described "Guest | Reservation code | Amount" and posted to the trust (balance-sheet) account saved for that
reservation's property or unit. Anything the program is not sure of is left alone, with the reason, for a person.

  plan()   reads QuickBooks and works out, without changing anything, what would be done and what is blocked
  apply()  makes one planned change (re-reading the deposit first), and records what it did (QBRecode) so it can be audited
           and undone by hand

Never guesses: no payout found, two payouts equally likely, a line with no reservation, a reservation with no unit, or a
property/unit with no trust account saved each leave the deposit untouched and say why."""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from . import quickbooks
from .models import Property, QBRecode, QuickBooksAccount

UNCATEGORIZED = {'airbnb': 'Uncategorized Airbnb Income', 'vrbo': 'Uncategorized VRBO Income'}
EARLY = timedelta(days=3)         # a deposit may be booked a little before the payout's own date
LATE = timedelta(days=10)         # ... and usually lands within a few days after it
LOOKBACK_DAYS = 150
ZERO = Decimal('0.00')


def _d(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))


def line_description(guest, code, amount):
    """The bookkeeping format: [Guest Name] | [Reservation Code] | [Amount]."""
    return f'{guest} | {code} | {amount:.2f}'


def trust_account_for(booking):
    """(QuickBooksAccount or None, label, error): the balance-sheet account this reservation's money belongs in - the unit's
    for a property kept unit by unit, otherwise the property's."""
    prop = booking.property
    if prop.financials_level == Property.FinancialsLevel.UNIT:
        unit = booking.unit
        if unit is None:
            who = f'{booking.guest_name or "a reservation"} ({booking.external_uid})' if booking.external_uid else (booking.guest_name or 'a reservation')
            return None, prop.name, f'{prop.name}: {who} is not assigned to a unit yet - assign it on the Reservations page ("Unit" button), or match it to a unit-coded deposit first'
        label, account = f'{prop.name} - {unit.label}', unit.qb_trust_account
    else:
        label, account = prop.name, prop.qb_trust_account
    if account is None:
        return None, label, f'{label} has no QuickBooks balance-sheet (trust) account saved - choose one on the property/unit record'
    return account, label, ''


def _account_ids():
    accounts, errors = {}, []
    for source, name in UNCATEGORIZED.items():
        account = QuickBooksAccount.objects.filter(name__iexact=name).first() or QuickBooksAccount.objects.filter(fully_qualified_name__iendswith=name).first()
        if account is None:
            errors.append(f'QuickBooks has no account called "{name}" (or it has not been synced yet) - {source.title()} deposits can not be found.')
        else:
            accounts[source] = account
    return accounts, errors


def _uncategorized_lines(deposit, account_ids):
    return [ln for ln in deposit.get('Line', []) if ((ln.get('DepositLineDetail') or {}).get('AccountRef') or {}).get('value') in account_ids]


def _fill_missing_units(items):
    """Before giving up on a reservation with no unit, try to resolve one - same as a normal booking import would,
    just retroactively for a reservation that slipped through with none. Two tries, strongest first:
      1. Its own listing name is already mapped to a unit (PropertyListingName) - a direct, certain match; this is
         exactly what a normal import already does at the moment a reservation is created, so a booking still
         missing its unit despite this usually means the mapping was only made (or corrected) afterward.
      2. The same trick the month-end reconciliation already uses: a unit is inferable from the bank if a deposit
         already coded to one unit names this reservation's confirmation code in its memo (see
         recon.assign_units_from_deposits).
    Tried once per property per call, not once per line."""
    from .models import PropertyListingName
    booking_ids = {item.booking_id for item in items if item.booking_id and item.booking.unit_id is None}
    if booking_ids:
        from onsite.models import Booking
        by_prop_platform = {}
        for b in Booking.objects.filter(pk__in=booking_ids).exclude(listing_name=''):
            by_prop_platform.setdefault((b.property_id, b.source), []).append(b)
        for (prop_id, source), bookings in by_prop_platform.items():
            names = {b.listing_name for b in bookings}
            mapped = {pln.name: pln.unit_id for pln in PropertyListingName.objects.filter(property_id=prop_id, platform=source, name__in=names, unit__isnull=False)}
            for b in bookings:
                if b.listing_name in mapped:
                    b.unit_id = mapped[b.listing_name]
                    b.save(update_fields=['unit'])
        for item in items:
            if item.booking_id and item.booking.unit_id is None:
                item.booking.refresh_from_db(fields=['unit'])

    from . import recon
    tried = set()
    for item in items:
        if item.booking_id is None or item.booking.unit_id is not None:
            continue
        prop = item.booking.property
        if prop.financials_level != Property.FinancialsLevel.UNIT or prop.pk in tried:
            continue
        tried.add(prop.pk)
        recon.assign_units_from_deposits(prop)
    if tried:
        for item in items:
            if item.booking_id and item.booking.unit_id is None and item.booking.property_id in tried:
                item.booking.refresh_from_db(fields=['unit'])


def _lines_for(batch):
    """(new lines, error) for one payout: a line per payout line, to the trust account of its reservation's property/unit."""
    items = list(batch.items.select_related('booking__property', 'booking__unit'))
    if not items:
        return None, 'the payout file has no lines for this payout - upload the transactions file again'
    if batch.breakdown_ok is False:
        return None, f'the payout lines add up to {batch.items_total}, not {batch.amount}'
    _fill_missing_units(items)
    lines, problems = [], []
    for item in items:
        if item.booking_id is None:
            problems.append(f'{item.type_label} {item.external_uid} ({item.amount}) is not a reservation in the system'.replace('  ', ' '))
            continue
        account, label, error = trust_account_for(item.booking)
        if error:
            problems.append(error)
            continue
        guest = item.guest_name or item.booking.guest_name or item.type_label
        code = item.external_uid or item.booking.external_uid
        lines.append({'account_id': account.qb_id, 'account_name': account.name, 'where': label, 'amount': item.amount,
                      'description': line_description(guest, code, item.amount), 'kind': item.type_label})
    if problems:
        return None, '; '.join(dict.fromkeys(problems))
    return lines, ''


def plan(token, today=None, days=LOOKBACK_DAYS):
    """What would be done. {'items': [...], 'errors': [...]}: each item is a deposit sitting in an uncategorized account with
    a status of 'ready' (safe to send), 'blocked' (a payout was found but a line can't be posted - the reason says why), or
    'unsure' (no single payout matches - left for a person)."""
    from onsite.models import PayoutBatch
    today = today or timezone.localdate()
    accounts, errors = _account_ids()
    result = {'items': [], 'errors': errors}
    if not accounts:
        return result
    from . import payout_tracking
    since = max(today - timedelta(days=days), payout_tracking.BOOKKEEPING_START).isoformat()
    deposits, error = quickbooks.query(token, f"select * from Deposit where TxnDate >= '{since}' order by TxnDate")
    if error:
        result['errors'].append(error)
        return result
    source_of = {a.qb_id: s for s, a in accounts.items()}
    done_deposits = set(QBRecode.objects.values_list('deposit_qb_id', flat=True))
    used_payouts = set(QBRecode.objects.values_list('payout_id', flat=True))
    for dep in deposits:
        uncat = _uncategorized_lines(dep, set(source_of))
        if not uncat or dep['Id'] in done_deposits:
            continue
        source = source_of[uncat[0]['DepositLineDetail']['AccountRef']['value']]
        amount = sum((_d(ln['Amount']) for ln in uncat), ZERO)
        when = date.fromisoformat(dep['TxnDate'])
        memo = ' '.join(filter(None, [dep.get('PrivateNote', '')] + [ln.get('Description', '') for ln in uncat]))[:120]
        item = {'qb_id': dep['Id'], 'date': when, 'amount': amount, 'source': source, 'raw': dep, 'payout': None, 'status': 'unsure', 'reason': '', 'lines': [], 'memo': memo}
        candidates = [b for b in PayoutBatch.objects.filter(source=source, amount=amount, date__gte=when - LATE, date__lte=when + EARLY).order_by('date', 'pk') if b.pk not in used_payouts]
        if not candidates:
            item['reason'] = 'no payout in the uploaded files has this amount and date - left for a person'
        else:
            nearest = sorted(candidates, key=lambda b: (abs((when - b.date).days), b.pk))
            if len(nearest) > 1 and abs((when - nearest[0].date).days) == abs((when - nearest[1].date).days):
                item['reason'] = f'{len(nearest)} payouts of this amount are equally close - left for a person'
            else:
                batch = nearest[0]
                used_payouts.add(batch.pk)
                item['payout'] = batch
                lines, problem = _lines_for(batch)
                if problem:
                    item['status'], item['reason'] = 'blocked', problem
                else:
                    item['status'], item['lines'] = 'ready', lines
        result['items'].append(item)
    return result


def apply(token, item, user=None, automatic=False):
    """Sends one planned change. Returns (True, '') or (False, why). Locked per deposit so two overlapping
    calls for the same one (e.g. a double-click before the first request's redirect lands) can't both pass
    the "already recoded" check and each independently create+delete their own replacement - confirmed as
    the real cause of one production deposit ending up recoded three times over instead of once."""
    from django.core.cache import cache
    lock_key = f'qb_recode_apply_{item["qb_id"]}'
    if not cache.add(lock_key, True, timeout=120):
        return False, 'already being sent to QuickBooks - try again shortly'
    try:
        return _apply_locked(token, item, user=user, automatic=automatic)
    finally:
        cache.delete(lock_key)


def _apply_locked(token, item, user=None, automatic=False):
    """The deposit is read again first, so a change made in QuickBooks since the plan was drawn up is never
    overwritten."""
    if item['status'] != 'ready':
        return False, item['reason'] or 'not ready'
    if QBRecode.objects.filter(deposit_qb_id=item['qb_id']).exists():
        return False, 'already recoded'
    accounts, errors = _account_ids()
    if errors:
        return False, errors[0]
    fresh, error = quickbooks.query(token, f"select * from Deposit where Id = '{item['qb_id']}'")
    if error or not fresh:
        return False, error or 'the deposit is no longer in QuickBooks'
    deposit = fresh[0]
    uncat = _uncategorized_lines(deposit, {a.qb_id for a in accounts.values()})
    if deposit.get('SyncToken') != item['raw'].get('SyncToken') or sum((_d(ln['Amount']) for ln in uncat), ZERO) != item['amount']:
        return False, 'the deposit was changed in QuickBooks after the check - run it again'
    keep = [ln for ln in deposit['Line'] if ln not in uncat]
    klass = next((ln['DepositLineDetail']['ClassRef'] for ln in uncat if (ln.get('DepositLineDetail') or {}).get('ClassRef')), None)
    new = []
    for ln in item['lines']:
        detail = {'AccountRef': {'value': ln['account_id'], 'name': ln['account_name']}}
        if klass:
            detail['ClassRef'] = klass
        new.append({'Amount': float(ln['amount']), 'Description': ln['description'], 'DetailType': 'DepositLineDetail', 'DepositLineDetail': detail})
    # Neither a sparse NOR a full update can actually remove the old uncategorized line - confirmed directly
    # against production: QuickBooks returns the object completely unchanged, SyncToken not even incremented,
    # when a Line array omits an existing entry. An update can only ADD or EDIT-by-Id; genuinely shrinking the
    # Line array means deleting the deposit and recreating it. Create the replacement FIRST and only delete
    # the original once it's confirmed to exist, so a failure here never loses the deposit entirely.
    create_body = {k: v for k, v in deposit.items() if k not in ('Id', 'SyncToken', 'MetaData', 'domain', 'sparse', 'TotalAmt', 'HomeTotalAmt')}
    create_body['Line'] = [{k: v for k, v in ln.items() if k not in ('Id', 'LineNum')} for ln in (keep + new)]
    created, error = quickbooks.create_object(token, 'Deposit', create_body)
    if error:
        return False, error
    ok, error = quickbooks.delete_object(token, 'Deposit', deposit['Id'], deposit['SyncToken'])
    if not ok:
        return False, (f'created the corrected deposit (QuickBooks Id {created.get("Id")}) but could not delete the '
                        f'original (Id {deposit["Id"]}): {error} - QuickBooks now has BOTH, please remove the '
                        f'original by hand')
    QBRecode.objects.create(payout_id=item['payout'].pk, source=item['source'], deposit_qb_id=created['Id'], deposit_date=item['date'], amount=item['amount'],
                            old_lines=uncat, new_lines=new, applied_by=user if getattr(user, 'pk', None) else None, automatic=automatic)
    return True, ''


MAX_PER_CLICK = 15   # two real QuickBooks calls each, one at a time - a big backlog sent in one click ran the
                      # request past the web server's own timeout and got killed mid-flight (a real production
                      # crash, not a graceful error), leaving staff unsure what had and hadn't actually gone
                      # through. A click now only ever sends a batch small enough to comfortably finish in time;
                      # the rest wait for the next click.


def apply_ready(token, result, user=None, automatic=False, limit=None):
    """Sends up to `limit` ready items (all of them when limit is None - used by the automatic nightly run, which
    has no request timeout to worry about). Returns (applied count, [(item, why not)], remaining count - how many
    more ready items are still waiting because of the limit)."""
    applied, failed, sent = 0, [], 0
    ready = [item for item in result['items'] if item['status'] == 'ready']
    for item in ready:
        if limit is not None and sent >= limit:
            break
        sent += 1
        ok, why = apply(token, item, user=user, automatic=automatic)
        if ok:
            applied += 1
        else:
            failed.append((item, why))
    return applied, failed, len(ready) - sent
