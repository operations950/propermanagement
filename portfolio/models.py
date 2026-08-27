"""Personal multi-business task tracker — a private layer on top of
proptasks for the owner's own businesses outside real estate (see the
"custom multi-business dashboard" conversation). Deliberately NOT wired
into Ticket/Property/StaffProfile.role at all: the real-estate ticket
engine stays untouched (assigned_role dispatch, checklists, Processes
gating, vendor SMS threads, Quo integration are all real-estate-specific
and too risky to fold another domain into). This app shares zero models
with it — the two are only ever unified at the dashboard layer, where the
Real Estate box reads live from tickets.models.Ticket and every other box
reads from BizTask below. See portfolio/services/generation.py for the
recurring engine, deliberately mirroring worksessions/services/
generation.py's cursor-walk shape.

Access is gated by StaffProfile.is_portfolio_owner (core/models.py) plus
Business.additional_staff below — see portfolio/views.py. Not linked from
the shared site nav; this only exists at /portfolio/ for whoever holds
that flag."""
import zlib

from django.db import models

from core.models import StaffProfile
from tickets.models import Priority


# Stable, arbitrary color assignment per business — same trick as
# tickets/views.py::_calendar_color, so a business always renders the same
# shade across page loads without needing a color field anyone has to pick.
_BOX_COLOR_ROTATION = [
    'var(--brand-primary)',
    'var(--accent-contrast)',
    'var(--brand-slate)',
    'color-mix(in srgb, var(--accent-contrast) 55%, white)',
    'color-mix(in srgb, var(--brand-primary) 55%, black)',
    'color-mix(in srgb, var(--accent-contrast) 55%, black)',
]


class Business(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=110, unique=True, blank=True)
    icon = models.CharField(
        max_length=40, blank=True, default='briefcase',
        help_text='A lucide-icons name (e.g. "briefcase", "home", "package") shown on its dashboard box.',
    )
    owner = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, related_name='owned_businesses',
        help_text='Always the portfolio owner today — see StaffProfile.is_portfolio_owner.',
    )
    additional_staff = models.ManyToManyField(
        StaffProfile, blank=True, related_name='shared_businesses',
        help_text='Staff besides the owner allowed into THIS business\'s own sub-dashboard only '
                   '(not the combined /portfolio/ view, which stays owner-only). Empty by default — '
                   'a hook for later, e.g. a bookkeeper on one specific business.',
    )
    custom_field_label = models.CharField(
        max_length=50, blank=True,
        help_text='Optional — set a label (e.g. "Account #") to show one extra free-text field on '
                   'every task under this business. Blank = no extra field.',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'businesses'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)[:110]
        super().save(*args, **kwargs)

    @property
    def color(self):
        return _BOX_COLOR_ROTATION[zlib.crc32(self.slug.encode()) % len(_BOX_COLOR_ROTATION)]


class BusinessCategory(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(max_length=100)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['business', 'name'], name='uniq_bizcategory_business_name'),
        ]
        verbose_name_plural = 'business categories'

    def __str__(self):
        return f'{self.business.name} — {self.name}'


class Frequency(models.TextChoices):
    WEEKLY = 'weekly', 'Weekly'
    BIWEEKLY = 'biweekly', 'Bi-weekly'
    MONTHLY_DAY = 'monthly_day', 'Monthly (on a specific day)'
    MONTHLY_WORKDAY = 'monthly_workday', 'Monthly (by working day)'
    QUARTERLY = 'quarterly', 'Quarterly'
    YEARLY = 'yearly', 'Yearly'


class BizRecurringRule(models.Model):
    """The rule; portfolio/services/generation.py walks next_due_date
    forward and creates BizTask rows along the way — same catch-up-safe
    cursor shape as worksessions.SessionTemplate/generation.py, just
    producing one flat task per occurrence instead of a Session+lines."""
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='recurring_rules')
    category = models.ForeignKey(
        BusinessCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='recurring_rules',
    )
    title = models.CharField(max_length=300, help_text='Used as the generated task\'s title each time.')
    notes = models.TextField(blank=True)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    custom_field_value = models.CharField(max_length=200, blank=True)

    frequency = models.CharField(max_length=20, choices=Frequency.choices)
    day_of_month = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Only used when frequency is "Monthly (on a specific day)" — 1-31. A month shorter '
                   'than the chosen day (e.g. 31 in April) lands on that month\'s last real day instead.',
    )
    workday_of_month = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Only used when frequency is "Monthly (by working day)" — e.g. 3 means the 3rd '
                   'Mon-Fri business day of the month. Weekends are skipped; holidays are not '
                   'accounted for.',
    )
    next_due_date = models.DateField(help_text='The next date this rule should generate a task for.')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['business', 'title']

    def __str__(self):
        return f'{self.title} ({self.get_frequency_display()})'


class BizTask(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        DONE = 'done', 'Done'
        CANCELLED = 'cancelled', 'Cancelled'

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='tasks')
    category = models.ForeignKey(
        BusinessCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='tasks',
    )
    recurring_rule = models.ForeignKey(
        BizRecurringRule, on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_tasks',
        help_text='Set when this task was generated by a recurring rule; blank for a one-off task.',
    )
    title = models.CharField(max_length=300)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    due_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Optional dollar amount — e.g. a bill total. Blank when not relevant.',
    )
    custom_field_value = models.CharField(
        max_length=200, blank=True, help_text='Shown/labeled only when business.custom_field_label is set.',
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['business', 'due_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['recurring_rule', 'due_date'],
                name='uniq_biztask_rule_due_date',
                # NULL-safe by ordinary SQL semantics (NULL never equals
                # NULL in a unique constraint on Postgres or SQLite), so
                # one-off tasks (recurring_rule left blank) never collide
                # with each other — this only dedupes actual generated
                # occurrences of the same rule on the same due date. See
                # portfolio/services/generation.py::generate_for_rule.
            ),
        ]

    def __str__(self):
        return self.title
