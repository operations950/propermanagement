"""Puts St Andrews GLEN into the books as placeholder data to play with.

What is real: the owners and what they owed (Yardi, Sep 30 2026), the trial balances (Yardi's cash and accrual books at Sep 30), the September bank statement
and the September ledger activity, the open bill and the three checks that had not cleared. What had to be worked out: the books begin Aug 31, so the
opening balances are Yardi's Sep 30 balances less what September did. September is replayed through the program's own rules (so every owner's ledger,
both books and the reconciliation are real working data), and the opening entry is whatever makes the books end at Yardi's Sep 30 figures to the cent.

Two small differences from Yardi's printout, on purpose: owners who paid ahead are shown as Prepaid Fees and owners who owe as Receivables, each
in full (Yardi netted $8,750 of the credits inside receivables), and checks 1338 to 1341 plus the interest are posted as the single entry Yardi made of
them, on an assumed account (Administrative Expenses).

Safe to run again: if the association already has entries, nothing is touched.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal as D

from django.db import transaction

from core.models import Contact, Property, Unit

from . import bankrec, charges, glen_data as G, ledger, owners, payables
from . import standard_chart as sc
from .models import (AssociationProfile, BankAccount, Bill, Charge, JournalEntry, JournalLine, Ownership, Payment, Receipt, RecurringBill, VendorTerms)

NAME = 'St Andrews Glen'
OPEN_DAY = date(2026, 8, 31)
SEP30 = date(2026, 9, 30)

SUNBIZ = {
    'legal_name': 'St. Andrews Glen Condominium Association, Inc.', 'state_doc_number': 'N35410', 'ein': '65-0167585', 'date_filed': date(1989, 11, 27), 'state_status': 'Active',
    'principal_address': '7581 Mackenzie Court, Lake Worth, FL 33467', 'mailing_address': '7581 Mackenzie Ct, Lake Worth, FL 33467', 'registered_agent': 'Proper Realty Co, 1045 E Atlantic Ave 309, Delray Beach, FL 33483',
    'annual_report_filed': date(2026, 4, 30), 'sunbiz_checked_on': date(2026, 10, 27),
    'sunbiz_url': 'https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?inquiryType=DocumentNumber&searchTerm=N35410',
    'sunbiz_officers': [{'name': 'Randall Humphrey', 'title': 'President'}, {'name': 'Janine Gosney', 'title': 'Secretary'}, {'name': 'Graham Holder', 'title': 'Treasurer'},
                        {'name': 'Ray Griffith', 'title': 'Vice President'}, {'name': 'John Lee', 'title': 'Director'}],
}
BOARD = [('Randall Humphrey', 'President', date(2026, 3, 16)), ('Janine Gosney', 'Secretary', None), ('Graham Holder', 'Treasurer', None), ('Ray Griffith', 'Vice President', None),
         ('John Lee', 'Director', None)]
BILLED = {'7601-113': 'Janine Gosney', '7621-212': 'Taylor Dawes', '7661-422': 'Kostas lkonomidis'}   # whom Yardi charged

# September, from the Yardi general ledger (cash book) for the operating account
RECEIPTS = [  # date, owner, unit, amount, method, reference
    (date(2026, 9, 3), 'Joanne Carner', '7641-314', '1750.00', 'check', '5106'), (date(2026, 9, 14), 'Erin Margiotta', '7681-521', '1360.00', 'check', ''),
    (date(2026, 9, 15), 'Rafael Salazar', '7681-513', '1750.00', 'card', ''), (date(2026, 9, 15), 'Rich Corporation', '7621-223', '1750.00', 'online', ''),
    (date(2026, 9, 16), 'Nelida Mejia', '7641-324', '1750.00', 'online', ''), (date(2026, 9, 25), 'Kostas lkonomidis', '7661-422', '1750.00', 'ach', ''),
    (date(2026, 9, 29), 'Kiem Luu', '7681-522', '1750.00', 'online', ''), (date(2026, 9, 30), 'Vincent Pinzone', '7621-221', '1750.00', 'online', ''),
]
# vendor, account, amount, bill date, paid how, check number, routine?
BILLS = [
    ('Competent Roof', '6210', '1400.00', date(2026, 9, 3), 'check', '110', False), ('Willie The Bee Man, Inc.', '6906', '795.00', date(2026, 9, 3), 'check', '111', False),
    ('Proper Realty Co', '6820', '1100.00', date(2026, 9, 14), 'check', '112', True), ('Competent Roof', '6210', '1450.00', date(2026, 9, 16), 'check', '113', False),
    ('Forever Green Landscape Services', '6893', '2435.00', date(2026, 9, 16), 'check', '114', True), ('WTC Backgrounds & Drug Testing, Inc', '6899', '49.98', date(2026, 9, 16), 'check', '115', False),
    ('ABA Pool Service', '6889', '300.00', SEP30, 'ach', '', True), ('FPL', '6410', '637.38', SEP30, 'ach', '', True), ('Hotwire Communications', '6440', '4016.69', SEP30, 'ach', '', True),
    ('Hotwire Communications', '6440', '61.95', SEP30, 'ach', '', True), ('Palm Beach County', '6430', '92.15', SEP30, 'ach', '', True),
    ('Plastridge Insurance', '6320', '7173.17', SEP30, 'ach', '', True), ('Cellgate', '6915', '274.77', SEP30, 'ach', '', True),
]
OPEN_BILL = ('Competent Roof', '6210', '1750.00', date(2026, 9, 17), '211')
OUTSTANDING_AT_OPEN = [('108', date(2026, 8, 7), 'Proper Realty Co', '2200.00'), ('109', date(2026, 8, 11), 'LAVINSCAPE', '6500.00')]

STATEMENT_BEGIN, STATEMENT_END = D('46444.68'), D('39797.92')
STATEMENT = [  # date, description, amount, check number
    (9, 2, 'WATER BILL PBC WATER UTILI 1688 ST ANDREWS GLEN CONDOM', '-92.15', ''), (9, 4, 'INTERNET PAYMENT PURCHASE HOTWIRE COMMUNIC 001', '-61.95', ''),
    (9, 4, 'Check 1338', '-477.00', '1338'), (9, 9, 'Check 110', '-1400.00', '110'),
    (9, 9, 'ELEC PYMT FPL DIRECT DEBIT PPDA SAINT ANDREWS GLEN CON', '-38.39', ''), (9, 9, 'ELEC PYMT FPL DIRECT DEBIT PPDA SAINT ANDREWS GLEN CON', '-40.71', ''),
    (9, 9, 'ELEC PYMT FPL DIRECT DEBIT PPDA SAINT ANDREWS GLEN CON', '-41.00', ''), (9, 9, 'ELEC PYMT FPL DIRECT DEBIT PPDA SAINT ANDREWS GLEN CON', '-41.95', ''),
    (9, 9, 'ELEC PYMT FPL DIRECT DEBIT PPDA SAINT ANDREWS GLEN CON', '-42.37', ''), (9, 9, 'ELEC PYMT FPL DIRECT DEBIT PPDA SAINT ANDREWS GLEN CON', '-432.96', ''),
    (9, 11, 'HOTWIRE CO HOTWIRE COMMUNIC 6077 ST ANDREWS GLEN', '-4016.69', ''), (9, 11, 'Check 1339', '-399.00', '1339'), (9, 15, 'PURCHASE The Plastridge A', '-7173.17', ''),
    (9, 15, 'Check 111', '-795.00', '111'), (9, 17, 'SALE ABA POOL SERVICE', '-300.00', ''), (9, 21, 'Check 114', '-2435.00', '114'), (9, 21, 'Check 1340', '-266.00', '1340'),
    (9, 22, 'ACH CORP DEBIT PURCHASE CELLGATE PROPER REALTY', '-274.77', ''), (9, 24, 'Check 113', '-1450.00', '113'), (9, 25, 'Check 115', '-49.98', '115'),
    (9, 28, 'Check 1341', '-429.00', '1341'),
    (9, 15, 'CONSOLIDATED COUPON PAYMENT 1', '1750.00', ''), (9, 16, 'CONSOL ELEC BILL PAY DEPOSIT 1', '1750.00', ''), (9, 16, 'DEPOSIT', '3110.00', ''),
    (9, 21, 'SAGLENTran YARDI CARD DEP SAGLEN-Proper Realty C', '1750.00', ''), (9, 29, 'Settlement PROPERREALTY-SAG Proper Realty Co', '1750.00', ''),
    (9, 29, 'CONSOL ELEC BILL PAY DEPOSIT 1', '1750.00', ''), (9, 30, 'CONSOL ELEC DEPOSIT 1', '1750.00', ''), (9, 30, 'INTEREST PAYMENT', '0.33', ''),
]

RECURRING = [  # vendor, description, account, amount (None = varies), next date, how paid, text the statement shows
    ('Forever Green Landscape Services', 'Landscaping', '6893', '2435.00', date(2026, 10, 16), 'check', ''),
    ('ABA Pool Service', 'Pool service', '6889', '300.00', date(2026, 10, 31), 'ach', 'ABA POOL'),
    ('FPL', 'Electricity', '6410', None, date(2026, 10, 31), 'ach', 'FPL'),
    ('Hotwire Communications', 'Internet and cable', '6440', None, date(2026, 10, 31), 'ach', 'HOTWIRE'),
    ('Palm Beach County', 'Water and sewer', '6430', None, date(2026, 10, 31), 'ach', 'WATER BILL'),
]


class _Rollback(Exception):
    pass


def _person(assoc, name):
    return owners.person_for_name(assoc, name)


def find_or_create_property():
    prop = Property.objects.filter(property_type=Property.Type.ASSOCIATION, name__iexact=NAME).first() or Property.objects.filter(name__iexact=NAME).first()
    if prop is None:
        prop = Property.objects.create(name=NAME, property_type=Property.Type.ASSOCIATION, street='7581 Mackenzie Court', city='Lake Worth', state='FL', zip_code='33467',
                                       address='7581 Mackenzie Court, Lake Worth, FL 33467', timezone='America/New_York')
    return prop


def _unit_label_for_owner(rows):
    return {name: unit for unit, name, *_ in rows}


def _opening_subledger(assoc):
    """One 'balance forward' charge for each owner who owed on Aug 31, and one paid-ahead receipt for each who was paid ahead. No entries: the opening entry covers them."""
    income = ledger.account(assoc, sc.INCOME_ASSESSMENT)
    for unit_label, name, opening, charges_, receipts, closing in G.AR_SEPT:
        opening = D(opening)
        if opening == 0:
            continue
        unit = Unit.objects.get(property=assoc, label=unit_label)
        person = _person(assoc, name)
        if opening > 0:
            Charge.objects.get_or_create(association=assoc, unit=unit, owner=person, kind=Charge.Kind.OPENING, due_date=OPEN_DAY,
                                         defaults={'description': 'Balance forward from Yardi', 'amount': opening, 'income_account': income})
        else:
            Receipt.objects.get_or_create(association=assoc, unit=unit, owner=person, date=OPEN_DAY, method=Receipt.Method.OTHER,
                                          defaults={'amount': -opening, 'memo': 'Paid ahead before the books moved'})


def _replay_september(assoc, bank):
    """September as the program's own rules would have posted it."""
    kostas = _person(assoc, 'Kostas lkonomidis')
    charges.post_charge(assoc, Unit.objects.get(property=assoc, label='7661-422'), kostas, Charge.Kind.ASSESSMENT, 'Quarterly assessment', date(2026, 9, 1), D('1750.00'),
                        income_account=ledger.account(assoc, sc.INCOME_ASSESSMENT))
    for day, name, unit_label, amount, method, ref in RECEIPTS:
        charges.receive_payment(assoc, Unit.objects.get(property=assoc, label=unit_label), _person(assoc, name), day, D(amount), method, reference=ref, bank_account=bank,
                                memo='From the September ledger')
    for vendor_name, code, amount, day, how, number, routine in BILLS:
        vendor = payables.vendor_for_name(vendor_name)
        kind = Bill.Kind.ROUTINE if routine else Bill.Kind.EXCEPTION
        bill = payables.create_bill(assoc, day, [(code, D(amount), '')], vendor=vendor, number='', due=day, kind=kind, source=Bill.Source.MANUAL, memo='From the September ledger')
        if bill.status != Bill.Status.APPROVED:
            payables.approve_bill(bill, board_by='Entered from Yardi', board_on=day)
        payables.pay_bills(assoc, bank, [(bill, bill.amount)], day, method=Payment.Method.CHECK if how == 'check' else Payment.Method.ACH, number=number, memo='From the September ledger')
    vendor_name, code, amount, day, number = OPEN_BILL
    bill = payables.create_bill(assoc, day, [(code, D(amount), 'Roof repair')], vendor=payables.vendor_for_name(vendor_name), number=number, due=day, kind=Bill.Kind.EXCEPTION,
                                source=Bill.Source.MANUAL, memo='Open at Sep 30, from Yardi')
    payables.approve_bill(bill, board_by='Entered from Yardi', board_on=day)
    ledger.post_entry(assoc, SEP30, [{'account': '6800', 'debit': D('1571.00'), 'memo': 'Checks 1338 to 1341'},
                                     {'account': sc.INTEREST_INCOME, 'credit': D('0.33'), 'memo': 'Interest'}, {'account': bank.account, 'credit': D('1570.67'), 'memo': 'Checks 1338 to 1341, net of interest'}],
                      memo='Checks 1338-1341 and interest (Yardi booked these as one entry, J-481; the account is assumed)', source=JournalEntry.Source.MANUAL)


