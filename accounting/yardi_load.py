"""Brings an association's books over from Yardi as of September 30, 2026, from the figures in yardi_sep30_data.py.

What is loaded, for an association we keep the books for:
  * the units and their owners (a tenant row is not an owner), each unit's share, the board, the official record from the state;
  * the chart of accounts, and one opening entry per book (accrual and cash) that makes every account equal Yardi's trial balance at Sep 30;
  * each owner's balance (what they owe by age, and what they paid ahead) from Yardi's receivable report, under the receivable and prepaid accounts;
  * every unpaid bill (Yardi's payable report) as a bill ready to pay, under accounts payable;
  * the bank accounts, each tied to its cash account, with the bank's Sep 30 balance and the checks, deposits and other items Yardi was carrying, so the
    September reconciliation only has to be confirmed.
Whatever Yardi's owner and vendor detail does not explain stays in the books as its own labeled line (receivables, prepaid, payables), so the books equal Yardi's
trial balance to the cent and the leftover is plain to see.

Nothing here posts into September itself: the books begin on Sep 30 with these balances, and October is entered fresh.
"""
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal as D

from django.db import transaction

from core.models import Contact, Property, Unit

from . import bankrec, charges, ledger, owners, payables
from .contact_match import Matcher, emails_of
from . import standard_chart as sc
from .models import (ZERO, Application, AssociationProfile, BankAccount, BankOpeningItem, BankStatement, Bill, BoardTerm, Budget, Charge, ChargeRule, JournalEntry,
                     MonthLock, OwnerProfile, Ownership, Payment, PaymentBill, ReconAcceptance, ReconMatch, Reconciliation, Receipt, RecurringBill, ShareKind, UnitShare,
                     VendorTerms)
from .yardi_sep30_data import DATA

SEP30 = date(2026, 9, 30)
SEPT = date(2026, 9, 1)
# the age buckets of Yardi's receivable report, as a due date that lands an opening charge in the right bucket on Sep 30 (0-30, 31-60, 61-90, over 90 days)
BUCKET_DUE = [date(2026, 9, 15), date(2026, 8, 16), date(2026, 7, 17), date(2026, 6, 15)]
BUCKET_WORDS = ['0 to 30 days', '31 to 60 days', '61 to 90 days', 'over 90 days']
FIND = {'castlewood': r'castlewood', 'la_pensee': r'pensee', 'lakeside_8': r'lakeside.*\b8\b', 'lakeside_2': r'lakeside.*(\b2\b|garden)', 'linton': r'linton', 'glen': r'andrews\s+glen',
        'grand': r'andrews\s+grand'}
ORDER = ['castlewood', 'la_pensee', 'lakeside_8', 'lakeside_2', 'linton', 'glen', 'grand']


class LoadError(Exception):
    pass


def norm(t):
    return re.sub(r'[^a-z]', '', (t or '').lower())


def money(v):
    return ledger.money(D(str(v)))


def kind_for_code(code):
    n = int(code)
    return 'asset' if n < 2000 else 'liability' if n < 3000 else 'equity' if n < 4000 else 'income' if n < 5000 else 'expense'


# ---------------------------------------------------------------- finding the association

def find_property(key):
    cfg = DATA[key]
    for p in Property.objects.filter(property_type=Property.Type.ASSOCIATION).order_by('id'):
        if re.search(FIND[key], p.name.lower().replace('#', ' '), re.I):
            return p, False
    prop = Property.objects.create(name=cfg['name'], property_type=Property.Type.ASSOCIATION, street=cfg['street'], city=cfg['city'], state=cfg['state'], zip_code=cfg['zip'],
                                   address=', '.join(x for x in (cfg['street'], cfg['city'], f"{cfg['state']} {cfg['zip']}") if x), timezone='America/New_York')
    return prop, True


def has_books(prop):
    return JournalEntry.objects.filter(association=prop).exists()


# ---------------------------------------------------------------- starting over

