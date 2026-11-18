"""THE tree of screens: the one place that says where every screen sits, and so where its Up link may go (see core/nav.py for how it is used).

Each entry gives a screen's url name, what Up says when it points AT that screen (its label), the screens it is REACHED FROM (its parents: only wider screens), what kind of screen
it is, and (when it cannot be worked out) where Up goes when the person has no trail (a bookmark, an emailed link): its usual parent.

  kind WIDE    a list, a hub, a checklist, a dashboard: something an Up link may point at.
  kind DETAIL  the bottom level: one ticket, one property, one contact, one unit. Up never points at a detail screen, except from the forms and pages that hang off that very record.
  kind FORM    an edit form, a preview or a print page that belongs to one record: it may go up to that record's detail screen (and to the wide screens above it).

The parents a screen's Up link may use are NOT written per screen any more. They are its ancestors in this tree: its parents, their parents, and so on. A screen written down here is
therefore automatically a possible Up for everything beneath it, and a screen that sits BESIDE another one (two steps of a checklist) is never an ancestor of it, so Up never goes sideways.
A screen that can be reached from two wide screens (late fees: from the month-end close or from the Accounting hub) lists both; the trail then picks the one the person came from.

To add a screen: add one line here (and a label, if it is a wide screen others sit under). core/test_nav.py walks the whole tree and fails on a cycle, a detail screen used as a parent
of a wide or detail screen, a wide screen with no label, and a screen that its view needs but nobody registered.

Labels are a string, or a function of (request, ctx) for the few that depend on the record (an association's own word for "Units"). A parent written as (name, label) says something
different on that one link than the screen's own label does.
"""
from .nav import D, Up

WIDE, DETAIL, FORM = 'wide', 'detail', 'form'


class Screen:
    __slots__ = ('name', 'label', 'parents', 'kind', 'usual')

    def __init__(self, name, label, parents, kind, usual):
        self.name, self.label, self.kind, self.usual = name, label, kind, usual
        self.parents = tuple((p, None) if isinstance(p, str) else (p[0], p[1]) for p in parents)


SCREENS = {}


def screen(name, label=None, parents=(), kind=WIDE, usual=None):
    if name in SCREENS:
        raise ValueError(f'{name} is in the nav tree twice')
    SCREENS[name] = Screen(name, label, parents, kind, usual)


def many(names, parents, kind=WIDE, usual=None, label=None):
    """Several screens with the same parents (a family of steps under one hub)."""
    for name in names:
        screen(name, label, parents, kind, usual)


# ---------------------------------------------------------------- what people already call these screens
CHECKLIST = 'Month-end checklist'
CLOSE_RENTALS = 'Rentals'
ADMIN = 'Admin Tools'
ONSITE = 'On-Site Visits'
TURNOVERS = 'Turnovers'
TICKETS = 'Tickets'
PROPERTIES = 'Properties'
CONTACTS = 'Contacts'
FINANCIALS = 'Financials'
COMPLIANCE = 'Compliance'
PROJECTS = 'Projects'
DASH = 'Dashboard'
HOME = 'Home'


def _new_face(request):
    profile = getattr(getattr(request, 'user', None), 'staff_profile', None)
    return bool(profile and profile.new_face)


def _session_home(request):
    """A recurring item's usual parent: Compliance for the new layout, My Recurring for the original."""
    return D('ws_compliance', COMPLIANCE) if _new_face(request) else D('my_sessions', 'My Recurring')


def _rules_home(request):
    return D('ws_compliance', 'Compliance rules', fixed={'tab': 'rules'}) if _new_face(request) else D('session_template_list', 'Recurring Rules')


def _word(code, fallback):
    """The association's own word for a screen (Units, Charges ...), when the view has said which association it is."""
    def label(request, ctx):
        prop = (ctx or {}).get('prop')
        if prop is None:
            return fallback
        from accounting import labels
        return labels.label(prop, code)
    return label


LINE_LABELS = {'associations': 'Associations', 'short-term-rentals': 'Short-term rentals', 'long-term-rentals': 'Long-term rentals', 'commercial': 'Commercial'}


def _line_label(request, ctx, entry):
    """The line-of-business page says which line it is: the trail entry's own address names it."""
    match = getattr(request, 'resolver_match', None)
    slug = (entry or {}).get('slug') or (match.kwargs.get('slug') if match and match.view_name == 'ws_line' else '')
    return LINE_LABELS.get(slug, 'Lines of business')


_line_label.uses_entry = True


def _associations_page(request):
    """Where Up goes from Legal forms with no trail: the Associations page it is a card on."""
    from django.urls import reverse
    return Up(reverse('ws_line', args=['associations']), 'Associations')


