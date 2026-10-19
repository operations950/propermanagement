from datetime import time

from django.conf import settings
from django.db import models

from . import policy


class Status(models.TextChoices):
    PENDING = 'pending', 'Waiting'
    APPROVED = 'approved', 'Approved'
    DECLINED = 'declined', 'Declined'


class TimeProfile(models.Model):
    """One staff member's time clock settings: whether the clock and vacation policy apply to them, how they are paid, when they were hired and
    which days they normally work. A staff member with no row here (or applies=False) is not covered at all."""
    class PayType(models.TextChoices):
        SALARIED = 'salaried', 'Salaried'
        HOURLY = 'hourly', 'Hourly'

    staff = models.OneToOneField('core.StaffProfile', on_delete=models.CASCADE, related_name='time_profile')
    applies = models.BooleanField(default=False, help_text='Must clock in, and is covered by the vacation and holiday policy.')
    tracking_start = models.DateField(null=True, blank=True, help_text='The first day the clock counts for this person (set when "applies" is first turned on).')
    pay_type = models.CharField(max_length=10, choices=PayType.choices, default=PayType.HOURLY)
    hire_date = models.DateField(null=True, blank=True, help_text='Sets the year of employment, and with it the vacation days earned. Nobody earns vacation until this is filled in.')
    work_days = models.CharField(max_length=13, default='0,1,2,3,4', help_text='Comma-separated weekday numbers, Monday = 0.')
    day_start = models.TimeField(default=time(9, 0))
    day_end = models.TimeField(default=time(17, 0))

    def __str__(self):
        return str(self.staff)

    @property
    def name(self):
        user = self.staff.user
        return user.get_full_name() or user.username

    @property
    def first_name(self):
        return self.staff.user.first_name or self.name.split(' ')[0]

    @property
    def days(self):
        return policy.parse_days(self.work_days)

    @property
    def is_hourly(self):
        return self.pay_type == self.PayType.HOURLY


class Punch(models.Model):
    """One working day's clock-in and clock-out. Lunch is asked once, at clock-out, and taken off the day (0 = no lunch). A manager's fix is a
    PunchChange row; the original values stay in it."""
    profile = models.ForeignKey(TimeProfile, on_delete=models.CASCADE, related_name='punches')
    day = models.DateField(db_index=True)
    clock_in = models.DateTimeField()
    clock_out = models.DateTimeField(null=True, blank=True)
    lunch_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    in_remote = models.BooleanField(default=False)
    out_remote = models.BooleanField(default=False)
    remote_reason = models.CharField(max_length=200, blank=True)
    in_ip = models.CharField(max_length=45, blank=True)
    out_ip = models.CharField(max_length=45, blank=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                 help_text='Set when a manager entered the day instead of the employee clocking in.')

    class Meta:
        unique_together = [('profile', 'day')]
        ordering = ['day']

    def __str__(self):
        return f'{self.profile} {self.day}'

    @property
    def minutes(self):
        """Minutes worked after lunch (None until clocked out)."""
        if not self.clock_out:
            return None
        gross = int((self.clock_out - self.clock_in).total_seconds() // 60)
        return max(0, gross - (self.lunch_minutes or 0))

    @property
    def remote(self):
        return self.in_remote or self.out_remote


class PunchChange(models.Model):
    """The history of a fixed punch: what it said before, what it says now, who changed it and why."""
    punch = models.ForeignKey(Punch, on_delete=models.CASCADE, related_name='changes')
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=300, blank=True)
    old_in = models.DateTimeField(null=True, blank=True)
    old_out = models.DateTimeField(null=True, blank=True)
    old_lunch = models.PositiveSmallIntegerField(null=True, blank=True)
    new_in = models.DateTimeField(null=True, blank=True)
    new_out = models.DateTimeField(null=True, blank=True)
    new_lunch = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['at']


class _Decided(models.Model):
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    note = models.CharField(max_length=300, blank=True, help_text="The employee's note.")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)

    class Meta:
        abstract = True


class TimeOff(_Decided):
    """Days away from work. `days` is the exact list of dates (the scheduled working days in the range when it was asked for), each worth a
    vacation day of 8 hours. Vacation and sick days come out of the vacation balance and are paid days; 'other' takes nothing and pays nothing."""
    class Kind(models.TextChoices):
        VACATION = 'vacation', 'Vacation'
        SICK = 'sick', 'Sick'
        OTHER = 'other', 'Other'

    profile = models.ForeignKey(TimeProfile, on_delete=models.CASCADE, related_name='time_off')
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.VACATION)
    days = models.JSONField(default=list)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField()
    asked_by_employee = models.BooleanField(default=True)

    class Meta:
        ordering = ['start_date']

    @property
    def dates(self):
        from datetime import date
        return [date.fromisoformat(d) for d in self.days]

    @property
    def hours(self):
        return len(self.days) * policy.DAY_HOURS

    @property
    def takes_balance(self):
        return self.kind in (self.Kind.VACATION, self.Kind.SICK)


class ScheduleChange(_Decided):
    """A request to work a different day: stop working `drop_date`, work `add_date` instead (either may be empty)."""
    profile = models.ForeignKey(TimeProfile, on_delete=models.CASCADE, related_name='schedule_changes')
    drop_date = models.DateField(null=True, blank=True)
    add_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']


class PunchRequest(_Decided):
    """An employee's request to correct (or add) a day's punch. Times are local clock times; a blank keeps what is there."""
    profile = models.ForeignKey(TimeProfile, on_delete=models.CASCADE, related_name='punch_requests')
    day = models.DateField()
    clock_in = models.TimeField(null=True, blank=True)
    clock_out = models.TimeField(null=True, blank=True)
    lunch_minutes = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['day']


class HolidayPay(models.Model):
    """A manager's decision on holiday pay for one hourly employee and one holiday. No row yet means the decision is still waiting."""
    profile = models.ForeignKey(TimeProfile, on_delete=models.CASCADE, related_name='holiday_pay')
    day = models.DateField()
    name = models.CharField(max_length=60)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPROVED)
    hours = models.DecimalField(max_digits=4, decimal_places=2, default=policy.DAY_HOURS)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(auto_now_add=True)
    decision_note = models.CharField(max_length=300, blank=True)

    class Meta:
        unique_together = [('profile', 'day')]
        ordering = ['day']


class PayPeriodLock(models.Model):
    """A pay period a manager has marked as entered in payroll: its punches and decisions are locked until it is reopened."""
    start = models.DateField(unique=True)
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    locked_at = models.DateTimeField(auto_now_add=True)


class PolicySignature(models.Model):
    """An employee's signature on one version of the policy, with the exact text they were shown (so a later copy matches what they signed)."""
    profile = models.ForeignKey(TimeProfile, on_delete=models.CASCADE, related_name='signatures')
    version = models.PositiveSmallIntegerField()
    typed_name = models.CharField(max_length=120)
    signed_at = models.DateTimeField(auto_now_add=True)
    ip = models.CharField(max_length=45, blank=True)
    text = models.TextField(blank=True)

    class Meta:
        ordering = ['-signed_at']


class OfficeConnection(models.Model):
    """A public internet address that counts as "in the office" (the office wifi). Clock-ins from anywhere else are marked remote."""
    ip = models.CharField(max_length=45, unique=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    added_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.ip