WIPE_ORDER = [('reconciliation acceptances', lambda p: ReconAcceptance.objects.filter(bank_account__association=p)), ('reconciliation matches', lambda p: ReconMatch.objects.filter(bank_account__association=p)),
              ('reconciliations', lambda p: Reconciliation.objects.filter(bank_account__association=p)), ('bank statements', lambda p: BankStatement.objects.filter(bank_account__association=p)),
              ('opening bank items', lambda p: BankOpeningItem.objects.filter(bank_account__association=p)),
              ('payments applied to charges', lambda p: Application.objects.filter(charge__association=p)), ('owner payments', lambda p: Receipt.objects.filter(association=p)),
              ('owner charges', lambda p: Charge.objects.filter(association=p)), ('recurring charges', lambda p: ChargeRule.objects.filter(association=p)),
              ('bill payments', lambda p: PaymentBill.objects.filter(payment__association=p)), ('payments to vendors', lambda p: Payment.objects.filter(association=p)),
              ('bills', lambda p: Bill.objects.filter(association=p)), ('recurring bills', lambda p: RecurringBill.objects.filter(association=p)),
              ('vendor terms', lambda p: VendorTerms.objects.filter(association=p)), ('bank accounts', lambda p: BankAccount.objects.filter(association=p)),
              ('journal entries', lambda p: JournalEntry.objects.filter(association=p)), ('closed months', lambda p: MonthLock.objects.filter(association=p)),
              ('ownerships', lambda p: Ownership.objects.filter(unit__property=p)), ('unit shares', lambda p: UnitShare.objects.filter(unit__property=p)),
              ('board terms', lambda p: BoardTerm.objects.filter(association=p))]


def wipe_counts(prop):
    return [(label, qs(prop).count()) for label, qs in WIPE_ORDER]


@transaction.atomic
def wipe(prop):
    """Take out an association's books and everything hanging off them. The property page, its units, the people, uploaded documents, the budget, the settings and the
    official record stay."""
    done = []
    for label, qs in WIPE_ORDER:
        rows = qs(prop)
        n = rows.count()
        if n:
            rows.delete()
        done.append((label, n))
    profile = ledger.profile_for(prop)
    profile.books_start = None
    profile.save(update_fields=['books_start'])
    return done


# ---------------------------------------------------------------- shares

def _set_shares(prop, yardi):
    """Yardi's shares add to a little over 100; scale them so each unit keeps its proportion and the total is exactly 100 (millionths of a percent, largest remainders)."""
    kind = owners.default_share_kind(prop)
    units = {u.label: u for u in owners.active_units(prop)}
    total = sum((D(v) for v in yardi.values()), ZERO)
    whole = 100 * 1_000_000
    raw = {l: D(v) * whole / total for l, v in yardi.items() if l in units}
    floors = {l: int(x) for l, x in raw.items()}
    spare = whole - sum(floors.values())
    for l in sorted(raw, key=lambda l: (-(raw[l] - floors[l]), l))[:spare]:
        floors[l] += 1
    for l, m in floors.items():
        UnitShare.objects.update_or_create(unit=units[l], kind=kind, defaults={'percent': D(m) / D(1_000_000)})


# ---------------------------------------------------------------- owners' balances

def _owner_index(cfg):
    """normalized name -> (display name, [unit labels]) over the owners the units list has (tenants are not owners)."""
    idx = {}
    for u in cfg['units']:
        for o in u['owners']:
            if o['type'] == 'tenant':
                continue
            name, labels = idx.setdefault(o['key'], (o['name'], []))
            if u['label'] not in labels:
                labels.append(u['label'])
    return idx


def _sort_label(l):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', l) if x]


def _match_receivables(cfg):
    """[(unit label, owner name, line)] for every line on Yardi's receivable report we can place on a unit, and the lines we cannot (former owners)."""
    if cfg.get('ar_direct'):
        return [(a['unit'], a['name'], a) for a in cfg['ar_direct']], []
    idx = {k: (n, sorted(ls, key=_sort_label)) for k, (n, ls) in _owner_index(cfg).items()}
    used, placed, lost = defaultdict(int), [], []
    for line in cfg['ar']:
        k = norm(line['name'])
        if k not in idx:
            lost.append(line)
            continue
        name, labels = idx[k]
        label = labels[min(used[k], len(labels) - 1)]
        used[k] += 1
        placed.append((label, name, line))
    return placed, lost


