"""Working days: Monday to Friday, less the major US holidays (the federal ones). Used to give each month-end close step its date."""
from datetime import date, timedelta


def _nth_weekday(year, month, weekday, n):
    """The nth (1-based) given weekday (Monday = 0) of a month."""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    """A holiday on a Saturday is observed the Friday before, on a Sunday the Monday after."""
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def us_holidays(year):
    """The days off: New Year's, Martin Luther King, Presidents, Memorial, Juneteenth, Independence, Labor, Columbus, Veterans, Thanksgiving and Christmas (observed days included)."""
    days = {
        _observed(date(year, 1, 1)), _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3), _last_weekday(year, 5, 0), _observed(date(year, 6, 19)), _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 10, 0, 2), _observed(date(year, 11, 11)), _nth_weekday(year, 11, 3, 4), _observed(date(year, 12, 25)),
    }
    days.add(_observed(date(year + 1, 1, 1)))          # New Year's of next year can be observed on Dec 31
    return days


def is_workday(day):
    return day.weekday() < 5 and day not in us_holidays(day.year)


def workday_of_month(year, month, n):
    """The date of the nth working day of a month (1 is the first)."""
    day, count = date(year, month, 1), 0
    while True:
        if is_workday(day):
            count += 1
            if count == n:
                return day
        day += timedelta(days=1)