# ================================================================ the top: where a person starts
screen('ws_home', HOME)
screen('ws_line', _line_label)                       # Associations, Short-term rentals, Long-term rentals, Commercial: the cards for each line of business
screen('dashboard', DASH)
screen('ws_week', 'My week')
screen('ws_compliance', COMPLIANCE)
screen('ws_projects', PROJECTS)
screen('ws_financials', FINANCIALS)
screen('admin_tools', ADMIN)
screen('ws_admin', 'Admin')
screen('property_list', PROPERTIES)
screen('contact_list', CONTACTS)
screen('ticket_list', TICKETS)
screen('onsite_dashboard', ONSITE)
screen('onsite_turnovers', TURNOVERS)
screen('my_sessions', 'My Recurring')
screen('session_template_list', 'Recurring Rules')
screen('portfolio_dashboard', 'My Businesses')
screen('supplies:cart', "Today's cart")
screen('process_template_list', 'Process templates')
screen('timeclock_home', 'My time')
screen('timeclock_hours', 'Hours')
screen('legalforms:list', 'Documents', parents=['ws_line', 'ws_compliance'], usual=_associations_page)
screen('accounting:home', 'Associations')
screen('accounting:budgets', 'Budgets')

# ================================================================ Admin tools
many(['bot_keys', 'property_data_import', 'quickbooks_accounts', 'staff_create'], ['admin_tools'], usual=D('admin_tools', ADMIN))
screen('owner_portal_admin', parents=['close_overview', 'admin_tools', 'ws_line'], usual=D('close_overview', CLOSE_RENTALS))
screen('ws_people', parents=['ws_admin'], usual=D('ws_admin', 'Admin'))

# ================================================================ The company month-end close: every step goes up to the checklist (and keeps its month)
_CLOSE_USUAL = D('close_home', CHECKLIST, query=('month',))
screen('close_home', CHECKLIST)
for _name, _label in (('close_bank_tieout', 'Bank tie-out'), ('close_escrow', 'Escrow account check'), ('close_misc', 'Misc trust check'), ('close_pay_owners', 'Pay owners'),
                      ('close_pay_proper', 'Pay Proper'), ('close_pay_ltr_owners', 'Pay long-term owners'), ('close_rent_collection', 'Rent collection'), ('close_sales_tax', 'Sales tax'),
                      ('tourist_tax', 'Tourist tax'), ('close_payouts', 'Platform payouts'), ('commercial:renewals', 'Renewals')):
    screen(_name, _label, ['close_home', 'ws_financials', 'ws_line'], usual=_CLOSE_USUAL)
screen('close_overview', CLOSE_RENTALS, parents=['close_home', 'ws_financials', 'ws_line'], usual=_CLOSE_USUAL)
screen('close_long_term', 'Long-term rentals', parents=['close_home', 'ws_financials', 'ws_line'], usual=_CLOSE_USUAL)
# one rental's month, and one unit's month inside it (their labels are the rental's own name: the views say it)
screen('close_property', 'This rental', parents=['close_overview', 'close_long_term', 'close_home'])
screen('close_unit', 'This unit', parents=['close_property'])
screen('close_statement', 'Statement', parents=['close_property'])                       # a rental's statement: its months, one page each
screen('close_month_statement', parents=['close_statement'], kind=DETAIL)
screen('close_pay_booking', parents=['close_pay_owners', 'close_pay_proper', 'close_pay_ltr_owners'])

# ================================================================ Properties and contacts
many(['missing_documents', 'faq_review_queue', 'property_create', 'property_edit'], ['property_list', 'ws_line'], usual=D('property_list', PROPERTIES))
screen('property_detail', 'Property', kind=DETAIL, usual=D('property_list', PROPERTIES), parents=[
    'property_list', 'ws_line', 'ticket_list', 'dashboard', 'ws_home', 'ws_compliance', 'ws_projects', 'ws_financials', 'close_home', 'close_overview', 'close_long_term', 'accounting:home',
    'contact_list', 'onsite_dashboard', 'onsite_turnovers', 'onsite_performance', 'onsite_reservation_list'])
screen('contact_duplicates', 'Duplicate contacts')
screen('contact_review', 'Contact review')
screen('contact_detail', 'Contact', kind=DETAIL, usual=D('contact_list', CONTACTS), parents=['contact_list', 'contact_duplicates', 'contact_review'])
screen('contact_create', parents=['contact_list'], usual=D('contact_list', CONTACTS))
screen('contact_edit', parents=['contact_detail', 'contact_list'], kind=FORM, usual=D('contact_detail', 'Contact', args=('pk',)))
screen('commercial:ltr_leases', 'Leases', parents=['property_detail'], kind=FORM, usual=D('property_detail', 'Property', args=('pk',)))
screen('commercial:ltr_lease', parents=['commercial:ltr_leases'], kind=FORM, usual=D('commercial:ltr_leases', 'Leases', args=('pk',)))