# ---------------------------------------------------------------- the load

def _resolve_people(prop, cfg, matcher, report):
    """For every owner Yardi lists, the Contact they are: one we already have (the same email, else the same phone, else the same or a close name) or a new one.
    {(unit label, lower-case name): contact, ('', lower-case name): contact} for the rest of the load."""
    people, created, links, undecided, reasons = {}, set(), [], [], defaultdict(int)
    for u in cfg['units']:
        taken = set()
        for o in u['owners']:
            if o['type'] == 'tenant':
                continue
            contact, reason, candidates = matcher.find(o, taken)
            if contact is None:
                mine = emails_of(o.get('email'))
                contact = owners.create_owner_contact(prop, o['name'], source=Contact.Source.YARDI)
                contact.email, contact.phone = (mine[0] if mine else '')[:254], (o.get('phone') or '')[:30]
                contact.save(update_fields=['email', 'phone'])
                created.add(contact.pk)
                matcher.add(contact)          # the same person on another unit is then this contact, not a second one
                if candidates:
                    undecided.append({'yardi': o['name'], 'unit': u['label'], 'made': contact.pk,
                                      'candidates': [{'name': c.name, 'id': c.pk, 'email': c.email, 'phone': c.phone, 'why': why} for c, why in candidates]})
            else:
                owners.attach_owner(prop, contact)
                if contact.pk not in created:
                    reasons[reason] += 1
                    links.append({'yardi': o['name'], 'existing': contact.name, 'id': contact.pk, 'reason': reason, 'unit': u['label']})
            taken.add(contact.pk)
            people[(u['label'], o['name'].lower())] = contact
            people.setdefault(('', o['name'].lower()), contact)
    report.update({'linked': links, 'link_reasons': dict(reasons), 'new_contacts': len(created), 'undecided': undecided})
    return people


def _people_and_units(prop, cfg, placed, people, log):
    rows, billed = [], dict(cfg.get('billed') or {})
    for label, name, line in placed:
        billed.setdefault(label, name)
    for u in cfg['units']:
        for o in u['owners']:
            if o['type'] == 'tenant':
                continue
            rows.append({'unit': u['label'], 'owner': o['name'], 'person': people[(u['label'], o['name'].lower())], 'absentee': o['type'] == 'absentee',
                         'since': date.fromisoformat(o['since']) if o.get('since') else None})
    made = owners.apply_import(prop, rows, billed=billed, equal_shares=not any(u.get('share') for u in cfg['units']))
    yardi_shares = {u['label']: u['share'] for u in cfg['units'] if u.get('share')}
    if yardi_shares:
        _set_shares(prop, yardi_shares)
    # what the person-by-person detail adds: how to reach them
    for u in cfg['units']:
        for o in u['owners']:
            if o['type'] == 'tenant':
                continue
            person = people[(u['label'], o['name'].lower())]
            changed = []
            if o['email'] and not person.email:
                person.email = o['email'][:254]
                changed.append('email')
            if o['phone'] and not person.phone:
                person.phone = o['phone'][:30]
                changed.append('phone')
            if changed:
                person.save(update_fields=changed)
            prof, _ = OwnerProfile.objects.get_or_create(contact=person)
            if o['mailing'] and not prof.mailing_address:
                prof.mailing_address = o['mailing']
            if o.get('phone_alt') and not prof.phone_alt:
                prof.phone_alt = o['phone_alt'][:30]
            prof.save()
    return made


