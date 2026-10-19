"""The policy an employee reads and signs, as data so the screen and the downloadable copy say exactly the same thing.

Each section is (heading, [items]); an item is a string, or (who, string) where who is 'salaried' or 'hourly' and the line shows only to that kind of employee.
Change any wording here, then raise POLICY_VERSION in policy.py: everyone is asked to read and sign again."""
from . import policy

TITLE = 'Our time clock, vacation and holiday policy'
EFFECTIVE = 'Effective Monday, October 26, 2026'

SHORT = [
    '<strong>Clock in when you arrive, clock out when you leave.</strong> When you clock out we ask whether you took a lunch and how long it was. Lunch is not paid.',
    '<strong>You earn vacation as you work:</strong> 10 days in your first year, 11 in your second, 12 in your third, and one more day each year after that.',
    ('salaried', '<strong>You are salaried:</strong> your pay never changes with your hours, and you earn vacation at a steady rate, as if you work 40 hours every week.'),
    ('hourly', '<strong>You are hourly:</strong> you are paid for the hours in the time clock, and you earn vacation in proportion to the hours you actually work.'),
    ('salaried', '<strong>The office is closed on seven holidays</strong> and you are paid for them.'),
    ('hourly', '<strong>The office is closed on seven holidays.</strong> Holiday pay for hourly employees has to be approved by a manager each time.'),
    '<strong>Your schedule is in the system.</strong> To work a different day than usual, ask first so everyone knows who is working.',
    '<strong>Everyone starts at zero</strong> on October 26. Nothing before that date counts for or against you.',
]

SECTIONS = [
    ('Clocking in and out', [
        'Clock in in the morning and clock out at the end of your day. You do not clock out for lunch.',
        'When you clock out you will be asked: <em>Did you take a lunch? How long?</em> We rely on your honesty. The time you tell us is taken out of your hours. '
        'You may work through lunch if you prefer; just answer “no lunch” when you clock out.',
        'If you are not in the office, you can still clock in. It is marked as a remote clock-in and you give a short reason.',
        'Forgot to clock out? Tell a manager. A manager fixes the time and writes down why. The original punch is kept.',
        ('hourly', 'For you, the hours in the time clock <strong>are</strong> your pay: a missed or wrong punch changes what you are paid until a manager corrects it.'),
        ('salaried', 'For you, the time clock is for attendance. Working 38 or 45 hours in a week does not change your pay.'),
        ('example', '<strong>Example:</strong> your day is 9 to 5. You arrive at 8:50, take a one-hour lunch, and leave at 5:00. You worked 7 hours 10 minutes that day.'),
    ]),
    ('Your schedule', [
        'Your regular schedule is in the system, and everyone can see on a calendar who is supposed to be working each day.',
        ('salaried', '<strong>{days}, 9:00 to 5:00.</strong>'),
        ('hourly', '<strong>{days}, 9:00 to 5:00.</strong>'),
        'Want to move a day (work Wednesday instead of Saturday, for example)? <strong>Request it in the portal first.</strong> Once a manager approves, the calendar changes.',
    ]),
    ('Vacation', [
        'The number of days you can earn in a year depends on how long you have worked here (counted from your hire date, and it goes up on each anniversary).',
        ('table', None),
        ('salaried', 'You earn at a steady rate every week, as if you work 40 hours, whatever your hours actually were.'),
        ('hourly', 'You earn based on the hours you actually work, up to that year’s days. Part-time weeks earn proportionally less.'),
        'A vacation day is 8 hours. You can see what you have earned, used and have scheduled on your dashboard.',
        'Ask for vacation in the portal. A manager approves every request, and a manager may approve one even if you haven’t earned the time yet. Approved days are paid days.',
        'If you take time you haven’t earned, your balance goes below zero and you earn it back as you work. Don’t expect more to be approved until it is back up.',
        'If you miss a scheduled day or call in sick, that day is taken from your vacation balance, and it can take the balance below zero. '
        'A balance that stays negative is something a manager will talk with you about.',
        'Unused vacation carries over to the next year.',
    ]),
    ('Holidays', [
        'The office is closed on: <strong>New Year’s Day, Memorial Day, the Fourth of July, Labor Day, Thanksgiving, the day after Thanksgiving, and Christmas.</strong>',
        ('salaried', 'You do not work those days and you are paid as usual. No vacation is taken.'),
        ('salaried', 'If a holiday falls on a weekend, you are not scheduled to work weekends anyway: there is no substitute day off and no extra vacation day.'),
        ('hourly', 'Holiday pay for hourly employees is <strong>approved by a manager</strong> each time. It is the manager’s decision. '
                   'The manager considers your hours that week, whether the office being closed cost you work, and how you are doing at your job.'),
        ('hourly', 'This is true even if the holiday falls on a Saturday or Sunday you would have worked.'),
    ]),
    ('A fresh start', [
        'This policy begins with the pay period that starts <strong>Monday, October 26, 2026</strong>. Everyone begins with <strong>zero</strong> vacation hours. '
        'We are not counting any vacation you took before that date.',
    ]),
]


def vacation_table():
    """Rows for the vacation table: year label, days, hours, hours earned per week (salaried), hours worked per hour earned (hourly)."""
    rows = []
    for year in (1, 2, 3):
        hours = policy.entitlement_hours(year)
        rows.append({
            'year': f'Year {year}', 'days': policy.entitlement_days(year), 'hours': hours,
            'weekly': f'{hours / 52:.2f} hours', 'per': f'{policy.YEAR_HOURS / hours:.1f}'.rstrip('0').rstrip('.') + ' hours worked',
        })
    rows.append({'year': 'Year 4 and after', 'days': '13, 14, 15…', 'hours': '+8 each year', 'weekly': '', 'per': ''})
    return rows


def lines(items, pay_type, days_text):
    """The items one kind of employee sees, as [(kind, html)] where kind is '', 'example' or 'table'."""
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(('', item))
            continue
        who, text = item
        if who == 'table':
            out.append(('table', ''))
        elif who == 'example':
            out.append(('example', text))
        elif who == pay_type:
            out.append(('', text.replace('{days}', days_text)))
    return out


def for_employee(pay_type, days_text):
    """The whole policy for one employee: short version + sections, each a list of (kind, html)."""
    return {
        'title': TITLE, 'effective': EFFECTIVE, 'version': policy.POLICY_VERSION,
        'short': lines(SHORT, pay_type, days_text),
        'sections': [(heading, lines(items, pay_type, days_text)) for heading, items in SECTIONS],
        'table': vacation_table(), 'pay_type': pay_type,
    }