# ================================================================ On-Site
screen('onsite_calendar', 'Calendar', parents=['onsite_dashboard', 'ws_home'], usual=D('onsite_dashboard', ONSITE))
many(['onsite_booking_feeds', 'onsite_checklist_custom_items', 'onsite_checklist_templates', 'onsite_cleaner_activity', 'onsite_cleaning_payments', 'onsite_visit_create',
      'onsite_visit_rule_list'], ['onsite_dashboard', 'ws_home', 'ws_line'], usual=D('onsite_dashboard', ONSITE))
screen('onsite_visit_detail', parents=['onsite_dashboard', 'onsite_calendar', 'onsite_turnovers', 'ws_home', 'ws_week'], kind=DETAIL, usual=D('onsite_dashboard', ONSITE))
screen('onsite_checklist_template_detail', parents=[('onsite_checklist_templates', 'Checklist templates')], usual=D('onsite_checklist_templates', 'Checklist templates'))
screen('onsite_booking_import', 'Import payouts', parents=['close_payouts', 'onsite_dashboard'], usual=D('close_payouts', 'Platform payouts'))
screen('onsite_booking_import_preview', parents=['onsite_booking_import'], usual=D('onsite_booking_import', 'Import payouts'))
screen('onsite_feeds_recalibrate', parents=[('onsite_booking_feeds', 'Booking calendars')], usual=D('onsite_booking_feeds', 'Booking calendars'))
screen('onsite_performance', 'Rental performance', parents=['onsite_turnovers', 'ws_line'], usual=D('onsite_turnovers', TURNOVERS))
many(['onsite_rentals_calendar'], ['onsite_turnovers', 'ws_line'], usual=D('onsite_turnovers', TURNOVERS))
screen('onsite_reservation_list', 'Reservations', parents=['onsite_turnovers', 'ws_line'], usual=D('onsite_turnovers', TURNOVERS))
screen('onsite_performance_property', 'Property performance', parents=['onsite_performance'], kind=DETAIL, usual=D('onsite_performance', 'Rental performance'))
screen('onsite_performance_calendar', parents=['onsite_performance_property', 'onsite_performance'], kind=FORM,
       usual=D('onsite_performance_property', 'Property performance', args={'property_id': 'property_id'}))
screen('onsite_reservation_review', 'Reservations to review', parents=['onsite_reservation_list', 'ws_line'], usual=D('onsite_reservation_list', 'Reservations'))
screen('onsite_reservation_create', parents=['onsite_reservation_list'], usual=D('onsite_reservation_list', 'Reservations'))
screen('onsite_reservation_edit', parents=['onsite_reservation_list', 'onsite_reservation_review'], usual=D('onsite_reservation_list', 'Reservations'))

# ================================================================ Tickets
screen('department_dashboard', DASH, parents=[('dashboard', 'Main dashboard')], usual=D('dashboard', 'Main dashboard'))
screen('ticket_detail', 'Back to ticket', kind=DETAIL, parents=['ticket_list', 'dashboard', 'department_dashboard', 'ws_home', 'ws_week', 'ws_compliance', 'ws_projects'])
screen('ticket_create', parents=['ticket_list', 'dashboard', 'department_dashboard', 'ws_home', 'ws_week', 'ws_compliance', 'ws_projects'], usual=D('ticket_list', 'All tickets'))
screen('ticket_previous_conversation', parents=['ticket_detail'], kind=FORM, usual=D('ticket_detail', 'Back to ticket', args=('pk',)))

# ================================================================ Time clock
many(['timeclock_policy', 'timeclock_change', 'timeclock_time_off', 'timeclock_fix'], [('timeclock_home', 'My time')], usual=D('timeclock_home', 'My time'))
screen('timeclock_punch', parents=[('timeclock_hours', 'Hours')], usual=D('timeclock_hours', 'Hours'))