def _board(prop, cfg, people):
    if cfg is DATA['glen']:
        from .seed_glen import BOARD
        terms = BOARD
    else:
        terms = [(o['name'], o['board'], None) for u in cfg['units'] for o in u['owners'] if o['board']]
    for name, position, start in terms:
        person = people.get(('', name.lower())) or owners.person_for_name(prop, name)
        if not BoardTerm.objects.filter(association=prop, person=person, position=position, end_date__isnull=True).exists():
            owners.add_board_term(prop, person, position, start=start)


def _profile(prop, cfg):
    sb = cfg['sunbiz']
    p = ledger.profile_for(prop)
    p.legal_name, p.state_doc_number, p.ein, p.state_status = sb['legal_name'], sb['doc'], sb['ein'], sb['status']
    p.date_filed, p.annual_report_filed = date.fromisoformat(sb['filed']), date.fromisoformat(sb['annual'])
    p.principal_address, p.mailing_address, p.registered_agent = sb['principal'], sb['mailing'], sb['agent']
    p.sunbiz_url = f"https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?inquiryType=DocumentNumber&searchTerm={sb['doc']}"
    p.sunbiz_officers = [{'name': n, 'title': t} for n, t in sb['officers']]
    p.sunbiz_checked_on, p.sunbiz_checked_by = date(2026, 10, 29), 'hand'
    p.sunbiz_alerts = [f'The registered agent is {sb["agent"].split(",")[0]}, not Proper Realty Co.'] if 'proper realty' not in sb['agent'].lower() else []
    p.we_do_financials = bool(cfg['financials'])
    if cfg['financials']:
        p.books_start, p.auto_post = SEP30, False
        if cfg.get('next_check'):
            p.next_check_number = cfg['next_check']
    p.save()
    for f, v in (('street', cfg['street']), ('city', cfg['city']), ('state', cfg['state']), ('zip_code', cfg['zip'])):
        if v and not getattr(prop, f):
            setattr(prop, f, v)
    if not prop.address and cfg['street']:
        prop.address = f"{cfg['street']}, {cfg['city']}, {cfg['state']} {cfg['zip']}"
    prop.save()
    return p


def _subledger(prop, placed, people, report):
    """One 'balance forward' charge per age bucket an owner owes in, and one paid-ahead receipt for each owner who paid ahead. No entries: the opening entry covers them."""
    income = ledger.account(prop, sc.INCOME_ASSESSMENT)
    owed = prepaid = ZERO
    for label, name, line in placed:
        unit = Unit.objects.get(property=prop, label=label)
        person = people.get((label, name.lower())) or owners.person_for_name(prop, name)
        for i, amount in enumerate(line['buckets']):
            amount = money(amount)
            if amount > 0:
                Charge.objects.create(association=prop, unit=unit, owner=person, kind=Charge.Kind.OPENING, description=f'Balance forward from Yardi, {BUCKET_WORDS[i]}', due_date=BUCKET_DUE[i],
                                      amount=amount, income_account=income)
                owed += amount
        pre = money(line['prepaid'])
        if pre > 0:
            Receipt.objects.create(association=prop, unit=unit, owner=person, date=SEP30, amount=pre, method=Receipt.Method.OTHER, memo='Paid ahead before the books moved')
            prepaid += pre
    report['owner_owed'], report['owner_prepaid'] = owed, prepaid
    return owed, prepaid


def _bills(prop, cfg):
    made = []
    for vendor_name, number, day, due, code, amount, template in cfg['ap']:
        made.append(payables.create_bill(prop, date.fromisoformat(day), [(code, D(amount), '')], vendor=payables.vendor_for_name(vendor_name), number=number, due=date.fromisoformat(due),
                                         kind=Bill.Kind.ROUTINE if template else Bill.Kind.EXCEPTION, memo='Open at Sep 30, from Yardi', opening=True))
    return made