def _book_sums(assoc):
    out = {'accrual': defaultdict(D), 'cash': defaultdict(D)}
    for l in JournalLine.objects.filter(entry__association=assoc).select_related('account', 'entry'):
        for book in out:
            if l.entry.basis in (book, 'both'):
                out[book][l.account.code] += l.debit - l.credit
    return out


def _yardi_tb(book):
    tb = {k: D(v) for k, v in (G.TB_ACCRUAL_SEP30 if book == 'accrual' else G.TB_CASH_SEP30).items()}
    if book == 'accrual':
        owing = sum((D(r[5]) for r in G.AR_SEPT if D(r[5]) > 0), D('0'))
        ahead = -sum((D(r[5]) for r in G.AR_SEPT if D(r[5]) < 0), D('0'))
        tb['1300'], tb['2210'] = owing, -ahead     # owed and paid-ahead each in full
    return tb


def _post_opening(assoc, bank, sums):
    for book in ('accrual', 'cash'):
        target = _yardi_tb(book)
        codes = set(target) | set(sums[book])
        lines = []
        for code in sorted(codes):
            amount = target.get(code, D('0')) - sums[book].get(code, D('0'))
            if amount == 0:
                continue
            if book == 'accrual' and code == sc.AR:
                per = [(c.unit, c.owner, c.amount) for c in Charge.objects.filter(association=assoc, kind=Charge.Kind.OPENING)]
                assert sum((a for _, _, a in per), D('0')) == amount, ('opening receivables do not tie', amount)
                lines += [{'account': code, 'debit': a, 'unit': u, 'owner': o, 'memo': 'Balance forward'} for u, o, a in per]
            elif book == 'accrual' and code == sc.PREPAID_FEES:
                per = [(r.unit, r.owner, r.amount) for r in Receipt.objects.filter(association=assoc, date=OPEN_DAY, entry__isnull=True)]
                assert -sum((a for _, _, a in per), D('0')) == amount, ('opening prepaid does not tie', amount)
                lines += [{'account': code, 'credit': a, 'unit': u, 'owner': o, 'memo': 'Paid ahead'} for u, o, a in per]
            else:
                lines.append({'account': code, 'debit': amount if amount > 0 else D('0'), 'credit': -amount if amount < 0 else D('0'), 'memo': 'Opening balance'})
        ledger.post_entry(assoc, OPEN_DAY, lines, memo='Opening balances, moved from Yardi', basis=book, source=JournalEntry.Source.OPENING, source_ref='yardi-sep30', allow_locked=True)


