"""Accounting settings, the official record, bank accounts and the documents (upload, ask, confirm)."""
from decimal import Decimal

from django.contrib import messages
from django.http import JsonResponse

from core.models import StaffProfile
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import charges, checkprint, closing, documents, invoice_inbox, ledger, owners
from . import standard_chart as sc
from .models import AssociationDocument, BankAccount, Budget, ChargeRule, SettingProposal
from .web import admin_only, association_or_404, parse_date, parse_money


def _go(name, *args):
    return reverse(f'accounting:{name}', args=args)


def settings_rows(prop):
    """The accounting settings, each with whether it is set: what the property page and the settings screen list."""
    profile = ledger.profile_for(prop)
    rule = ChargeRule.objects.filter(association=prop, is_active=True, is_special=False).first()
    share_rows = owners.shares(prop)
    shares_ok = bool(share_rows) and sum((p for u, p in share_rows), Decimal('0')) == Decimal('100')
    shares_text = ('Equal shares, %d units' % len(share_rows) if len({round(p, 5) for u, p in share_rows}) == 1 else 'Set unit by unit') if shares_ok else 'Not set'
    rows = [
        ('We do the financials', 'On' if profile.we_do_financials else 'Off', profile.we_do_financials, None, ''),
        ('Fiscal year', f'{["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"][profile.fiscal_year_start_month]} start', True, 'settings', 'terms'),
        ('Billing', (f'${rule.amount:,.2f} {"a unit" if rule.method == "flat" else "split by share"}, {rule.get_frequency_display().lower()}') if rule else 'No regular charge yet', bool(rule), 'charges', ''),
        ('Unit shares', shares_text, shares_ok, 'shares', ''),
        ('Bank accounts', ', '.join(f'{b.name} ({b.bank_name})' if b.bank_name else b.name for b in prop.bank_accounts.filter(is_active=True)) or 'None yet', prop.bank_accounts.filter(is_active=True).exists(), 'settings', 'banks'),
        ('Reserve spending', 'Always needs board approval', True, None, ''),
        ('Late fee and interest', 'Read from the declaration', profile.late_fee_is_set(), 'settings', 'late'),
        ('Spending limit without the board', f'${profile.spending_limit:,.2f}' if profile.spending_limit is not None else 'Read from the management contract', profile.spending_limit is not None, 'settings', 'terms'),
        ('Management fee', f'${profile.management_fee:,.2f} a month' if profile.management_fee is not None else 'Read from the management contract', profile.management_fee is not None, 'settings', 'terms'),
        ('Check printing', 'Layout learned from your check stock' if profile.check_layout.get('learned') else ('Standard layout' if profile.check_stock else 'Upload a copy of the blank check stock'), bool(profile.check_layout.get('learned')), 'check_setup', ''),
    ]
    out = []
    for label, value, ok, url, anchor in rows:
        if label in ('Late fee and interest',) and profile.late_fee_is_set():
            fee = {'flat': f'${profile.late_fee_flat:,.2f}', 'percent': f'{charges._plain(profile.late_fee_percent)}%', 'greater': f'${profile.late_fee_flat or 0:,.2f} or {charges._plain(profile.late_fee_percent)}%, whichever is greater'}.get(profile.late_fee_kind, '')
            value = f'{fee}, {profile.late_fee_grace_days or 0} days after the due date' + (f'; interest {charges._plain(profile.interest_rate)}% a year' if profile.interest_is_set() else '; no interest')
        out.append({'label': label, 'value': value, 'ok': ok, 'url': _go(url, prop.pk) if url else '', 'anchor': anchor, 'key': 'financials' if label == 'We do the financials' else ''})
    return out


def _example_assessment(prop):
    """The regular assessment, to show the late fee on: the first active charge rule that is not special."""
    rule = ChargeRule.objects.filter(association=prop, is_active=True, is_special=False, method='flat').first()
    return rule.amount if rule else None


def _bubble_days(current, base):
    """The bubbles for a number of days: the usual ones, and the one in use if it is not among them."""
    days = sorted(set(base) | {current})
    return [(d, 'The day after it is due' if d == 0 else f'{d} days after') for d in days]