def _opening_entries(prop, cfg, bills, report):
    ar_acct, pre_acct, ap_acct = ledger.account(prop, sc.AR), ledger.account(prop, sc.PREPAID_FEES), ledger.account(prop, sc.AP)
    owner_charges = list(Charge.objects.filter(association=prop, kind=Charge.Kind.OPENING).select_related('unit', 'owner'))
    owner_receipts = list(Receipt.objects.filter(association=prop, date=SEP30, entry__isnull=True).select_related('unit', 'owner'))
    for book in ('accrual', 'cash'):
        lines = []
        for code in sorted(cfg['tb'][book]):
            name, raw = cfg['tb'][book][code]
            amount = money(raw)
            if amount == 0:
                continue
            if book == 'accrual' and code == sc.AR:
                for c in owner_charges:
                    lines.append({'account': ar_acct, 'debit': c.amount, 'unit': c.unit, 'owner': c.owner, 'memo': 'Balance forward'})
                gap = amount - sum((c.amount for c in owner_charges), ZERO)
                report['ar_difference'] = gap
                if gap:
                    lines.append({'account': ar_acct, 'debit': gap if gap > 0 else ZERO, 'credit': -gap if gap < 0 else ZERO, 'memo': 'Difference to Yardi: owed, not on an owner ledger'})
            elif book == 'accrual' and code == sc.PREPAID_FEES:
                for r in owner_receipts:
                    lines.append({'account': pre_acct, 'credit': r.amount, 'unit': r.unit, 'owner': r.owner, 'memo': 'Paid ahead'})
                gap = -amount - sum((r.amount for r in owner_receipts), ZERO)      # the credit Yardi holds, less the credits on owners' ledgers
                report['prepaid_difference'] = gap
                if gap:
                    lines.append({'account': pre_acct, 'credit': gap if gap > 0 else ZERO, 'debit': -gap if gap < 0 else ZERO, 'memo': 'Difference to Yardi: paid ahead, not on an owner ledger'})
            elif book == 'accrual' and code == sc.AP:
                for b in bills:
                    lines.append({'account': ap_acct, 'credit': b.amount, 'vendor': b.vendor, 'memo': f'{b.payee} {b.number}'.strip()})
                gap = -amount - sum((b.amount for b in bills), ZERO)
                report['ap_not_itemized'] = gap
                if gap:
                    lines.append({'account': ap_acct, 'credit': gap if gap > 0 else ZERO, 'debit': -gap if gap < 0 else ZERO, 'memo': 'Difference to Yardi: owed, no invoice detail'})
            else:
                lines.append({'account': code, 'debit': amount if amount > 0 else ZERO, 'credit': -amount if amount < 0 else ZERO, 'memo': 'Opening balance'})
        ledger.post_entry(prop, SEP30, lines, memo='Opening balances, moved from Yardi', basis=book, source=JournalEntry.Source.OPENING, source_ref='yardi-sep30', allow_locked=True)


def _banks(prop, cfg, report):
    out = []
    for name, bank_name, last4, kind, code, statement, checks, others in cfg['banks']:
        bank = BankAccount.objects.create(association=prop, name=name, bank_name=bank_name, last4=last4, kind=kind, account=ledger.account(prop, code))
        row = {'name': name, 'code': code, 'book': ledger.cash_balance(bank, SEP30), 'statement': None, 'status': 'no statement', 'difference': None}
        if statement is not None:
            BankStatement.objects.create(bank_account=bank, period_end=SEP30, end_balance=money(statement), read_by='yardi')
            for number, day, payee, amount in checks:
                p = Payment.objects.create(association=prop, bank_account=bank, number=number, date=date.fromisoformat(day), payee_text=payee, amount=money(amount), method=Payment.Method.CHECK,
                                           opening=True, memo='Written before the books moved; not cleared at Sep 30')
                ReconAcceptance.objects.create(bank_account=bank, month=SEPT, payment=p, note='Carried over from the Yardi reconciliation at Sep 30')
            for day, desc, amount in others:
                item = BankOpeningItem.objects.create(bank_account=bank, date=date.fromisoformat(day), description=desc, amount=money(amount), memo='In the books at Sep 30, not yet at the bank')
                ReconAcceptance.objects.create(bank_account=bank, month=SEPT, opening_item=item, note='Carried over from the Yardi reconciliation at Sep 30')
            s = bankrec.summary(bank, SEPT)
            row.update({'statement': money(statement), 'status': s['status'], 'difference': s['difference']})
        out.append(row)
    report['banks'] = out


