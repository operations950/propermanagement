"""One place to review every FAQ answer the message assistant wrote that nobody has checked yet,
grouped by property, so a single person can go through them all at once. Reviewing an entry
(or editing it) locks it: the assistant can go on using it but can no longer overwrite it."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import faq as faq_service
from . import nudges
from .models import Property, PropertyFAQ, Unit

MAX_SHOWN = 300
BASIS_ORDER = {PropertyFAQ.Basis.INFERRED: 0, PropertyFAQ.Basis.PROPERTY_RECORD: 1, PropertyFAQ.Basis.HOST_REPLY: 2}


def _unreviewed(basis=''):
    qs = PropertyFAQ.objects.filter(status=PropertyFAQ.Status.ACTIVE, reviewed=False).select_related('property', 'unit', 'created_by_key')
    if basis in PropertyFAQ.Basis.values:
        qs = qs.filter(basis=basis)
    return qs


@login_required
def faq_review_queue(request):
    basis = request.GET.get('basis', '')
    if basis not in PropertyFAQ.Basis.values:
        basis = ''
    here = request.get_full_path()
    if request.method == 'POST':
        action = request.POST.get('action', '')
        try:
            if action == 'review_property':
                entries = list(_unreviewed(basis).filter(property_id=request.POST.get('property_id')))
                for entry in entries:
                    faq_service.staff_review(entry, request.user)
                messages.success(request, f'{len(entries)} answer{"" if len(entries) == 1 else "s"} marked reviewed.')
            elif action == 'review_picked':
                ids = [int(i) for i in request.POST.getlist('pick') if i.isdigit()]
                entries = list(_unreviewed().filter(pk__in=ids))
                for entry in entries:
                    faq_service.staff_review(entry, request.user)
                messages.success(request, f'{len(entries)} answer{"" if len(entries) == 1 else "s"} marked reviewed.' if entries else 'Tick the answers you want to mark reviewed first.')
            elif action in ('review', 'archive', 'edit'):
                entry = get_object_or_404(PropertyFAQ, pk=request.POST.get('faq_id'), status=PropertyFAQ.Status.ACTIVE)
                if action == 'review':
                    faq_service.staff_review(entry, request.user)
                    messages.success(request, 'Marked reviewed — the assistant can use it but no longer change it.')
                elif action == 'archive':
                    faq_service.staff_archive(entry)
                    messages.success(request, 'Removed.')
                else:
                    unit = faq_service.KEEP_UNIT
                    if 'unit_id' in request.POST:
                        raw = request.POST['unit_id']
                        unit = Unit.objects.filter(pk=raw, property=entry.property).first() if raw.isdigit() else None
                    faq_service.staff_edit(entry, request.user, request.POST.get('question', ''), request.POST.get('answer', ''), unit=unit)
                    messages.success(request, 'Saved and marked reviewed.')
        except faq_service.FAQError as exc:
            messages.error(request, str(exc))
        return redirect(here)

    today = timezone.localdate()
    entries = list(_unreviewed(basis))
    truncated = len(entries) > MAX_SHOWN
    for entry in entries:
        entry.waiting_days = (today - timezone.localtime(entry.created_at).date()).days
        entry.overdue = entry.waiting_days >= nudges.FAQ_OVERDUE_DAYS
    groups = {}
    for entry in sorted(entries, key=lambda e: (e.property.name.lower(), BASIS_ORDER.get(e.basis, 3), -e.times_used, e.question)):
        group = groups.setdefault(entry.property_id, {'property': entry.property, 'entries': [], 'units': None})
        if len(group['entries']) < MAX_SHOWN:
            group['entries'].append(entry)
    sections = list(groups.values())
    for group in sections:
        group['count'] = len(group['entries'])
        group['oldest'] = max((e.waiting_days for e in group['entries']), default=0)
        group['guesses'] = sum(1 for e in group['entries'] if e.basis == PropertyFAQ.Basis.INFERRED)
        group['units'] = list(group['property'].units.filter(is_active=True))
    sections.sort(key=lambda g: (-g['oldest'] if g['oldest'] >= nudges.FAQ_OVERDUE_DAYS else 0, -g['count'], g['property'].name.lower()))
    stats = nudges.faq_stats(today)
    return render(request, 'core/faq_review.html', {
        'sections': sections, 'stats': stats, 'basis': basis, 'total_shown': sum(g['count'] for g in sections),
        'truncated': truncated, 'basis_choices': [('', 'All'), (PropertyFAQ.Basis.INFERRED, 'Assistant\'s guesses'), (PropertyFAQ.Basis.HOST_REPLY, 'Host replies'), (PropertyFAQ.Basis.PROPERTY_RECORD, 'From the property record')],
        'open_all': sum(g['count'] for g in sections) <= 25, 'overdue_days': nudges.FAQ_OVERDUE_DAYS,
    })
