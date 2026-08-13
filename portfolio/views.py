"""The private multi-business dashboard — see portfolio/models.py's module
docstring for why this shares zero models with the real-estate Ticket
engine. Every view here is gated: the combined dashboard is owner-only
(StaffProfile.is_portfolio_owner), a single business's own sub-dashboard
additionally accepts anyone in that Business's additional_staff (see
_can_access_business) — the hook the owner asked for so one business can
later be opened to a helper without touching this file again."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from tickets.models import Priority, Ticket
from tickets.views import OPEN_STATUSES

from .models import BizRecurringRule, BizTask, Business, BusinessCategory, Frequency
from .services.generation import preview_next_occurrences


def _staff_profile(request):
    return getattr(request.user, 'staff_profile', None)


def _require_portfolio_owner(request):
    profile = _staff_profile(request)
    return profile if profile and profile.is_portfolio_owner else None


def _can_access_business(profile, business):
    if not profile:
        return False
    if profile.is_portfolio_owner:
        return True
    return business.additional_staff.filter(pk=profile.pk).exists()


@login_required
def dashboard(request):
    profile = _require_portfolio_owner(request)
    if not profile:
        return HttpResponseForbidden("You don't have access to this page.")

    now = timezone.now()
    today = timezone.localdate()

    real_estate_tickets = list(
        Ticket.objects.filter(assigned_staff=profile, status__in=OPEN_STATUSES)
        .select_related('property').order_by('due_date')
    )
    real_estate_box = {
        'name': 'Real Estate',
        'icon': 'building-2',
        'color': 'var(--brand-primary)',
        'url': reverse('dashboard'),
        'total': len(real_estate_tickets),
        'overdue_count': sum(
            1 for t in real_estate_tickets
            if t.due_date and timezone.localtime(t.due_date).date() < today
        ),
        'top': real_estate_tickets[:5],
        'kind': 'ticket',
    }

    business_boxes = []
    for business in Business.objects.filter(is_active=True).prefetch_related('tasks'):
        if not _can_access_business(profile, business):
            continue
        open_tasks = [t for t in business.tasks.all() if t.status == BizTask.Status.OPEN]
        open_tasks.sort(key=lambda t: (t.due_date is None, t.due_date))
        business_boxes.append({
            'name': business.name,
            'icon': business.icon or 'briefcase',
            'color': business.color,
            'url': reverse('portfolio_business_detail', args=[business.slug]),
            'total': len(open_tasks),
            'overdue_count': sum(1 for t in open_tasks if t.due_date and t.due_date < today),
            'top': open_tasks[:5],
            'kind': 'task',
        })

    return render(request, 'portfolio/dashboard.html', {
        'now': now,
        'real_estate_box': real_estate_box,
        'business_boxes': business_boxes,
    })


@login_required
def business_detail(request, slug):
    business = get_object_or_404(Business, slug=slug, is_active=True)
    profile = _staff_profile(request)
    if not _can_access_business(profile, business):
        raise PermissionDenied("You don't have access to this business.")

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_task':
            title = request.POST.get('title', '').strip()
            if not title:
                messages.error(request, "A task needs a title.")
                return redirect('portfolio_business_detail', slug=slug)
            category_id = request.POST.get('category') or None
            due_date = request.POST.get('due_date') or None
            amount = request.POST.get('amount') or None
            BizTask.objects.create(
                business=business,
                category_id=category_id,
                title=title,
                notes=request.POST.get('notes', '').strip(),
                priority=request.POST.get('priority') or Priority.MEDIUM,
                due_date=due_date,
                amount=amount,
                custom_field_value=request.POST.get('custom_field_value', '').strip(),
            )
            messages.success(request, 'Task added.')

        elif action == 'toggle_task':
            task = get_object_or_404(BizTask, pk=request.POST.get('task_id'), business=business)
            if task.status == BizTask.Status.DONE:
                task.status = BizTask.Status.OPEN
                task.completed_at = None
            else:
                task.status = BizTask.Status.DONE
                task.completed_at = timezone.now()
            task.save(update_fields=['status', 'completed_at'])

        elif action == 'delete_task':
            BizTask.objects.filter(pk=request.POST.get('task_id'), business=business).delete()
            messages.success(request, 'Task deleted.')

        elif action == 'add_category':
            name = request.POST.get('name', '').strip()
            if name:
                BusinessCategory.objects.get_or_create(business=business, name=name)
                messages.success(request, f'Category "{name}" added.')

        elif action == 'delete_category':
            BusinessCategory.objects.filter(pk=request.POST.get('category_id'), business=business).delete()

        elif action == 'add_rule':
            title = request.POST.get('rule_title', '').strip()
            frequency = request.POST.get('frequency')
            next_due = request.POST.get('next_due_date')
            if not title or frequency not in Frequency.values or not next_due:
                messages.error(request, "A recurring rule needs a title, frequency, and first due date.")
                return redirect('portfolio_business_detail', slug=slug)
            day_of_month = request.POST.get('day_of_month') or None
            workday_of_month = request.POST.get('workday_of_month') or None
            BizRecurringRule.objects.create(
                business=business,
                category_id=request.POST.get('rule_category') or None,
                title=title,
                notes=request.POST.get('rule_notes', '').strip(),
                priority=request.POST.get('rule_priority') or Priority.MEDIUM,
                amount=request.POST.get('rule_amount') or None,
                custom_field_value=request.POST.get('rule_custom_field_value', '').strip(),
                frequency=frequency,
                day_of_month=int(day_of_month) if day_of_month else None,
                workday_of_month=int(workday_of_month) if workday_of_month else None,
                next_due_date=next_due,
            )
            messages.success(request, 'Recurring rule added — its first task will appear on schedule.')

        elif action == 'toggle_rule':
            rule = get_object_or_404(BizRecurringRule, pk=request.POST.get('rule_id'), business=business)
            rule.is_active = not rule.is_active
            rule.save(update_fields=['is_active'])

        elif action == 'delete_rule':
            BizRecurringRule.objects.filter(pk=request.POST.get('rule_id'), business=business).delete()
            messages.success(request, 'Recurring rule deleted.')

        return redirect('portfolio_business_detail', slug=slug)

    show = request.GET.get('show', 'open')
    tasks = business.tasks.select_related('category').all()
    if show == 'open':
        tasks = tasks.filter(status=BizTask.Status.OPEN)
    elif show == 'done':
        tasks = tasks.filter(status=BizTask.Status.DONE)

    rules = list(business.recurring_rules.select_related('category').all())
    for rule in rules:
        # Attached directly to the object rather than passed as a
        # separate pk-keyed dict — Django templates can't do arbitrary
        # dict lookups by a per-row key without a custom filter, so this
        # is the simpler path. Avoids strftime's %-d (POSIX-only, absent
        # on Windows — see worksessions/services/generation.py's own note
        # on the same issue) by building the label with str(d.day) instead.
        rule.next_occurrences_display = ', '.join(
            f'{d.strftime("%b")} {d.day}' for d in preview_next_occurrences(rule, count=3)
        )

    return render(request, 'portfolio/business_detail.html', {
        'business': business,
        'tasks': tasks,
        'categories': business.categories.all(),
        'rules': rules,
        'show': show,
        'priority_choices': Priority.choices,
        'frequency_choices': Frequency.choices,
        'today': timezone.localdate(),
        'is_owner': profile.is_portfolio_owner if profile else False,
    })