@transaction.atomic
def seed(log=print):
    assoc = find_or_create_property()
    if JournalEntry.objects.filter(association=assoc).exists():
        log(f'{assoc.name}: the books already have entries; leaving them alone.')
        return assoc
    profile = ledger.profile_for(assoc)
    for k, v in SUNBIZ.items():
        setattr(profile, k, v)
    profile.we_do_financials, profile.books_start, profile.auto_post, profile.next_check_number = True, OPEN_DAY, False, 116
    profile.save()
    ledger.ensure_chart(assoc)
    operating = BankAccount.objects.get_or_create(association=assoc, name='Operating', defaults={'bank_name': 'Truist', 'last4': '5100', 'kind': 'operating', 'account': ledger.account(assoc, sc.CASH_OPERATING)})[0]
    BankAccount.objects.get_or_create(association=assoc, name='Reserves', defaults={'bank_name': 'Live Oak Bank', 'last4': '0405', 'kind': 'reserve', 'account': ledger.account(assoc, sc.CASH_RESERVE)})

    rows = [{'unit': u, 'owner': name, 'absentee': away, 'since': date.fromisoformat(since)} for u, name, away, since in G.UNITS]
    owners.apply_import(assoc, rows, billed=BILLED)
    kramer = _person(assoc, 'Robert Kramer')
    unit_422 = Unit.objects.get(property=assoc, label='7661-422')
    if not Ownership.objects.filter(unit=unit_422, person=kramer).exists():
        Ownership.objects.create(unit=unit_422, person=kramer, end_date=date(2026, 8, 4))
    for name, position, start in BOARD:
        owners.add_board_term(assoc, _person(assoc, name), position, start=start)
    for unit_label, name, *_ in G.AR_SEPT:     # every name Yardi charged exists as a person
        _person(assoc, name)

    _opening_subledger(assoc)
    sums = None
    try:
        with transaction.atomic():
            _replay_september(assoc, operating)
            sums = _book_sums(assoc)
            raise _Rollback()
    except _Rollback:
        pass
    _post_opening(assoc, operating, sums)
    _replay_september(assoc, operating)

    for book, label in (('accrual', 'accrual'), ('cash', 'cash')):
        tb = ledger.trial_balance(assoc, date(2026, 9, 1), SEP30, book)
        want = _yardi_tb(book)
        got = {r['account'].code: r['ending'] for r in tb['rows'] if r['account'].code}
        bad = {c: (got.get(c, D('0')), want.get(c, D('0'))) for c in set(got) | set(want) if got.get(c, D('0')) != want.get(c, D('0'))}
        assert not bad, (label, bad)
    assert ledger.cash_balance(operating, OPEN_DAY) == D('37744.68'), 'opening operating cash is not what the Yardi ledger began with'

    for number, day, payee, amount in OUTSTANDING_AT_OPEN:
        Payment.objects.get_or_create(association=assoc, bank_account=operating, number=number, defaults={'date': day, 'payee_text': payee, 'amount': D(amount), 'method': 'check', 'opening': True,
                                                                                                          'memo': 'Written before the books moved; not cleared'})
    from .models import BankStatement, BankLine
    stmt = BankStatement.objects.create(bank_account=operating, period_end=SEP30, begin_balance=STATEMENT_BEGIN, end_balance=STATEMENT_END, read_by='seed')
    BankLine.objects.bulk_create([BankLine(statement=stmt, date=date(2026, m, d), description=desc, amount=D(amt), kind=bankrec.kind_for(desc, D(amt), num), check_number=num) for m, d, desc, amt, num in STATEMENT])
    bankrec.auto_match(operating, date(2026, 9, 1))

    for vendor_name, desc, code, amount, nxt, how, match in RECURRING:
        vendor = payables.vendor_for_name(vendor_name)
        RecurringBill.objects.get_or_create(association=assoc, vendor=vendor, description=desc, defaults={'account': ledger.account(assoc, code), 'amount': D(amount) if amount else None, 'frequency': 'monthly',
                                                                                                          'next_date': nxt, 'pay_method': how, 'statement_match': match})
        VendorTerms.objects.get_or_create(association=assoc, vendor=vendor, defaults={'default_account': ledger.account(assoc, code), 'pay_method': how})
    from .models import ChargeRule
    ChargeRule.objects.get_or_create(association=assoc, name='Regular assessment', defaults={'frequency': 'quarterly', 'first_due': date(2026, 10, 1), 'method': 'flat', 'amount': D('1750.00'),
                                                                                              'income_account': ledger.account(assoc, sc.INCOME_ASSESSMENT)})
    log(f'{assoc.name}: loaded {Unit.objects.filter(property=assoc).count()} units, {JournalEntry.objects.filter(association=assoc).count()} entries, the September statement.')
    return assoc
