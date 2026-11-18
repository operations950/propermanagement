"""The On-Site Turnovers screen: the week board (seven days from today, a week at a time) and the Day view, one screen with a Week/Day toggle. Also the two things you can do from it
(change a check-out or check-in time; schedule a cleaning and put a cleaner on it), and the redirects from the two screens it replaced (Rentals today, On-Site Properties)."""
from django.contrib import messages
from django.db import transaction
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.views.decorators.http import require_POST

from workspace.views import new_face_only

from .models import Booking, GuestRequest, Visit
from .services import notify as notify_service
from .services import reservations as reservation_service
from .services import str_board, turnovers


def _url(start=None, day=None):
    target = reverse('onsite_turnovers')
    q = QueryDict(mutable=True)
    if day:
        q['day'] = day.isoformat()
    elif start:
        q['start'] = start.isoformat()
    return f'{target}?{q.urlencode()}' if q else target


def _back(request):
    """Back to the board the person was on: the day if they were on a day, else the week they were looking at."""
    return redirect(_url(start=parse_date(request.POST.get('start', '')), day=parse_date(request.POST.get('day', ''))))


@new_face_only
def turnovers_screen(request):
    today = timezone.localdate()
    day = parse_date(request.GET.get('day', '') or '')
    if day:
        d = turnovers.day_view(day, today)
        return render(request, 'onsite/turnovers.html', {'mode': 'day', 'd': d, 'today': today, 'cleaners': d['cleaners']})
    start = parse_date(request.GET.get('start', '') or '')
    w = turnovers.week(start, today, show_all=request.GET.get('all') == '1', status_filter=request.GET.get('status', ''))
    links = [(key, f'{w["counts"][key]} {key}') for key in turnovers.STATUSES]
    return render(request, 'onsite/turnovers.html', {'mode': 'week', 'w': w, 'today': today, 'cleaners': w['cleaners'], 'status_links': links})


def old_today(request):
    """The old Rentals today address: it now opens the Turnovers Day view for the same day (?date= is kept)."""
    day = parse_date(request.GET.get('date', '') or '') or timezone.localdate()
    return redirect(_url(day=day))


def old_properties(request):
    """The old On-Site Properties board address: it is the Turnovers screen now (?start= is kept)."""
    return redirect(_url(start=parse_date(request.GET.get('start', '') or '')))


@require_POST
@new_face_only
def turnovers_time(request):
    """Change one stay's check-out or check-in time (or put it back to normal). A change that helps the cleaning is simply agreed and the cleaner told; one that takes time away waits for a yes (see str_board.record_request)."""
    booking = get_object_or_404(Booking, pk=request.POST.get('booking_id'))
    kind = request.POST.get('kind')
    if kind in ('checkout', 'checkin'):
        kinds = GuestRequest.CHECKOUT_KINDS if kind == 'checkout' else GuestRequest.CHECKIN_KINDS
        word = 'Check-out' if kind == 'checkout' else 'Check-in'
        try:
            with transaction.atomic():
                live = GuestRequest.objects.filter(booking=booking, kind__in=kinds).exclude(status=GuestRequest.Status.DECLINED)
                if request.POST.get('action') == 'remove':
                    live.delete()
                    messages.success(request, f'{word} time is back to normal.')
                else:
                    requested = parse_time((request.POST.get('time') or '').strip())
                    live.delete()           # changing a change: the old one is replaced, and put back if the new one is refused
                    made = str_board.record_request(booking, kind, requested, request.POST.get('note', ''), request.user)
                    messages.success(request, f'{word} time changed.' + (' It helps the cleaning, so it is agreed and the cleaner has been told.' if made.status == GuestRequest.Status.APPROVED
                                                                      else ' It waits for a yes or no (the ! on the board).'))
        except str_board.RequestError as e:
            messages.error(request, str(e))
    return _back(request)


@require_POST
@new_face_only
def turnovers_clean(request):
    """From the round mark on a checkout: schedule the cleaning if there is none, and put a cleaner on it if one was picked. Handled the way the visit screen does it (the cleaner is sent their link)."""
    from core.models import Contact, StaffProfile

    booking = get_object_or_404(Booking, pk=request.POST.get('booking_id'))
    visit = booking.visits.exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED]).order_by('-pk').first()
    if visit is None:
        reservation_service._visit_for(booking)
        visit = booking.visits.exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED]).order_by('-pk').first()
        if visit is None:
            messages.error(request, 'A cleaning could not be scheduled: there is no turnover visit type set up for it.')
        else:
            messages.success(request, 'Cleaning scheduled.')
    kind, _, raw_id = request.POST.get('assignee', '').partition('-')
    if visit is not None and kind in ('staff', 'contact') and raw_id.isdigit():
        if kind == 'staff' and StaffProfile.objects.filter(pk=raw_id).exists():
            visit.assigned_staff_id, visit.assigned_contact = int(raw_id), None
        elif kind == 'contact' and Contact.objects.filter(pk=raw_id).exists():
            visit.assigned_contact_id, visit.assigned_staff = int(raw_id), None
        else:
            visit = None
        if visit is not None:
            if visit.status == Visit.Status.UNASSIGNED:
                visit.status = Visit.Status.SCHEDULED
            visit.full_clean()
            visit.save()
            link_note = ''
            if notify_service.dispatch_link(visit, request) == notify_service.SCHEDULED:
                link_note = f' The cleaner will be sent the link {notify_service.link_send_description(visit)}.'
            messages.success(request, f'Cleaning is with {visit.assignee_label()}.{link_note}')
    return _back(request)
