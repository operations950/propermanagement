"""The time clock, vacation and holiday policy as numbers and dates (the text an employee reads and signs is in policy_text.py).

Everything here is plain arithmetic with no database, so the rules can be read, and tested, in one place."""
from datetime import date, timedelta

POLICY_VERSION = 1
POLICY_START = date(2026, 10, 26)       # the policy begins with the pay period that starts this Monday; everyone starts at zero
PAY_ANCHOR = date(2026, 10, 12)         # a pay period starts on this Monday and every 14 days after it
DAY_HOURS = 8                           # a vacation day is eight hours
YEAR_HOURS = 2080                       # a full-time year: 40 hours x 52 weeks
LATE_GRACE_MINUTES = 10      # a clock-in more than this many minutes after the scheduled start is late

WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def entitlement_days(year):
    """Vacation days for year `year` of employment: 10 in the first year, one more each year after (no cap)."""
    return 9 + max(1, year)


def entitlement_hours(year):
    return entitlement_days(year) * DAY_HOURS


def anniversary(hire, years):
    """The date `years` full years after the hire date (Feb 29 becomes Mar 1 in a year with no Feb 29)."""
    try:
        return hire.replace(year=hire.year + years)
    except ValueError:
        return date(hire.year + years, 3, 1)


def year_of_employment(hire, on):
    """1 on and after the hire date until the first anniversary, then 2, and so on."""
    if on < hire:
        return 1
    year = 1
    while anniversary(hire, year) <= on:
        year += 1
    return year


def pay_period(on):
    """(first day, last day) of the pay period holding this date."""
    start = PAY_ANCHOR + timedelta(days=14 * ((on - PAY_ANCHOR).days // 14))
    return start, start + timedelta(days=13)


def week_start(on):
    return on - timedelta(days=on.weekday())


def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def holidays(year):
    """The seven days the office is closed, as {date: name}. A holiday on a weekend stays on that weekend day: no substitute day."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return {
        date(year, 1, 1): "New Year's Day",
        _last_weekday(year, 5, 0): 'Memorial Day',
        date(year, 7, 4): 'Fourth of July',
        _nth_weekday(year, 9, 0, 1): 'Labor Day',
        thanksgiving: 'Thanksgiving',
        thanksgiving + timedelta(days=1): 'Day after Thanksgiving',
        date(year, 12, 25): 'Christmas',
    }


def holiday_name(on):
    return holidays(on.year).get(on)


def next_holidays(on, count=3):
    found = []
    for year in (on.year, on.year + 1):
        found += [(d, n) for d, n in holidays(year).items() if d >= on]
    return sorted(found)[:count]


def parse_days(text):
    return sorted({int(x) for x in (text or '').split(',') if x.strip().isdigit() and 0 <= int(x) <= 6})


def days_label(days):
    """'Mon-Fri' for a straight run, otherwise the names: 'Mon, Tue, Thu, Fri, Sat'."""
    days = sorted(days)
    if not days:
        return 'none'
    if len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        return f'{WEEKDAYS[days[0]]}–{WEEKDAYS[days[-1]]}'
    return ', '.join(WEEKDAYS[d] for d in days)


def minutes_label(minutes):
    """7h 10m"""
    minutes = int(minutes or 0)
    return f'{minutes // 60}h {minutes % 60:02d}m'


def hours_label(hours):
    """38.50"""
    return f'{float(hours):.2f}'


def short_day(d):
    """Tue Dec 29"""
    return f'{d:%a %b} {d.day}'


def clock_label(t):
    """9:00 (no leading zero, no AM/PM)"""
    return f'{t.hour % 12 or 12}:{t.minute:02d}'
