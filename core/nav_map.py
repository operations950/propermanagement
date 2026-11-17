"""Where each screen's Up link can go (see core/nav.py).

UP maps a screen's url name to (parents, default):
  parents  {url name of a WIDE screen it can be reached from: the label Up shows when the person came from there}
  default  where Up goes when the trail has none of those (a bookmark, an emailed link): D(url name, label, args=..., query=...) or a function of the request.
A screen is listed here when its Up link needs no work in the view. Screens whose label or default is worked out from the record on the page (a property's name, a ticket's saved
filters, an association's words) call core.nav.up() in the view and put the answer in the context as `up` (the {% up_link %} tag reads that first).

DETAIL_VIEWS are the bottom level: one ticket, one property, one contact, one unit. Up from one of them never goes to another (tests/test_nav_map guards this); the forms and
sub-pages that hang off a detail screen (edit, preview, a document) may list their detail as a parent, because it is the screen they belong to.
"""
from .nav import D

# ---- labels people already know these screens by
CHECKLIST = 'Month-end checklist'
CLOSE_RENTALS = 'Rentals'
ADMIN = 'Admin Tools'
ONSITE = 'On-Site Visits'
RENTALS_TODAY = 'Rentals today'
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


# The wide screens a property's page is opened from.
PROPERTY_FROM = {
    'property_list': PROPERTIES, 'ticket_list': TICKETS, 'dashboard': DASH, 'ws_home': HOME, 'ws_compliance': COMPLIANCE, 'ws_projects': PROJECTS, 'ws_financials': FINANCIALS,
    'close_home': CHECKLIST, 'close_overview': CLOSE_RENTALS, 'close_long_term': 'Long-term rentals', 'accounting:home': 'Associations', 'contact_list': CONTACTS,
    'onsite_dashboard': ONSITE, 'onsite_str_today': RENTALS_TODAY, 'onsite_performance': 'Rental performance', 'onsite_reservation_list': 'Reservations',
}

TICKET_FROM = {'ticket_list': TICKETS, 'dashboard': DASH, 'department_dashboard': DASH, 'ws_home': HOME, 'ws_week': 'My week', 'ws_compliance': COMPLIANCE, 'ws_projects': PROJECTS}

ONSITE_FROM = {'onsite_dashboard': ONSITE, 'onsite_calendar': 'Calendar', 'onsite_str_today': RENTALS_TODAY, 'ws_home': HOME, 'ws_week': 'My week'}

_CLOSE_CHILD = ({'close_home': CHECKLIST}, D('close_home', CHECKLIST, query=('month',)))
_ADMIN_CHILD = ({'admin_tools': ADMIN}, D('admin_tools', ADMIN))
_ONSITE_CHILD = ({'onsite_dashboard': ONSITE, 'ws_home': HOME}, D('onsite_dashboard', ONSITE))
_RENTALS_CHILD = ({'onsite_str_today': RENTALS_TODAY}, D('onsite_str_today', RENTALS_TODAY))

