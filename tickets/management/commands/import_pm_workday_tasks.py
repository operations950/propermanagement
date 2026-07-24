"""One-time import of the real property-manager monthly ops checklist
(pasted by the user 2026-07-21) as recurring TicketTemplates.

The source list is expressed as "Working Day N of the month" (a business-day
position, not a fixed calendar date — see workday_of_month on TicketTemplate
and next_workday_occurrence() in generate_recurring_tickets.py); the original
request illustrated it against August 2026, but the concept is evergreen.
next_run_date is seeded to each task's date in the CURRENT real month (not a
fixed month), with skip_missed=True — so a working day already passed this
month rolls straight to next month without backfilling a stale "overdue"
ticket, while a working day still ahead (including today) fires normally.
generate_recurring_tickets recomputes the correct date fresh every month
after that.

Working Day 14 had no tasks listed and is intentionally skipped. These are
company-wide bookkeeping/ops tasks (not tied to one specific address), so
they're created with target_type=COMPANY — a single occurrence per period,
not fanned out to every active property.

Idempotent — safe to re-run, keyed by title (get_or_create) so it won't
duplicate. Only sets next_run_date/skip_missed on first creation — re-running
this after templates already exist won't rewind their schedule.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import StaffProfile
from tickets.management.commands.generate_recurring_tickets import nth_business_day
from tickets.models import Frequency, Priority, TicketTemplate

# (workday_of_month, title, description)
TASKS = [
    (1, 'Monthly bookkeeping: paste data & QB entry', (
        '- Paste Airbnb/VRBO/offline data into the QB Excel workbook.\n'
        '- Complete QuickBooks bookkeeping for: General Escrow Account, Amex, Checking, Chase CC, '
        'Property Management.'
    )),
    (2, 'Monthly reconciliations & trust ledger', (
        '- Download Seacoast statements.\n'
        '- Complete Property Management reconciliation.\n'
        '- Complete General Escrow reconciliation.\n'
        '- Complete QB Miscellaneous Property Trust Ledger.\n'
        '- Run prior-month Balance Sheet and download all Property Management accounts; paste '
        'liabilities into the QB workbook.\n'
        '- Run prior-month Income Statement and download all expenses; paste expenses into the QB '
        'workbook.\n'
        '- Run 1 - ReimbursableExpenses.\n'
        '- Run 2 - CreateTrustRecons.\n'
        '- Run 3 - CreateIncomeRecons.'
    )),
    (3, 'Financial reports, property review & owner payments', (
        '- Run 4 - CreateFinReports.\n'
        '- Run 5 - UpdateConsolidateFiles.\n'
        '- Review and analyze financials for: 803 NE 7th, 111 NW 1st Ave, 712 NE 8th, 300 Ross Dr, '
        '2036 Alta Meadows, 100 Neptune, 716 Kittyhawk, 4606 Brady, 800 Tropic A, 800 Tropic B, '
        '800 Tropic C, 800 Tropic D, 323 Decarie, 224 NW 4th, 702 SW 1st, 1745 Palm Cove, '
        '2919 Cormorant Rd.\n'
        '- Run 6 - ConsolidatedFinancials.\n'
        '- Process owner payments through Seacoast for: Justin Browne, Joe Lynn, Lee Ry, David & '
        'Margaret Pennes, Hurricane Properties, Jed Kanner, 4KC LLC / James Kai, ANNE, Mike, Carolina, '
        'Philip Henry.\n'
        '- Collect payment from Calvin.\n'
        '- Process Proper Realty commission.\n'
        '- Process Proper Realty expense reimbursement.'
    )),
    (4, 'Rent confirms, AP, tax filings & card payments', (
        '- Confirm rent was received for: 2036 Alta Meadows (Jan/Feb renewal conversation), 300 Ross '
        '(Mar/Apr renewal conversation), 1748 Palm Cove (Jan/Feb renewal conversation), 325 3rd Ave '
        '— Residential.\n'
        '- Email owners their monthly statements and highlight abnormal items.\n'
        '- Process Accounts Payable for: La Pensee, St Andrews, Abrams.\n'
        '- Make Amex payment — must be paid by the 15th.\n'
        '- Make Chase payment — must be paid by the 18th.\n'
        '- File Florida Sales Tax returns for: Proper Realty, Jed Kanner, JLLS LLC, DEL ALLEY LLC, '
        '381 BLUE LLC.\n'
        '- File PBC TDT returns, even if zero, for: 803 NE 7th, 826 N Ocean Breeze, 712 NE 8th, '
        '716 Kittyhawk, 100 Neptune, 4606 Brady, 111 NW 1st, 18 NW 12th, 323 Decarie St, '
        '800 Tropic Blvd, Blue Heaven, Kelvin\'s, PHIL, 2821 Frederick.\n'
        '- Pay all PBC TDT tax returns.'
    )),
    (5, 'La Pensee financials, late letters & collections', (
        '- Prepare La Pensee monthly financial statements.\n'
        '- Upload La Pensee financials to Condo Cafe.\n'
        '- Email La Pensee financials to the board.\n'
        '- Send La Pensee late letters.\n'
        '- Collect/process payments: La Pensee ($3,750 check), 324 Arts LLC ($1,000 check), Del Park '
        'LLC ($850 check), 381 Red Barn LLC ($550 check), Qar Qube LLC ($200 check), Sky Dining LLC '
        '($300 check), St. Andrews Grand ($1,400 check), Ken & Rita / Laing ($150 via Venmo to Ana — '
        'confirm), Ken & Rita / Harmon Ct ($150 via Venmo to Ana — confirm), Michelle Kaplan (collect '
        '$500 check), Lakeside #8 ($450 check), Lakeside #2 ($500 check), St. Andrews Glenn ($1,100 '
        'check), Linton Woods ($500).'
    )),
    (6, 'St Andrews financials, insurance & vendor checks', (
        '- Prepare St Andrews monthly financial statements.\n'
        '- Upload St Andrews financials to Condo Cafe.\n'
        '- Email St Andrews financials to the board.\n'
        '- Send St Andrews late letters.\n'
        '- Pay owner for 2036 Alta Meadows / Jan Fuchs via Seacoast ACH — only if tenant has paid.\n'
        '- Pay Abrams insurance: Tower Hill Insurance, Liberty Mutual Insurance, Berkley Aspire.\n'
        '- Write checks: 300 Ross ($150 to Felipe Castaneda), 100 Neptune ($200 to Felipe Castaneda), '
        '803 NE 7th ($80 to Felipe Castaneda), 323 Decarie ($130 to Felipe Castaneda), 712 NE 8th '
        '($120 to Misael Garcia), Office rent ($2,275.69 to 1045 E Atlantic Inc).'
    )),
    (7, 'Renew FL elevator entities via Sunbiz', (
        '- Renew Florida Elevator entity for 324 West through Sunbiz.\n'
        '- Renew Florida Elevator entity for 324 East through Sunbiz.\n'
        '- Renew Florida Elevator entity for La Pensee through Sunbiz.'
    )),
    (8, 'Lakeside/St Andrews Glen financials & rent confirms', (
        '- Lakeside #2: prepare monthly financial statements, upload to Condo Cafe, email to the '
        'board.\n'
        '- Lakeside #8: prepare monthly financial statements, upload to Condo Cafe, email to the '
        'board.\n'
        '- St Andrews Glen: prepare monthly financial statements, upload to Condo Cafe, email to the '
        'board.\n'
        '- Confirm rent was received for: 324 NE 3rd — Miralain, 324 NE 3rd — JRI, 324 NE 3rd — '
        'Dandelight, 324 NE 3rd — Delray Energy, 324 NE 3rd — Rooftop, 325 NE 3rd — Bedner\'s, '
        '325 NE 3rd — Glimmer Cafe.\n'
        '- Complete monthly financial statements for: 324 NE 3rd, 325 NE 3rd, 381 NE 3rd.\n'
        '- Confirm prior-month Delray Beach Water autopay went through for: 325 Decarie, 323 Decarie, '
        '803 NE 7th.\n'
        '- Complete QuickBooks bookkeeping for all accounts.'
    )),
    (9, 'Record owner payments in Yardi', (
        '- Record La Pensee owner payments in Yardi from the bank and apply prepaids.\n'
        '- Record St Andrews owner payments in Yardi from the bank and apply prepaids.'
    )),
    (10, 'Late fees & Accounts Payable', (
        '- Apply late fees for La Pensee.\n'
        '- Apply late fees for St Andrews.\n'
        '- Process Accounts Payable for: La Pensee, St Andrews, Abrams.'
    )),
    (11, 'Sunbiz reconciliation', (
        '- Complete Sunbiz reconciliation and confirm all association information is up to date.'
    )),
    (12, 'Send tenant invoices via Yardi', (
        '- Send tenant invoices through Yardi.'
    )),
    (13, 'Send late letter to Lamen', (
        '- Send late letter to Lamen.'
    )),
    # Working Day 14 had no tasks listed — intentionally skipped.
    (15, 'Record owner payments — La Pensee & St Andrews', (
        '- Record owner payments for La Pensee.\n'
        '- Record owner payments for St Andrews.'
    )),
    (16, 'Apply interest — La Pensee & St Andrews', (
        '- Apply interest for La Pensee.\n'
        '- Apply interest for St Andrews.'
    )),
    (17, 'Post monthly receivables in Yardi', (
        '- Post monthly commercial receivables in Yardi.\n'
        '- Post monthly association receivables in Yardi.'
    )),
    (18, 'Board checklist, vendor & lawyer follow-up', (
        '- Review the board meeting checklist.\n'
        '- Confirm all vendors were paid.\n'
        '- Follow up with the lawyer regarding delinquent owners.'
    )),
]


class Command(BaseCommand):
    help = 'Import the real property-manager monthly working-day task checklist as TicketTemplates.'

    def handle(self, *args, **options):
        today = timezone.localdate()
        created = 0
        for workday, title, description in TASKS:
            due = nth_business_day(today.year, today.month, workday)
            if due is None:
                raise CommandError(f'No business day #{workday} in {today.year}-{today.month:02d}')
            _, was_created = TicketTemplate.objects.get_or_create(
                title=title,
                defaults={
                    'description': description,
                    'target_type': TicketTemplate.TargetType.COMPANY,
                    'frequency': Frequency.MONTHLY_WORKDAY,
                    'workday_of_month': workday,
                    'next_run_date': due,
                    'skip_missed': True,
                    'default_assigned_role': StaffProfile.Role.PROPERTY_MANAGER,
                    'default_priority': Priority.MEDIUM,
                },
            )
            if was_created:
                created += 1
                self.stdout.write(f'Working Day {workday}: {title} (first due {due})')

        self.stdout.write(self.style.SUCCESS(f'Created {created} new TicketTemplate(s).'))
