"""The weekly check run for every association at once, and the envelopes that go with it (the Payables module's last two steps).

Approved payables (a Bill in status "Ready to pay") wait here. `plan` groups them by the bank account that pays them, puts every vendor's invoices in one bank account on ONE check,
orders the checks by association and then due date, and says what the check numbers will be. `print_run` is the moment of truth: it makes the payments (payables.pay_bills, so the
books are posted exactly as they are everywhere else), the bills become PAID, and `checks_pdf` draws one page per check on each association's own check stock layout. A reprint draws the
same pages again and posts nothing; a void (payables.void_payment) puts the bills back to "Ready to pay". `envelopes_pdf` makes one plain #10 envelope per check with the vendor's address
printed on it, from Proper Realty.

Nothing here adds a table: a "run" is the checks printed in the same minute by the same person (Payment.printed_at), and nobody has to name it.
"""
import io
from collections import OrderedDict, defaultdict
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from legalforms import config as lf_config

from . import budget_project as bp
from . import checkface, checkprint, ledger, payables
from .models import ZERO, BankAccount, Bill, Payment, VendorTerms

FONT, FONT_SIZE = 'Helvetica', 11
ASTERISKS = '*' * 33


# ---------------------------------------------------------------- what is ready, and where it is paid from

def uses_standard_face(profile):
    """True unless the association has a check layout learned from its own pre-printed stock: then that layout is kept; everyone else prints the built-in face (checkface.py)."""
    return not (profile.check_layout or {}).get('learned')


def _bill_has_bank_field():
    return any(f.name == 'bank_account' for f in Bill._meta.get_fields())


def bank_for_bill(bill, active_operating=None):
    """The bank account a payable is paid from: the one chosen on it (Bill.bank_account, set when the payable is made), otherwise the association's ONLY active operating bank
    account. None when there is no such account or when there are several and none was chosen: the payable then waits in the "can't print yet" list. This is the only place that
    knows where a payable's bank account comes from."""
    chosen = getattr(bill, 'bank_account', None)
    if chosen is not None:
        return chosen
    banks = active_operating if active_operating is not None else list(BankAccount.objects.filter(association_id=bill.association_id, is_active=True, kind=BankAccount.Kind.OPERATING))
    return banks[0] if len(banks) == 1 else None


def vendor_address_lines(vendor):
    """The vendor's mailing address as lines for an envelope: the address kept on the vendor's profile (what the Payables page saves when a vendor is made from an invoice), else a mailing address
    on the contact or its owner profile. Empty when there is none."""
    if vendor is None:
        return []
    from .payables_hub import vendor_address_lines as profile_lines
    lines = profile_lines(vendor)
    if lines:
        return lines
    for text in (getattr(vendor, 'mailing_address', ''), getattr(getattr(vendor, 'owner_profile', None), 'mailing_address', '')):
        lines = bp.address_lines(text or '')
        if lines:
            return lines
    return []


def ready_bills():
    qs = Bill.objects.filter(status=Bill.Status.APPROVED).select_related('association', 'vendor').prefetch_related('payments', 'lines')
    if _bill_has_bank_field():
        qs = qs.select_related('bank_account')
    return [b for b in qs.order_by('association__name', 'due_date', 'date', 'pk') if b.balance > 0]


def check_memo(bills):
    """What the memo line says on a check that pays these invoices: their numbers, or their dates when they have none."""
    numbers = [b.number.strip() for b in bills if b.number and b.number.strip()]
    if numbers and len(numbers) == len(bills):
        return 'Inv ' + ', '.join(numbers)
    if numbers:
        return 'Inv ' + ', '.join(numbers) + f' and {len(bills) - len(numbers)} more'
    return 'Invoices dated ' + ', '.join(f'{b.date:%b} {b.date.day}' for b in bills)


def _last_number(bank):
    nums = [int(n) for n in Payment.objects.filter(bank_account=bank, method=Payment.Method.CHECK, opening=False).exclude(status=Payment.Status.VOID).values_list('number', flat=True) if n and n.isdigit()]
    return max(nums) if nums else None