# ================================================================ Compliance (recurring items), projects and processes
screen('session_detail', kind=DETAIL, usual=_session_home, parents=['ws_compliance', 'my_sessions', 'ws_home', 'ws_week'])
screen('session_template_detail', 'The rule', kind=DETAIL, usual=_rules_home, parents=[('ws_compliance', 'Compliance rules'), 'session_template_list'])
screen('session_template_edit', parents=['session_template_detail', ('ws_compliance', 'Compliance rules'), 'session_template_list'], kind=FORM, usual=_rules_home)
screen('session_template_create', parents=[('ws_compliance', 'Compliance rules'), 'session_template_list'], usual=_rules_home)
many(['process_template_create'], ['process_template_list'], usual=D('process_template_list', 'Process templates'))
screen('process_template_edit', 'Back to builder', parents=['process_template_list'], usual=D('process_template_list', 'Process templates'))
screen('process_template_preview', parents=['process_template_edit'], kind=FORM, usual=D('process_template_edit', 'Back to builder', args=('pk',)))
screen('ws_run_detail', kind=DETAIL, parents=['ws_projects', 'ws_home'], usual=D('ws_projects', 'Projects'))

# ================================================================ Portfolio, supplies
screen('portfolio_business_detail', 'Business', parents=['portfolio_dashboard'], usual=D('portfolio_dashboard', 'My Businesses'))
screen('portfolio_task_detail', kind=DETAIL, parents=['portfolio_business_detail'], usual=D('portfolio_business_detail', 'Business', args=('slug',)))
screen('supplies:order_detail', parents=['supplies:cart'], usual=D('supplies:cart', "Today's cart"))

# ================================================================ Documents (legal forms)
screen('legalforms:new', parents=['legalforms:list'])
screen('legalforms:detail', 'The document', kind=DETAIL, parents=['legalforms:list', 'accounting:collections'])
screen('legalforms:edit', parents=['legalforms:detail'], kind=FORM)

# ================================================================ Association books (accounting)
# The hub of one association, and the month-end close that sits beside it (neither is above the other).
screen('accounting:hub', 'Accounting', parents=['accounting:home', 'ws_financials'])
screen('accounting:close', 'Month-end close', parents=[('accounting:home', _word('close_steps', 'Month-end close')), 'close_home', 'accounting:hub'])
screen('accounting:close_review', parents=['accounting:close'])
screen('accounting:reserve_check', 'Reserve funds', parents=['accounting:close'])
screen('accounting:send_board', parents=['accounting:close'])
screen('accounting:units', _word('units_title', 'Units'), parents=['accounting:hub', 'accounting:close'])
screen('accounting:units_import', parents=['accounting:units'])
screen('accounting:charges', _word('charges_title', 'Charges'), parents=['accounting:hub', 'accounting:close'])
many(['accounting:charge_new', 'accounting:charge_edit'], ['accounting:charges'])
screen('accounting:shares', parents=['accounting:charges', 'accounting:units', 'accounting:hub'])
screen('accounting:unit', 'Unit', kind=DETAIL, parents=['accounting:units', 'accounting:charges', 'accounting:collections', 'accounting:delinquencies', 'accounting:report_aging',
                                                         'accounting:late_fees', 'accounting:hub'])
screen('accounting:owner', kind=DETAIL, parents=['accounting:units', 'accounting:report_aging', 'accounting:collections', 'accounting:hub'])
screen('accounting:charge_one', kind=FORM, parents=['accounting:unit', 'accounting:hub', 'accounting:collections', 'accounting:late_fees', 'accounting:report_aging'])
screen('accounting:receipt_new', kind=FORM, parents=['accounting:unit', 'accounting:units', 'accounting:hub', 'accounting:collections', 'accounting:report_aging', 'accounting:bank',
                                                      'accounting:close', 'commercial:lease'])
# collections: late fees is a step BESIDE owner collections, not above it
screen('accounting:collections_home', 'Owner collections', parents=['ws_financials', 'ws_line'])
screen('accounting:collections', 'Owner collections', parents=['accounting:hub', 'accounting:close', 'accounting:collections_home'])
screen('accounting:late_fees', 'Late fees and interest', parents=['accounting:close', 'accounting:hub'])
screen('accounting:delinquencies', 'Delinquencies', parents=['accounting:hub', 'accounting:close'])
screen('accounting:case', kind=DETAIL, parents=['accounting:collections', 'accounting:delinquencies'])
# budgets
screen('accounting:budget_project', 'Budget project', parents=['accounting:budgets', 'accounting:hub'])
screen('accounting:budget_year', parents=['accounting:budget_project', 'accounting:budgets'])
many(['accounting:budget_packet', 'accounting:budget_load'], ['accounting:budget_project'])
# money: bank, bills, checks
screen('accounting:bank', 'Bank reconciliation', parents=['accounting:close', 'accounting:hub', 'accounting:reports'])
screen('accounting:deposits', parents=['accounting:bank'])
screen('accounting:payables_home', 'Payables', parents=['ws_financials', 'ws_line'])
screen('accounting:bills', 'Bills', parents=['accounting:hub', 'accounting:close', 'accounting:payables_home'])
screen('accounting:bill', kind=DETAIL, parents=['accounting:bills', 'accounting:payables_home', 'accounting:checks', 'accounting:report_payables', 'accounting:bank', 'accounting:hub'])
screen('accounting:pay', parents=['accounting:bills'])
screen('accounting:checks', 'Checks', parents=['accounting:bills', 'accounting:hub', 'accounting:payables_home'])
screen('accounting:check_setup', parents=['accounting:checks'])
screen('accounting:contracts', 'Contracts', parents=['accounting:hub'])
screen('accounting:contracts_print', parents=['accounting:contracts'])
screen('accounting:recurring', 'Recurring', parents=['accounting:hub', 'accounting:close'])
screen('accounting:journal', 'Journal', parents=['accounting:hub', 'accounting:close'])
screen('accounting:master_chart', parents=['ws_financials', 'ws_line'])
screen('accounting:settings', parents=['accounting:hub', 'accounting:close', 'accounting:charges', 'accounting:collections', 'accounting:delinquencies', 'accounting:late_fees',
                                       'accounting:payables_home', 'accounting:reserve_check', 'accounting:recurring', 'accounting:checks'])