def _glen_extras(prop):
    from .seed_glen import RECURRING
    ChargeRule.objects.get_or_create(association=prop, name='Regular assessment', defaults={'frequency': 'quarterly', 'first_due': date(2026, 10, 1), 'method': 'flat', 'amount': D('1750.00'),
                                                                                           'income_account': ledger.account(prop, sc.INCOME_ASSESSMENT)})
    for vendor_name, desc, code, amount, nxt, how, match in RECURRING:
        vendor = payables.vendor_for_name(vendor_name)
        RecurringBill.objects.get_or_create(association=prop, vendor=vendor, description=desc, defaults={'account': ledger.account(prop, code), 'amount': D(amount) if amount else None,
                                                                                                          'frequency': 'monthly', 'next_date': nxt, 'pay_method': how, 'statement_match': match})
        VendorTerms.objects.get_or_create(association=prop, vendor=vendor, defaults={'default_account': ledger.account(prop, code), 'pay_method': how})


def _verify(prop, cfg, report):
    """The books must equal Yardi's trial balances, both books, every account, to the cent; every cash account must equal its Yardi balance."""
    for book in ('accrual', 'cash'):
        want = {code: money(raw) for code, (name, raw) in cfg['tb'][book].items()}
        got = {r['account'].code: r['ending'] for r in ledger.trial_balance(prop, date(2026, 1, 1), SEP30, book)['rows'] if r['account'].code}
        bad = {c: (got.get(c, ZERO), want.get(c, ZERO)) for c in set(got) | set(want) if got.get(c, ZERO) != want.get(c, ZERO)}
        if bad:
            raise LoadError(f'{prop.name}: the {book} book does not equal Yardi at Sep 30: {bad}')
    for row in report['banks']:
        want = money(cfg['tb']['accrual'][row['code']][1])
        if row['book'] != want:
            raise LoadError(f'{prop.name}: {row["name"]} holds {row["book"]} but Yardi has {want}')
        if row['statement'] is not None and (row['difference'] != 0 or row['status'] != 'ready'):
            raise LoadError(f'{prop.name}: {row["name"]} does not reconcile ({row["status"]}, difference {row["difference"]})')


@transaction.atomic
def load(key, log=print, overrides=None, strict=False):
    """Load one association. Refuses if its books already have entries (wipe them first). Returns a report of what was loaded and what is left over.
    `overrides` is {Yardi name: contact id} for owners a person has said are an existing contact. With `strict`, an owner who could be more than one existing contact
    stops the load (nothing is saved) until a person has decided."""
    cfg = DATA[key]
    prop, created = find_property(key)
    if cfg['financials'] and has_books(prop):
        raise LoadError(f'{prop.name} already has books. Wipe them first (--wipe-first), or leave them as they are.')
    report = {'key': key, 'name': prop.name, 'created_property': created, 'financials': cfg['financials'], 'ar_difference': ZERO, 'prepaid_difference': ZERO, 'ap_not_itemized': ZERO}
    existing_units = set(Unit.objects.filter(property=prop, is_active=True).values_list('label', flat=True))
    package_units = {u['label'] for u in cfg['units']}
    report['other_units_already_there'] = sorted(existing_units - package_units, key=_sort_label)
    if cfg['financials']:
        ledger.ensure_chart(prop)
        have = set(prop.accounts.values_list('code', flat=True))
        for book in cfg['tb'].values():
            for code, (name, raw) in book.items():
                if code not in have:
                    from .models import Account
                    Account.objects.create(association=prop, code=code, name=name, kind=kind_for_code(code))
                    have.add(code)
    _profile(prop, cfg)
    placed, lost = ([], []) if not cfg['financials'] else _match_receivables(cfg)
    people = _resolve_people(prop, cfg, Matcher(prop, overrides), report)
    if strict and report['undecided']:
        names = '; '.join(f"{u['yardi']} (unit {u['unit']})" for u in report['undecided'][:8])
        raise LoadError(f"{prop.name}: {len(report['undecided'])} owner(s) could be a contact you already have and need a decision: {names}. "
                        'Say which are the same person with --link "Yardi name=contact id", or load them as new people with --new-for-undecided.')
    made = _people_and_units(prop, cfg, placed, people, log)
    _board(prop, cfg, people)
    report.update({'units': Unit.objects.filter(property=prop, is_active=True).count(), 'owners_made': made['owners'], 'ownerships_made': made['ownerships'],
                   'tenants_not_loaded': sum(1 for u in cfg['units'] for o in u['owners'] if o['type'] == 'tenant'), 'left_out': cfg['left_out']})
    if not cfg['financials']:
        return report
    _subledger(prop, placed, people, report)
    bills = _bills(prop, cfg)
    _opening_entries(prop, cfg, bills, report)
    _banks(prop, cfg, report)
    if key == 'glen':
        _glen_extras(prop)
    report['former_owners'] = [{'name': l['name'], 'status': l['status'], 'owed': money(l['owed']), 'prepaid': money(l['prepaid'])} for l in lost]
    report['bills'] = len(bills)
    _verify(prop, cfg, report)
    return report