def plan(selected=None):
    """The run as it stands. `selected` is the set of bill ids that are ticked (None: all). Returns
    {'groups': [{association, bank, checks, total, warnings, start, ...}], 'blocked': [{bill, why, url, label}], 'n_checks', 'total', 'n_bills'}.
    Each check: {payee, vendor, bills, due, total (of the ticked bills), number (None when nothing on it is ticked)}. Groups and checks are in printing order."""
    bills = ready_bills()
    operating = defaultdict(list)
    for b in BankAccount.objects.filter(is_active=True, kind=BankAccount.Kind.OPERATING):
        operating[b.association_id].append(b)
    active_count = defaultdict(int)
    for assoc_id in BankAccount.objects.filter(is_active=True).values_list('association_id', flat=True):
        active_count[assoc_id] += 1
    not_by_check = set(VendorTerms.objects.exclude(pay_method=VendorTerms.Pay.CHECK).values_list('association_id', 'vendor_id'))
    groups, blocked, profiles, autopay = OrderedDict(), [], {}, []
    for b in bills:
        b.ticked = selected is None or b.pk in selected
        if b.vendor_id and (b.association_id, b.vendor_id) in not_by_check:             # taken from the bank by the vendor: no check is written for it
            autopay.append(b)
            continue
        bank = bank_for_bill(b, operating[b.association_id])
        settings_url = reverse('accounting:settings', args=[b.association_id]) + '#banks'
        if bank is None:
            several = len(operating[b.association_id]) > 1
            why = (f'{b.association.name} writes checks from several bank accounts and none is chosen for this payable.' if several
                   else f'{b.association.name} has no bank account set up to write checks from.')
            blocked.append({'bill': b, 'why': why, 'url': settings_url, 'label': 'Set up the bank account'})
            continue
        if not bank.is_active:
            blocked.append({'bill': b, 'why': f'The bank account {bank.label} is switched off.', 'url': settings_url, 'label': 'Open the bank accounts'})
            continue
        if bank.association_id not in profiles:                       # the checkbook's owner: for a trust account that is Proper Realty, not the property the bill is for
            profiles[bank.association_id] = ledger.profile_for(bank.association)
        gaps = bank.print_gaps() if uses_standard_face(profiles[bank.association_id]) else []
        if gaps:
            blocked.append({'bill': b, 'why': f'The bank account {bank.label} is not ready to print checks. Missing: {", ".join(gaps)}.', 'url': settings_url, 'label': 'Set up the bank account', 'gaps': gaps})
            continue
        g = groups.setdefault((bank.pk, b.association_id), {'association': b.association, 'bank': bank, 'bills': []})
        g['bills'].append(b)
    ordered = sorted(groups.values(), key=lambda g: (g['association'].name.lower(), g['bank'].kind, g['bank'].name.lower()))
    start, counters = {}, {}
    for g in ordered:
        aid = g['association_id'] = g['bank'].association_id          # the number sequence belongs to the checkbook's owner
        profile = ledger.profile_for(g['bank'].association)
        start.setdefault(aid, profile.next_check_number)
        counters.setdefault(aid, profile.next_check_number)
        g['profile'] = profile
        g['standard_face'] = uses_standard_face(profile)
        g['start'] = start[aid]
        by_payee = OrderedDict()
        for b in g['bills']:
            key = ('v', b.vendor_id) if b.vendor_id else ('t', (b.vendor_text or '').strip().lower() or f'bill{b.pk}')
            by_payee.setdefault(key, {'payee': b.payee, 'vendor': b.vendor, 'bills': []})['bills'].append(b)
        checks = list(by_payee.values())
        for c in checks:
            c['bills'].sort(key=lambda x: (x.due_date or x.date, x.date, x.pk))
            c['due'] = c['bills'][0].due_date or c['bills'][0].date
            c['total'] = sum((x.balance for x in c['bills'] if x.ticked), ZERO)
        checks.sort(key=lambda c: (c['due'], c['payee'].lower()))
        for c in checks:
            if any(x.ticked for x in c['bills']):
                c['number'] = counters[aid]
                counters[aid] += 1
            else:
                c['number'] = None
        g['checks'] = checks
        g['total'] = sum((c['total'] for c in checks), ZERO)
        g['n_checks'] = sum(1 for c in checks if c['number'] is not None)
        warnings = []
        last = _last_number(g['bank'])
        if last is not None and profile.next_check_number != last + 1:
            warnings.append({'text': (f'The next check number is {profile.next_check_number}, but the last check written from {g["bank"].label} was {last}'
                                      + (' (a gap)' if profile.next_check_number > last + 1 else ' (that number would be used twice)') + '.'), 'url': reverse('accounting:check_setup', args=[aid]), 'label': 'Check the numbering'})
        if active_count[aid] > 1:
            warnings.append({'text': f'{g["bank"].association.name} has more than one bank account: its checks share one number sequence.', 'url': '', 'label': ''})
        g['warnings'] = warnings
    n_checks = sum(g['n_checks'] for g in ordered)
    return {'groups': ordered, 'blocked': blocked, 'autopay': autopay, 'n_checks': n_checks, 'total': sum((g['total'] for g in ordered), ZERO), 'n_bills': sum(1 for g in ordered for c in g['checks'] for x in c['bills'])}