# reports (the bank reconciliation report is reached from the bank screen too)
screen('accounting:reports', 'Reports', parents=['accounting:hub', 'accounting:close'])
many(['accounting:report_tb', 'accounting:report_bs', 'accounting:report_pl', 'accounting:report_bva', 'accounting:report_is', 'accounting:report_gl', 'accounting:report_checks',
      'accounting:report_rec', 'accounting:budget'], ['accounting:reports', 'accounting:close', 'accounting:hub', 'accounting:bank'])
screen('accounting:report_aging', 'Aging', parents=['accounting:reports', 'accounting:close', 'accounting:hub', 'accounting:bank'])
screen('accounting:report_payables', 'Payables report', parents=['accounting:reports', 'accounting:close', 'accounting:hub', 'accounting:bank', 'accounting:bills', 'accounting:payables_home'])

# ================================================================ Commercial properties (the same books, with tenants)
screen('commercial:leases', 'Leases', parents=['accounting:hub', 'accounting:close', 'accounting:units', 'ws_financials'])
screen('commercial:lease', 'Tenant', kind=DETAIL, parents=['commercial:leases', 'commercial:invoices', 'commercial:cam', 'accounting:units', 'accounting:report_aging', 'accounting:charges',
                                                            'accounting:hub', 'accounting:close'])
many(['commercial:lease_new'], ['commercial:leases'])
screen('commercial:lease_edit', kind=FORM, parents=['commercial:lease', 'commercial:leases'])
screen('commercial:invoices', 'Tenant invoices', parents=['commercial:leases', 'accounting:hub', 'accounting:close'])
screen('commercial:owners', parents=['commercial:leases', 'accounting:hub', 'accounting:close'])
screen('commercial:cam', 'CAM', parents=['commercial:leases', 'accounting:hub', 'accounting:close'])
many(['commercial:cam_pool_new', 'commercial:cam_pool'], ['commercial:cam'])
screen('commercial:cam_reconcile', parents=['commercial:cam', 'accounting:close'])


# ================================================================ reading the tree
def ancestors(name):
    """Every screen above `name`, nearest first (its parents, then theirs ...). A screen never lists itself; a cycle in the tree is a bug the test catches, here it is simply not followed."""
    seen, order, queue = {name}, [], [name]
    while queue:
        node = SCREENS.get(queue.pop(0))
        if node is None:
            continue
        for parent, _ in node.parents:
            if parent not in seen:
                seen.add(parent)
                order.append(parent)
                queue.append(parent)
    return order


def allowed(name):
    """{wide screen above `name`: the label its Up link says} (a label is a string, a function of (request, ctx) or None for the screen's own)."""
    node = SCREENS.get(name)
    if node is None:
        return {}
    direct = {parent: label for parent, label in node.parents if label is not None}
    out = {}
    for parent in ancestors(name):
        above = SCREENS.get(parent)
        out[parent] = direct.get(parent) or (above.label if above is not None else None) or parent
    return out


def usual(name):
    """Where Up goes with no trail: the screen's own `usual`, else its first parent (nothing when it has none)."""
    node = SCREENS.get(name)
    if node is None:
        return None
    if node.usual is not None:
        return node.usual
    if node.parents:
        parent, label = node.parents[0]
        above = SCREENS.get(parent)
        return D(parent, label or (above.label if above is not None and isinstance(above.label, str) else None) or parent, args='infer')
    return None