def describe(report):
    """The report as lines of text for the person running the command."""
    out = [f"{report['name']}: {report['units']} units, {report.get('tenants_not_loaded', 0)} tenant rows not loaded"
           + (f", left out as placeholders: {', '.join(report['left_out'])}" if report.get('left_out') else '')]
    if 'linked' in report:
        r = report['link_reasons']
        parts = ', '.join(f'{n} {w}' for w, n in sorted(r.items(), key=lambda kv: -kv[1]))
        out.append(f"  owners: {len(report['linked'])} matched to contacts you already have" + (f' ({parts})' if parts else '') + f", {report['new_contacts']} new, {len(report['undecided'])} to decide")
        shown = [l for l in report['linked'] if l['reason'] != 'same name' or l['yardi'].lower() != l['existing'].lower()]
        for l in shown[:60]:
            out.append(f"    {l['reason']}: {l['yardi']} (unit {l['unit']}) = {l['existing']} [contact {l['id']}]")
        if len(shown) > 60:
            out.append(f'    ... and {len(shown) - 60} more')
        for u in report['undecided']:
            cands = '; '.join(f"{c['name']} [contact {c['id']}] {c['email'] or c['phone'] or 'no email or phone'} ({c['why']})" for c in u['candidates'])
            out.append(f"    TO DECIDE: {u['yardi']} (unit {u['unit']}) could be: {cands}")
    if report.get('other_units_already_there'):
        out.append(f"  units already in the app that Yardi's list does not have (left alone): {', '.join(report['other_units_already_there'])}")
    if not report['financials']:
        out.append('  books not kept for this association: units, owners and the official record only')
        return out
    out.append(f"  owners owe {report['owner_owed']:,}, paid ahead {report['owner_prepaid']:,}; not on an owner's ledger: receivables {report['ar_difference']:,}, paid ahead {report['prepaid_difference']:,}; "
               f"unpaid bills {report['bills']}, payables with no invoice detail {report['ap_not_itemized']:,}")
    for b in report['banks']:
        out.append(f"  {b['code']} {b['name']}: books {b['book']:,}" + (f", statement {b['statement']:,}, {b['status']}, difference {b['difference']:,}" if b['statement'] is not None else ', no statement yet (Yardi sent no reconciliation)'))
    for f in report.get('former_owners', []):
        out.append(f"  FORMER OWNER with a balance, unit unknown: {f['name']} ({f['status']}): owes {f['owed']:,}, paid ahead {f['prepaid']:,}")
    out.append('  the books equal Yardi\'s trial balance at Sep 30, both books, every account.')
    return out