def assoc_context(prop):
    """Everything the association's property page needs for the cards it shows only for associations."""
    profile = ledger.profile_for(prop)
    from .views import _unit_rows
    rows = _unit_rows(prop)
    owing = sorted([r for r in rows if r['balance'] > 0], key=lambda r: -r['balance'])
    totals = charges.totals(charges.accounts(prop))
    todo = []
    if profile.registered_agent_alert():
        todo.append('the registered agent is not us')
    if not documents.has_contract(prop):
        todo.append('upload the management contract')
    if profile.we_do_financials and not profile.late_fee_is_set():
        todo.append('set up the late fee rule')
    if profile.we_do_financials and not Budget.objects.filter(association=prop).exists():
        todo.append('load the budget')
    officers = profile.sunbiz_officers or []
    board_names = {t.person.name.lower() for t in owners.board(prop)}
    matches = bool(officers) and all(o.get('name', '').lower() in board_names for o in officers)
    proposals = list(SettingProposal.objects.filter(property=prop, status='proposed').select_related('document'))
    for p in proposals:
        p.label, p.summary, p.source = documents.LABELS.get(p.key, p.key), documents.describe(p), documents.source_label(p.document, p.page)
    ap_files = documents.files(prop)
    if any(f['state'] == 'needs_look' for f in ap_files):
        todo.append('confirm what the uploaded documents are')
    return {'ap_files': ap_files, 'doc_kinds': [(k, 'Amendment' if k == 'amendment' else l) for k, l in AssociationDocument.Kind.choices if k != 'other'] + [('other', 'Something else')], 'ap': profile, 'ap_rows': settings_rows(prop), 'ap_docs': documents.checklist(prop), 'staff_choices': StaffProfile.objects.filter(user__is_active=True).select_related('user').order_by('user__first_name', 'user__username'), 'terms_lines': charges.terms_sentences(profile, _example_assessment(prop)), 'late_days': _bubble_days(profile.late_fee_grace_days or 0, (0, 5, 10, 15, 20, 30)), 'interest_days': _bubble_days(profile.interest_grace_days or 0, (0, 10, 15, 30)),
            'example_assessment': _example_assessment(prop), 'applies_choices': [('declaration', 'Declaration'), ('articles', 'Articles'), ('bylaws', 'Bylaws'), ('rules', 'Rules and regulations')], 'ap_proposals': proposals,
            'ap_units': rows, 'ap_owing': owing[:5], 'ap_totals': totals, 'ap_todo': todo, 'ap_board': owners.board(prop), 'ap_board_matches': matches,
            'ap_qa': documents.qa_payload(prop), 'ap_has_contract': documents.has_contract(prop)}