UP = {
    # Admin tools
    'bot_keys': _ADMIN_CHILD, 'property_data_import': _ADMIN_CHILD, 'quickbooks_accounts': _ADMIN_CHILD, 'staff_create': _ADMIN_CHILD,
    'owner_portal_admin': ({'close_overview': CLOSE_RENTALS, 'admin_tools': ADMIN}, D('close_overview', CLOSE_RENTALS)),
    # The company month-end close: every step's screen goes up to the checklist (and keeps its month)
    'close_bank_tieout': _CLOSE_CHILD, 'close_long_term': _CLOSE_CHILD, 'close_overview': _CLOSE_CHILD, 'close_escrow': _CLOSE_CHILD, 'close_misc': _CLOSE_CHILD,
    'close_pay_owners': _CLOSE_CHILD, 'close_pay_proper': _CLOSE_CHILD, 'close_pay_ltr_owners': _CLOSE_CHILD, 'close_rent_collection': _CLOSE_CHILD,
    'close_sales_tax': _CLOSE_CHILD, 'tourist_tax': _CLOSE_CHILD, 'close_payouts': _CLOSE_CHILD, 'commercial:renewals': _CLOSE_CHILD,
    'commercial:ltr_leases': ({'property_detail': 'Property'}, D('property_detail', 'Property', args=('pk',))), 'commercial:ltr_lease': ({'commercial:ltr_leases': 'Leases'}, D('commercial:ltr_leases', 'Leases', args=('pk',))),
    # Properties and contacts
    'missing_documents': ({'property_list': PROPERTIES}, D('property_list', PROPERTIES)),
    'faq_review_queue': ({'property_list': PROPERTIES}, D('property_list', PROPERTIES)),
    'property_create': ({'property_list': PROPERTIES}, D('property_list', PROPERTIES)),
    'property_edit': ({'property_list': PROPERTIES}, D('property_list', PROPERTIES)),
    'property_detail': (PROPERTY_FROM, D('property_list', PROPERTIES)),
    'contact_detail': ({'contact_list': CONTACTS, 'contact_duplicates': 'Duplicate contacts', 'contact_review': 'Contact review'}, D('contact_list', CONTACTS)),
    'contact_create': ({'contact_list': CONTACTS}, D('contact_list', CONTACTS)),
    'contact_edit': ({'contact_detail': 'Contact', 'contact_list': CONTACTS}, D('contact_detail', 'Contact', args=('pk',))),
    # On-Site
    'onsite_booking_feeds': _ONSITE_CHILD, 'onsite_calendar': _ONSITE_CHILD, 'onsite_checklist_custom_items': _ONSITE_CHILD, 'onsite_checklist_templates': _ONSITE_CHILD,
    'onsite_cleaner_activity': _ONSITE_CHILD, 'onsite_cleaning_payments': _ONSITE_CHILD, 'onsite_visit_create': _ONSITE_CHILD, 'onsite_visit_rule_list': _ONSITE_CHILD,
    'onsite_visit_detail': (ONSITE_FROM, D('onsite_dashboard', ONSITE)),
    'onsite_checklist_template_detail': ({'onsite_checklist_templates': 'Checklist templates'}, D('onsite_checklist_templates', 'Checklist templates')),
    'onsite_booking_import': ({'close_payouts': 'Platform payouts', 'onsite_dashboard': ONSITE}, D('close_payouts', 'Platform payouts')),
    'onsite_booking_import_preview': ({'onsite_booking_import': 'Import payouts'}, D('onsite_booking_import', 'Import payouts')),
    'onsite_feeds_recalibrate': ({'onsite_booking_feeds': 'Booking calendars'}, D('onsite_booking_feeds', 'Booking calendars')),
    'onsite_performance': _RENTALS_CHILD, 'onsite_rentals_calendar': _RENTALS_CHILD, 'onsite_reservation_list': _RENTALS_CHILD,
    'onsite_performance_property': ({'onsite_performance': 'Rental performance'}, D('onsite_performance', 'Rental performance')),
    'onsite_performance_calendar': ({'onsite_performance_property': 'Property performance', 'onsite_performance': 'Rental performance'}, D('onsite_performance_property', 'Property performance', args={'property_id': 'property_id'})),
    'onsite_reservation_review': ({'onsite_reservation_list': 'Reservations'}, D('onsite_reservation_list', 'Reservations')),
    'onsite_reservation_create': ({'onsite_reservation_list': 'Reservations'}, D('onsite_reservation_list', 'Reservations')),
    'onsite_reservation_edit': ({'onsite_reservation_list': 'Reservations', 'onsite_reservation_review': 'Reservations to review'}, D('onsite_reservation_list', 'Reservations')),
    # Tickets
    'ticket_create': (TICKET_FROM, D('ticket_list', 'All tickets')),
    'department_dashboard': ({'dashboard': 'Main dashboard'}, D('dashboard', 'Main dashboard')),
    'ticket_previous_conversation': ({'ticket_detail': 'Back to ticket'}, D('ticket_detail', 'Back to ticket', args=('pk',))),
    # Time clock
    'timeclock_policy': ({'timeclock_home': 'My time'}, D('timeclock_home', 'My time')),
    'timeclock_change': ({'timeclock_home': 'My time'}, D('timeclock_home', 'My time')),
    'timeclock_time_off': ({'timeclock_home': 'My time'}, D('timeclock_home', 'My time')),
    'timeclock_fix': ({'timeclock_home': 'My time'}, D('timeclock_home', 'My time')),
    # Compliance (recurring items) and processes
    'session_detail': ({'ws_compliance': COMPLIANCE, 'my_sessions': 'My Recurring', 'ws_home': HOME, 'ws_week': 'My week'}, _session_home),
    'session_template_detail': ({'ws_compliance': 'Compliance rules', 'session_template_list': 'Recurring Rules'}, _rules_home),
    'session_template_edit': ({'session_template_detail': 'The rule', 'ws_compliance': 'Compliance rules', 'session_template_list': 'Recurring Rules'}, _rules_home),
    'session_template_create': ({'ws_compliance': 'Compliance rules', 'session_template_list': 'Recurring Rules'}, _rules_home),
    'process_template_create': ({'process_template_list': 'Process templates'}, D('process_template_list', 'Process templates')),
    'process_template_edit': ({'process_template_list': 'Process templates'}, D('process_template_list', 'Process templates')),
    'process_template_preview': ({'process_template_edit': 'Back to builder'}, D('process_template_edit', 'Back to builder', args=('pk',))),
    # Portfolio, supplies
    'ws_people': ({'ws_admin': 'Admin'}, D('ws_admin', 'Admin')),
    'portfolio_business_detail': ({'portfolio_dashboard': 'My Businesses'}, D('portfolio_dashboard', 'My Businesses')),
    'portfolio_task_detail': ({'portfolio_business_detail': 'Business'}, D('portfolio_business_detail', 'Business', args=('slug',))),
    'supplies:order_detail': ({'supplies:cart': "Today's cart"}, D('supplies:cart', "Today's cart")),
}

# The bottom level: one record. Up from one of these never lands on another of these.
DETAIL_VIEWS = frozenset({
    'property_detail', 'contact_detail', 'ticket_detail', 'onsite_visit_detail', 'session_detail', 'session_template_detail', 'portfolio_task_detail',
    'accounting:unit', 'accounting:owner', 'accounting:bill', 'accounting:case', 'legalforms:detail', 'onsite_performance_property', 'close_statement', 'close_month_statement',
    'commercial:lease', 'commercial:cam_pool',          # a tenant's page and a CAM pool: the bottom level of the commercial screens
})