def mark_paid(bill, day, user=None):
    """An approved payable that the bank paid by itself (an automatic payment): record it as paid by ACH on `day` from the bank account it is paid from."""
    operating = list(BankAccount.objects.filter(association_id=bill.association_id, is_active=True, kind=BankAccount.Kind.OPERATING))
    bank = bank_for_bill(bill, operating)
    if bank is None:
        raise payables.PayableError(f'{bill.association.name} has no single operating bank account to record this against. Choose one on the payable first.')
    return payables.pay_bills(bill.association, bank, [(bill, bill.balance)], day, method=Payment.Method.ACH, user=user, memo='Paid by the bank (automatic payment)')


# ---------------------------------------------------------------- printing

@transaction.atomic
def print_run(selected, day, user=None):
    """Make the payments for the ticked payables, in printing order, and mark them printed. One check per vendor per bank account. Returns the payments in that order.
    Everything or nothing: if one cannot be made, none is."""
    result = plan(selected)
    made = []
    for g in result['groups']:
        for c in g['checks']:
            ticked = [b for b in c['bills'] if b.ticked]
            if not ticked:
                continue
            made.append(payables.pay_bills(g['association'], g['bank'], [(b, b.balance) for b in ticked], day, method=Payment.Method.CHECK, user=user, memo=check_memo(ticked)))
    if made:
        Payment.objects.filter(pk__in=[p.pk for p in made]).update(printed_at=timezone.now(), envelope_state='waiting')          # each check now has an envelope waiting
    return made


def _draw_check(c, layout, fields, invoices=(), test=False):
    """A check on an association's own LEARNED layout (pre-printed stock). The built-in face is checkface.draw_check."""
    from reportlab.lib import colors
    w, h = layout['page_width_in'] * 72, layout['page_height_in'] * 72
    pos = checkprint.positions(layout)
    c.setFont(FONT, FONT_SIZE)
    c.setFillColor(colors.black)
    text = {'date': fields['date'], 'payee': fields['payee'], 'amount': '**' + fields['amount'], 'memo': fields['memo'], 'words': fields['words'] + (' ' + ASTERISKS if len(fields['words']) < 60 else ''),
            'stub_payee': fields['stub_payee'], 'stub_amount': fields['stub_amount'], 'stub_date': fields['stub_date'], 'stub_memo': fields['stub_memo']}
    for key, p in pos.items():
        c.drawString(p['left'] * 72, h - p['top'] * 72 - FONT_SIZE * 0.92, text.get(key, ''))
    if invoices:                                          # the invoices this check pays, under the stub's memo line
        p = pos['stub_memo']
        c.setFont(FONT, 8)
        y = h - p['top'] * 72 - FONT_SIZE * 0.92 - 15
        for line in invoices[:8]:
            c.drawString(p['left'] * 72, y, line)
            y -= 10.5
        if len(invoices) > 8:
            c.drawString(p['left'] * 72, y, f'and {len(invoices) - 8} more')
    if test:
        c.setFont(FONT, 7)
        c.setFillColor(colors.HexColor('#8a93a3'))
        c.drawString(0.3 * 72, 0.25 * 72, 'TEST PAGE: print on plain paper and hold it over a blank check. Nothing was paid.')
    return w, h