@admin_only
def settings(request, pk):
    prop = association_or_404(pk)
    profile = ledger.profile_for(prop)
    here = _go('settings', prop.pk)
    if request.method == 'POST' and request.POST.get('action') == 'q_read':
        # someone sat with these answers on screen: one read each, once a day per person
        n = sum(1 for q in prop.document_questions.filter(pk__in=[i for i in request.POST.getlist('ids') if i.isdigit()][:10]) if documents.record_read(q, request.user))
        return JsonResponse({'counted': n})
    asked_id = None
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'switch':
                on = bool(request.POST.get('on'))
                if on and not documents.has_contract(prop):
                    raise ValueError('Upload the management contract first: every association we keep the books for needs its contract on file.')
                profile.we_do_financials = on
                profile.save(update_fields=['we_do_financials'])
                if on:
                    ledger.ensure_chart(prop)
                    owners.default_share_kind(prop)
                messages.success(request, 'We now keep the books for this association.' if on else 'We no longer keep the books for this association. What is there stays.')
            elif action == 'late_terms':
                kind = request.POST.get('late_fee_kind') or 'none'
                if kind not in ('none', 'flat', 'percent', 'greater'):
                    raise ValueError('Choose the kind of late fee.')
                flat = parse_money(request.POST.get('late_fee_flat')) if (request.POST.get('late_fee_flat') or '').strip() else None
                pct = Decimal(request.POST['late_fee_percent'].replace('%', '').strip()) if (request.POST.get('late_fee_percent') or '').strip() else None
                if kind in ('flat', 'greater') and not flat:
                    raise ValueError('Type the flat amount of the late fee.')
                if kind in ('percent', 'greater') and not pct:
                    raise ValueError('Type the percent of the late fee.')
                rate = Decimal(request.POST['interest'].replace('%', '').strip()) if (request.POST.get('interest') or '').strip() else None
                if rate is not None and not Decimal('0') <= rate <= Decimal('100'):
                    raise ValueError('Interest is a percent a year between 0 and 100.')
                profile.late_fee_kind, profile.late_fee_flat, profile.late_fee_percent = kind, (flat if kind in ('flat', 'greater') else None), (pct if kind in ('percent', 'greater') else None)
                profile.late_fee_grace_days = int(request.POST.get('late_grace') or 0)
                profile.late_fee_confirmed = kind != 'none'
                profile.interest_rate = rate if rate else None
                profile.interest_grace_days = int(request.POST.get('interest_grace') or 0)
                profile.interest_confirmed = bool(rate)
                profile.save()
                messages.success(request, 'Saved: ' + ' '.join(charges.terms_sentences(profile)))
            elif action == 'terms':
                owner_id = (request.POST.get('compliance_owner') or '').strip()
                profile.compliance_owner_id = int(owner_id) if owner_id.isdigit() else None
                profile.spending_limit = parse_money(request.POST.get('spending_limit')) if (request.POST.get('spending_limit') or '').strip() else None
                profile.management_fee = parse_money(request.POST.get('management_fee')) if (request.POST.get('management_fee') or '').strip() else None
                profile.fiscal_year_start_month = max(1, min(12, int(request.POST.get('fiscal_month') or 1)))
                profile.auto_post = bool(request.POST.get('auto_post'))
                profile.save()
                messages.success(request, 'Saved.')
            elif action == 'official':
                for f in ('legal_name', 'state_doc_number', 'ein', 'state_status', 'principal_address', 'mailing_address', 'registered_agent', 'sunbiz_url'):
                    setattr(profile, f, (request.POST.get(f) or '').strip())
                profile.date_filed, profile.annual_report_filed = parse_date(request.POST.get('date_filed')), parse_date(request.POST.get('annual_report_filed'))
                profile.sunbiz_checked_on, profile.sunbiz_checked_by = timezone.localdate(), 'hand'
                profile.sunbiz_alerts = ['The registered agent is ' + (profile.registered_agent.split(',')[0] or 'not listed') + ', not Proper Realty Co.'] if profile.registered_agent_alert() else []
                officers = []
                for line in (request.POST.get('officers') or '').splitlines():
                    if ',' in line:
                        name, title = line.split(',', 1)
                        officers.append({'name': name.strip(), 'title': title.strip()})
                profile.sunbiz_officers = officers
                profile.save()
                messages.success(request, 'The official record is saved.')
            elif action == 'sunbiz_check':
                from . import sunbiz
                ok, message = sunbiz.check(profile)
                (messages.success if ok else messages.error)(request, message)
            elif action == 'bank':
                name = (request.POST.get('name') or '').strip()
                code = request.POST.get('account')
                if not name:
                    raise ValueError('Name the bank account.')
                acct = prop.accounts.filter(pk=code).first()
                if acct is None:
                    raise ValueError('Choose the cash account in the books it is.')
                BankAccount.objects.update_or_create(association=prop, name=name, defaults={'bank_name': (request.POST.get('bank_name') or '').strip(), 'last4': (request.POST.get('last4') or '').strip()[:4],
                                                                                             'kind': 'reserve' if request.POST.get('kind') == 'reserve' else 'operating', 'account': acct, 'is_active': True})
                messages.success(request, 'Saved.')
            elif action == 'bank_off':
                BankAccount.objects.filter(pk=request.POST.get('bank'), association=prop).update(is_active=False)
            elif action == 'upload':
                f = request.FILES.get('file')
                if f is None:
                    raise ValueError('Choose the document.')
                kind = request.POST.get('kind') if request.POST.get('kind') in dict(AssociationDocument.Kind.choices) else 'other'
                documents.upload(prop, kind, f, request.user)
                messages.success(request, 'Uploaded.')
            elif action == 'upload_many':
                made, problems = documents.upload_many(prop, request.FILES.getlist('files'), request.user)
                for p in problems:
                    messages.error(request, p)
                if made:
                    messages.success(request, f'{len(made)} file{"s" if len(made) != 1 else ""} uploaded. Each is read once, which can take a few minutes for a long scan; then it is sorted for you to confirm.')
                elif not problems:
                    raise ValueError('Choose at least one file.')
            elif action == 'sort_confirm':
                doc = get_object_or_404(AssociationDocument, pk=request.POST.get('doc'), property=prop)
                documents.confirm_sections(doc, [(request.POST.get(f'kind_{i}'), request.POST.get(f'title_{i}'), request.POST.get(f'applies_{i}')) for i in range(len(doc.sections or []))])
                messages.success(request, 'Saved: the checklist is up to date.')
            elif action == 'sort_again':
                documents.sort_again(get_object_or_404(AssociationDocument, pk=request.POST.get('doc'), property=prop))
                messages.success(request, 'Sorting it again: it will be ready to look at in a minute.')
            elif action == 'read_again':
                documents.read_again(get_object_or_404(AssociationDocument, pk=request.POST.get('doc'), property=prop))
                messages.success(request, 'Reading it again.')
            elif action == 'delete_doc':
                get_object_or_404(AssociationDocument, pk=request.POST.get('doc'), property=prop).delete()
                messages.success(request, 'Removed.')
            elif action == 'ask':
                q = documents.ask(prop, request.POST.get('question'), request.user, force=bool(request.POST.get('again')))
                asked_id = q.pk
                messages.success(request, 'Asked before: shown from the list, nothing was read again.' if getattr(q, 'from_memory', False) else 'Answered.')
            elif action == 'read_doc':
                made = documents.propose_from_document(get_object_or_404(AssociationDocument, pk=request.POST.get('doc'), property=prop), request.user)
                messages.success(request, f'{len(made)} setting{"s" if len(made) != 1 else ""} found. Confirm the ones that are right.' if made else 'No settings were found in that document.')
            elif action in ('confirm', 'dismiss'):
                p = get_object_or_404(SettingProposal, pk=request.POST.get('proposal'), property=prop)
                if action == 'dismiss':
                    documents.dismiss(p, request.user)
                else:
                    value = dict(p.value)
                    for k in list(value):
                        raw = request.POST.get(f'v_{k}')
                        if raw not in (None, ''):
                            value[k] = raw.replace('$', '').replace('%', '').replace(',', '').strip()
                    documents.confirm(p, request.user, value)
                    messages.success(request, f'{documents.LABELS[p.key]} is set.')
        except (ValueError, documents.DocumentError, checkprint.CheckPrintError) as e:
            messages.error(request, str(e))
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception('Accounting settings action failed')
            messages.error(request, 'That did not work. Try again.')
        target = request.POST.get('next') or here
        if asked_id:
            base, _, frag = target.partition('#')
            target = f'{base}{"&" if "?" in base else "?"}asked={asked_id}#{frag or "documents"}'
        return redirect(target)
    ctx = assoc_context(prop)
    ctx.update({'assoc': prop, 'profile': profile, 'cash_accounts': prop.accounts.filter(code__lt='1200', kind='asset').order_by('code'), 'banks': prop.bank_accounts.all(),
                'officers_text': '\n'.join(f'{o["name"]}, {o["title"]}' for o in (profile.sunbiz_officers or [])), 'months': list(enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'], 1)),
                'acc_back': reverse('property_detail', args=[prop.pk]), 'acc_back_label': prop.name})
    return render(request, 'accounting/settings.html', ctx)