def checks_pdf(payments):
    """One page per check, in the order given. An association with a learned layout keeps it; every other check is drawn on the built-in 3-part stock face. Draws only; nothing is posted or changed."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = None
    for p in payments:
        profile = ledger.profile_for(p.bank_account.association if p.bank_account_id else p.association)
        learned = not uses_standard_face(profile)
        layout = checkprint.layout_for(profile) if learned else None
        size = (layout['page_width_in'] * 72, layout['page_height_in'] * 72) if learned else (checkface.W, checkface.H)
        if c is None:
            c = canvas.Canvas(buf, pagesize=size)
            c.setTitle('Checks')
            c.setAuthor('Proper Realty Co.')
        else:
            c.setPageSize(size)
        if learned:
            invoices = [f'{(l.bill.number or "(no number)")}   {l.bill.date:%b} {l.bill.date.day}   ${l.amount:,.2f}' for l in p.bill_links.select_related('bill').order_by('bill__date', 'bill_id')]
            _draw_check(c, layout, checkprint.check_fields(p), invoices)
        else:
            checkface.draw_check(c, checkface.check_data(p))
        c.showPage()
    if c is None:
        return b''
    c.save()
    return buf.getvalue()


def sample_checks_pdf(association, guides=False):
    """One made-up check for lining the stock up (with `guides`, faint marks for the face, the perforations and the seal). Nothing is paid. Always says TEST PAGE."""
    from reportlab.pdfgen import canvas
    profile = ledger.profile_for(association)
    buf = io.BytesIO()
    if not uses_standard_face(profile):
        layout = checkprint.layout_for(profile)
        c = canvas.Canvas(buf, pagesize=(layout['page_width_in'] * 72, layout['page_height_in'] * 72))
        c.setTitle(f'Test check - {association.name}')
        _draw_check(c, layout, checkprint.sample_check(), ['1001   Oct 3   $734.56', '1002   Oct 17   $500.00'], test=True)
    else:
        c = canvas.Canvas(buf, pagesize=(checkface.W, checkface.H))
        c.setTitle(f'Test check - {association.name}')
        checkface.draw_check(c, checkface.sample_data(association), guides=guides)
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- recent runs

def _number_key(p):
    return (p.association.name.lower(), int(p.number) if (p.number or '').isdigit() else 0, p.pk)


def recent_runs(days=14, now=None):
    """The checks printed lately, newest run first. A run is the checks printed in the same minute by the same person. Voided checks stay listed (greyed) so the history is whole."""
    since = (now or timezone.now()) - timedelta(days=days)
    pays = (Payment.objects.filter(method=Payment.Method.CHECK, opening=False, printed_at__gte=since)
            .select_related('association', 'vendor', 'bank_account', 'created_by').prefetch_related('bill_links__bill'))
    runs = OrderedDict()
    for p in sorted(pays, key=lambda p: (-p.printed_at.timestamp(), _number_key(p))):
        when = timezone.localtime(p.printed_at)
        key = f'{when:%Y%m%d%H%M}-{p.created_by_id or 0}'
        r = runs.setdefault(key, {'key': key, 'when': when, 'by': p.created_by, 'payments': []})
        r['payments'].append(p)
    out = []
    for r in runs.values():
        r['payments'].sort(key=_number_key)
        live = [p for p in r['payments'] if p.status != Payment.Status.VOID]
        r['total'] = sum((p.amount for p in live), ZERO)
        r['n'] = len(live)
        out.append(r)
    return out


def run_payments(key, days=90):
    for r in recent_runs(days):
        if r['key'] == key:
            return [p for p in r['payments'] if p.status != Payment.Status.VOID]
    return []


# ---------------------------------------------------------------- envelopes

def return_lines():
    """The return address on every envelope: the management company, not the association."""
    co = lf_config.company()
    return [co['company_name'], co['company_address1'], co['company_city']]


def is_own_check(payee):
    """A check to ourselves (the management fee): it is not mailed, so its envelope starts unticked."""
    return 'proper realty' in (payee or '').lower()


def envelope_rows(payments):
    rows = []
    for p in payments:
        lines = vendor_address_lines(p.vendor)
        rows.append({'owner': p.payee, 'lines': lines, 'source': 'mailing' if lines else 'none', 'payment': p, 'vendor': p.vendor, 'note': '' if lines else 'No mailing address on file', 'own': is_own_check(p.payee)})
    return rows


def _envelope_checks():
    return Payment.objects.filter(method=Payment.Method.CHECK, opening=False).exclude(status=Payment.Status.VOID).select_related('association', 'vendor', 'bank_account')


def waiting_envelopes():
    """The checks whose envelope has not been printed or skipped yet, from every check run, in the order the checks were printed. They stay until dealt with."""
    return sorted(_envelope_checks().filter(envelope_state='waiting'), key=lambda p: ((p.printed_at or p.created_at), _number_key(p)))


def finished_envelopes(days=14, now=None):
    """The envelopes printed or skipped lately, newest first, so a mistake can be put back."""
    since = (now or timezone.now()) - timedelta(days=days)
    return list(_envelope_checks().filter(envelope_state__in=('printed', 'skipped'), envelope_at__gte=since).order_by('-envelope_at', 'pk'))


def set_envelope_state(ids, state):
    """Mark these checks' envelopes printed, skipped, or waiting again. Returns how many changed."""
    if state not in ('waiting', 'printed', 'skipped'):
        raise ValueError(state)
    return _envelope_checks().filter(pk__in=list(ids)).update(envelope_state=state, envelope_at=None if state == 'waiting' else timezone.now())


def envelopes_pdf(rows, test=False):
    """One plain size-10 envelope per check, in the order given (the same as the checks). Vendors with no address get none (they are listed on the screen)."""
    return bp.envelopes_pdf(SimpleNamespace(name='Payables'), rows, SimpleNamespace(return_address='\n'.join(return_lines())), 'no10', test)
