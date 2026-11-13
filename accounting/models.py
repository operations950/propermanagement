"""The books for the associations we keep financials for.

A real double-entry ledger: every JournalEntry's debits equal its credits (ledger.post_entry refuses anything else), a posted entry is never edited
(a mistake is reversed), and a closed month is locked. One set of entries feeds two books, as Yardi's did: an entry's `basis` says whether it belongs
to both books, only the accrual book (a charge or a bill) or only the cash book (the income or expense recognised when the money moves).

Owners are the existing core.Contact; a unit is the existing core.Unit. What an association needs on top of them lives here: ownership rows with
dates, board terms, unit shares, charges, receipts, bills, payments, bank statements and reconciliations.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from core.models import Contact, Property, Unit
from core.storage import DocumentStorage

ZERO = Decimal('0.00')
MONEY = dict(max_digits=14, decimal_places=2)


# ---------------------------------------------------------------- the association itself

class AssociationProfile(models.Model):
    """What we know about an association beyond its Property row: the official record, and the settings the books run on."""
    class LateFeeKind(models.TextChoices):
        NONE = 'none', 'No late fee'
        FLAT = 'flat', 'A flat amount'
        PERCENT = 'percent', 'A percent of the charge'
        GREATER = 'greater', 'The greater of a flat amount and a percent'

    property = models.OneToOneField(Property, on_delete=models.CASCADE, related_name='assoc')
    we_do_financials = models.BooleanField(default=False, help_text='We keep this association\'s books. Off for an association we only manage.')

    # the official record (from the state's corporation search)
    legal_name = models.CharField(max_length=200, blank=True)
    state_doc_number = models.CharField(max_length=30, blank=True)
    ein = models.CharField(max_length=20, blank=True)
    date_filed = models.DateField(null=True, blank=True)
    state_status = models.CharField(max_length=30, blank=True)
    principal_address = models.CharField(max_length=300, blank=True)
    mailing_address = models.CharField(max_length=300, blank=True)
    registered_agent = models.CharField(max_length=200, blank=True)
    annual_report_filed = models.DateField(null=True, blank=True)
    sunbiz_checked_on = models.DateField(null=True, blank=True)
    sunbiz_url = models.URLField(blank=True)
    sunbiz_officers = models.JSONField(default=list, blank=True, help_text='[{"name", "title"}] as the state lists them.')
    sunbiz_checked_by = models.CharField(max_length=10, blank=True, help_text='auto when the program looked it up, hand when a person typed it in.')
    sunbiz_alerts = models.JSONField(default=list, blank=True, help_text='What the last lookup found that needs a person: not our registered agent, not active, report overdue, officers changed.')

    # how the books run
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)
    books_start = models.DateField(null=True, blank=True, help_text='The day the opening balances are as of (the books begin the next day).')
    spending_limit = models.DecimalField(null=True, blank=True, **MONEY, help_text='What we may pay without the board, from the management contract.')
    management_fee = models.DecimalField(null=True, blank=True, **MONEY, help_text='The monthly management fee, from the management contract.')
    late_fee_kind = models.CharField(max_length=10, choices=LateFeeKind.choices, default=LateFeeKind.NONE)
    late_fee_flat = models.DecimalField(null=True, blank=True, **MONEY)
    late_fee_percent = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=3)
    late_fee_grace_days = models.PositiveSmallIntegerField(null=True, blank=True)
    interest_rate = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=3, help_text='Annual percent charged on unpaid assessments.')
    late_fee_confirmed = models.BooleanField(default=False)
    interest_grace_days = models.PositiveSmallIntegerField(default=0, help_text='Days after the due date before interest starts (0: from the due date, as Florida law and most declarations say).')
    interest_confirmed = models.BooleanField(default=False)
    delinquency_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('100.00'), help_text='An owner is chased (late fee, interest, a late letter) once the balance is more than this.')
    compliance_owner = models.ForeignKey('core.StaffProfile', null=True, blank=True, on_delete=models.SET_NULL, related_name='+', help_text='Who the monthly delinquency reviews and the contract alerts are assigned to.')
    auto_post = models.BooleanField(default=True, help_text='Post recurring charges and bills by themselves on their dates.')

    # printing checks
    check_stock = models.FileField(upload_to='check_stock/%Y/%m/', blank=True, storage=DocumentStorage())
    check_layout = models.JSONField(default=dict, blank=True)
    next_check_number = models.PositiveIntegerField(default=1001)

    class Meta:
        ordering = ['property__name']

    def __str__(self):
        return self.legal_name or self.property.name

    def late_fee_is_set(self):
        return self.late_fee_kind != self.LateFeeKind.NONE and self.late_fee_confirmed

    def interest_is_set(self):
        return bool(self.interest_rate) and self.interest_rate > 0 and self.interest_confirmed

    def registered_agent_is_us(self):
        return 'proper realty' in (self.registered_agent or '').lower()

    def registered_agent_alert(self):
        """True when the state lists someone else as the registered agent (nothing to say while the record is still blank)."""
        return bool((self.registered_agent or '').strip()) and not self.registered_agent_is_us()

    def fiscal_year_start(self, day):
        """First day of the fiscal year that `day` falls in."""
        from datetime import date
        m = self.fiscal_year_start_month or 1
        year = day.year if day.month >= m else day.year - 1
        return date(year, m, 1)


class AssociationDocument(models.Model):
    class Kind(models.TextChoices):
        DECLARATION = 'declaration', 'Declaration of Condominium'
        ARTICLES = 'articles', 'Articles of Incorporation'
        BYLAWS = 'bylaws', 'Bylaws'
        RULES = 'rules', 'Rules and regulations'
        AMENDMENT = 'amendment', 'Amendment'
        EXHIBIT = 'exhibit', 'Exhibit or survey'
        CONTRACT = 'contract', 'Management contract'
        BUDGET = 'budget', 'Budget'
        OTHER = 'other', 'Other'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='assoc_documents')
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    name = models.CharField(max_length=200, blank=True)
    file = models.FileField(upload_to='association_documents/%Y/%m/', storage=DocumentStorage())
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    # read once, remembered: the words of every page are kept (DocumentPage), so a question never re-reads the file
    text_status = models.CharField(max_length=10, blank=True, default='', help_text="'' not read yet, waiting, reading, ready, partial (some scanned pages could not be read), failed.")
    page_count = models.PositiveIntegerField(null=True, blank=True)
    read_tries = models.PositiveSmallIntegerField(default=0)
    read_started_at = models.DateTimeField(null=True, blank=True)
    read_note = models.CharField(max_length=300, blank=True)
    # sorting one upload into the documents it holds (a binder may hold a declaration, articles, bylaws, rules and amendments)
    auto_sort = models.BooleanField(default=False, help_text='Uploaded without saying what it is: the program works it out and a person confirms.')
    sort_status = models.CharField(max_length=10, blank=True, default='', help_text="'' nothing to sort, waiting, proposed (needs a look), manual (choose by hand), confirmed.")
    sections = models.JSONField(default=list, blank=True, help_text='[{"kind","title","start","end","confidence"}] by page, once sorted.')
    indexed = models.BooleanField(default=False, help_text='Claude has noted what each page is about (once), so a question finds the right pages.')

    class Meta:
        ordering = ['kind', '-uploaded_at']

    def __str__(self):
        return f'{self.get_kind_display()} - {self.property}'


class DocumentPage(models.Model):
    """The words on one page of an uploaded document, kept so the document is never read again to answer a question."""
    document = models.ForeignKey(AssociationDocument, on_delete=models.CASCADE, related_name='pages')
    number = models.PositiveIntegerField()
    text = models.TextField(blank=True)
    by_claude = models.BooleanField(default=False, help_text='A scanned page that had to be read by Claude, once.')
    tags = models.TextField(blank=True, help_text='What the page is about, noted once by Claude: used to find the pages that answer a question.')

    class Meta:
        ordering = ['document_id', 'number']
        constraints = [models.UniqueConstraint(fields=['document', 'number'], name='uniq_document_page')]


class SettingProposal(models.Model):
    """A setting read out of a document, waiting for a person to confirm it. Nothing takes effect until it is confirmed."""
    class Status(models.TextChoices):
        PROPOSED = 'proposed', 'Proposed'
        CONFIRMED = 'confirmed', 'Confirmed'
        DISMISSED = 'dismissed', 'Dismissed'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='setting_proposals')
    key = models.CharField(max_length=40)
    value = models.JSONField(default=dict)
    document = models.ForeignKey(AssociationDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name='proposals')
    page = models.PositiveIntegerField(null=True, blank=True)
    quote = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROPOSED)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class DocumentQuestion(models.Model):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='document_questions')
    question = models.TextField()
    answer = models.TextField(blank=True)
    docs_signature = models.CharField(max_length=40, blank=True, help_text='Which documents were on file when it was answered: the same question is answered from memory until they change.')
    asked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True, help_text='When Claude last answered it (created_at for older rows).')
    sig_significant = models.CharField(max_length=40, blank=True, help_text='Which declaration, articles, bylaws, rules, amendments and contract were on file when it was answered.')
    sources = models.TextField(blank=True, help_text='The pages Claude was given, one per line.')
    asks = models.PositiveIntegerField(default=1, help_text='How many times this question was asked (a repeat is answered from memory).')
    reads = models.PositiveIntegerField(default=0, help_text='How many times someone sat with its answer on screen.')
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class DocumentQuestionRead(models.Model):
    """One person reading one answer on one day: counted once, so the list's ranking cannot be inflated."""
    question = models.ForeignKey(DocumentQuestion, on_delete=models.CASCADE, related_name='read_log')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    day = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['question', 'user', 'day'], name='uniq_question_read_per_day')]


# ---------------------------------------------------------------- the ledger

class Account(models.Model):
    class Kind(models.TextChoices):
        ASSET = 'asset', 'Asset'
        LIABILITY = 'liability', 'Liability'
        EQUITY = 'equity', 'Equity'
        INCOME = 'income', 'Income'
        EXPENSE = 'expense', 'Expense'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='accounts')
    code = models.CharField(max_length=8)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    is_active = models.BooleanField(default=True)
    budget_section = models.CharField(max_length=14, blank=True, default='', help_text='Where an expense account sits in the budget. Blank: the program decides from the number.')

    class Meta:
        ordering = ['code']
        constraints = [models.UniqueConstraint(fields=['association', 'code'], name='uniq_account_code_per_association')]

    def __str__(self):
        return f'{self.code} {self.name}'

    @property
    def normal_debit(self):
        return self.kind in (self.Kind.ASSET, self.Kind.EXPENSE)

    @property
    def is_balance_sheet(self):
        return self.kind in (self.Kind.ASSET, self.Kind.LIABILITY, self.Kind.EQUITY)


class JournalEntry(models.Model):
    class Basis(models.TextChoices):
        BOTH = 'both', 'Both books'
        ACCRUAL = 'accrual', 'Accrual book only'
        CASH = 'cash', 'Cash book only'

    class Source(models.TextChoices):
        MANUAL = 'manual', 'Journal entry'
        OPENING = 'opening', 'Opening balances'
        CHARGE = 'charge', 'Owner charge'
        RECEIPT = 'receipt', 'Owner payment'
        BILL = 'bill', 'Bill'
        PAYMENT = 'payment', 'Payment to a vendor'
        TRANSFER = 'transfer', 'Transfer between funds'
        BANK = 'bank', 'From the bank statement'
        REVERSAL = 'reversal', 'Reversal'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='journal_entries')
    number = models.PositiveIntegerField()
    date = models.DateField(db_index=True)
    memo = models.CharField(max_length=300, blank=True)
    basis = models.CharField(max_length=8, choices=Basis.choices, default=Basis.BOTH)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    source_ref = models.CharField(max_length=60, blank=True)
    reverses = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='reversed_by')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'number']
        constraints = [models.UniqueConstraint(fields=['association', 'number'], name='uniq_entry_number_per_association')]

    def __str__(self):
        return f'J-{self.number} {self.date}'


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='lines')
    debit = models.DecimalField(default=ZERO, **MONEY)
    credit = models.DecimalField(default=ZERO, **MONEY)
    unit = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    owner = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    vendor = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    memo = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['id']
        constraints = [models.CheckConstraint(condition=models.Q(debit__gte=0) & models.Q(credit__gte=0), name='journal_line_not_negative')]

    @property
    def signed(self):
        return self.debit - self.credit


class MonthLock(models.Model):
    """A month whose books are closed: nothing can be posted into it."""
    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='month_locks')
    month = models.DateField(help_text='First day of the month.')
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    locked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['association', 'month'], name='uniq_month_lock')]


class Budget(models.Model):
    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='budgets')
    year = models.PositiveSmallIntegerField(help_text='The year the fiscal year starts in.')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['association', 'year'], name='uniq_budget_year')]


class BudgetLine(models.Model):
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='+')
    annual_amount = models.DecimalField(**MONEY)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['budget', 'account'], name='uniq_budget_account')]


class PriorYearToDate(models.Model):
    """What an account did in the fiscal year before our books began (a reserve's contributions from January to the month before we took over, say), loaded once from the
    old system. Expense accounts do not need it: their year-to-date came across in the opening entry. A reserve account's opening entry is the fund's balance, not what was
    put in this year, so the budget reads the year's contributions from here."""
    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='prior_ytd')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='+')
    year = models.PositiveSmallIntegerField(help_text='The year the fiscal year starts in.')
    amount = models.DecimalField(**MONEY)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['association', 'account', 'year'], name='uniq_prior_ytd')]


class BudgetVersion(models.Model):
    """One stage of a year's budget, kept exactly as it was at that stage: the draft I propose, the draft after the board's comments, the version edited at the meeting, and the approved (locked) one."""
    class Stage(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        BOARD = 'board', 'Board draft'
        MEETING = 'meeting', 'Meeting final'
        APPROVED = 'approved', 'Approved'

    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name='versions')
    stage = models.CharField(max_length=10, choices=Stage.choices)
    through_month = models.DateField(null=True, blank=True, help_text='First day of the last closed month the actuals run through.')
    months_actual = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['budget', 'stage'], name='uniq_budget_stage')]


class BudgetVersionLine(models.Model):
    version = models.ForeignKey(BudgetVersion, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='+')
    prior_budget = models.DecimalField(default=ZERO, **MONEY)
    actual = models.DecimalField(default=ZERO, **MONEY)
    recent = models.DecimalField(default=ZERO, help_text='The last (up to) three closed months.', **MONEY)
    method = models.CharField(max_length=10, default='avg')
    custom = models.DecimalField(default=ZERO, **MONEY)
    adjust = models.CharField(max_length=20, blank=True, help_text='A dollar amount or a percent, added on top of the projected year.')
    note = models.CharField(max_length=200, blank=True)
    proposed = models.DecimalField(default=ZERO, **MONEY)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['version', 'account'], name='uniq_budget_version_account')]


# ---------------------------------------------------------------- units, owners, board

class ShareKind(models.Model):
    """A kind of percentage each unit carries (its share of the common expenses; later maybe one building only, or a garage)."""
    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='share_kinds')
    name = models.CharField(max_length=80)
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['association', 'name'], name='uniq_share_kind')]


class UnitProfile(models.Model):
    unit = models.OneToOneField(Unit, on_delete=models.CASCADE, related_name='assoc_profile')
    building = models.CharField(max_length=40, blank=True)


class UnitShare(models.Model):
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='shares')
    kind = models.ForeignKey(ShareKind, on_delete=models.CASCADE, related_name='unit_shares')
    percent = models.DecimalField(max_digits=10, decimal_places=6)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['unit', 'kind'], name='uniq_unit_share')]


class OwnerProfile(models.Model):
    """What an owner needs beyond a plain contact. One per person, whatever number of units they own."""
    class Delivery(models.TextChoices):
        MAIL = 'mail', 'By mail'
        EMAIL = 'email', 'By email'
        BOTH = 'both', 'Both'

    contact = models.OneToOneField(Contact, on_delete=models.CASCADE, related_name='owner_profile')
    is_company = models.BooleanField(default=False)
    legal_name = models.CharField(max_length=200, blank=True, help_text='Exactly as on the deed, for notices and liens.')
    mailing_address = models.TextField(blank=True)
    seasonal_address = models.TextField(blank=True)
    seasonal_from = models.DateField(null=True, blank=True)
    seasonal_to = models.DateField(null=True, blank=True)
    phone_alt = models.CharField(max_length=30, blank=True)
    email_alt = models.EmailField(blank=True)
    statements_by = models.CharField(max_length=6, choices=Delivery.choices, default=Delivery.MAIL)
    ok_to_text = models.BooleanField(default=False)
    language = models.CharField(max_length=30, blank=True)
    is_voter = models.BooleanField(default=True)
    emergency_assist = models.BooleanField(default=False)
    emergency_contact = models.TextField(blank=True)


class Ownership(models.Model):
    """One person owning one unit for a stretch of time. A unit may have several current owners; one of them is billed."""
    class Occupancy(models.TextChoices):
        OCCUPANT = 'occupant', 'Lives there'
        ABSENTEE = 'absentee', 'Not living there'

    class Pays(models.TextChoices):
        UNKNOWN = '', 'Not known'
        CHECK = 'check', 'Check'
        ONLINE = 'online', 'Online bill pay'
        ACH = 'ach', 'Bank transfer'
        CARD = 'card', 'Card'

    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='ownerships')
    person = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='ownerships')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    occupancy = models.CharField(max_length=10, choices=Occupancy.choices, default=Occupancy.OCCUPANT)
    is_billed = models.BooleanField(default=False, help_text='The bill and the statement go to this owner.')
    usual_payment = models.CharField(max_length=10, choices=Pays.choices, blank=True, default='')
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['unit__label', '-is_billed', 'start_date']

    @property
    def is_current(self):
        return self.end_date is None


class BoardTerm(models.Model):
    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='board_terms')
    person = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='board_terms')
    position = models.CharField(max_length=60)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['association', 'end_date', 'start_date']

    @property
    def is_current(self):
        return self.end_date is None


# ---------------------------------------------------------------- charges and what owners pay

class ChargeRule(models.Model):
    class Frequency(models.TextChoices):
        MONTHLY = 'monthly', 'Every month'
        QUARTERLY = 'quarterly', 'Every 3 months'
        SEMIANNUAL = 'semiannual', 'Twice a year'
        ANNUAL = 'annual', 'Once a year'
        ONCE = 'once', 'One time'

    class Method(models.TextChoices):
        FLAT = 'flat', 'The same for every unit'
        SHARE = 'share', "Split by each unit's share"

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='charge_rules')
    name = models.CharField(max_length=120)
    is_special = models.BooleanField(default=False)
    frequency = models.CharField(max_length=12, choices=Frequency.choices, default=Frequency.QUARTERLY)
    first_due = models.DateField()
    method = models.CharField(max_length=6, choices=Method.choices, default=Method.FLAT)
    amount = models.DecimalField(**MONEY, help_text='Per unit when flat; the total to split when by share.')
    share_kind = models.ForeignKey(ShareKind, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    income_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='+')
    installments = models.PositiveSmallIntegerField(null=True, blank=True, help_text='Stop after this many charges.')
    post_days_ahead = models.PositiveSmallIntegerField(default=10, help_text='Post the charges this many days before they are due, dated the due date, so owners can see the balance.')
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['is_special', 'name']


class Charge(models.Model):
    class Kind(models.TextChoices):
        ASSESSMENT = 'assessment', 'Assessment'
        SPECIAL = 'special', 'Special assessment'
        LATE_FEE = 'late_fee', 'Late fee'
        INTEREST = 'interest', 'Interest'
        FINE = 'fine', 'Fine'
        OTHER = 'other', 'Other charge'
        OPENING = 'opening', 'Balance forward'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='charges')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='charges')
    owner = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='charges')
    rule = models.ForeignKey(ChargeRule, null=True, blank=True, on_delete=models.SET_NULL, related_name='charges')
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.ASSESSMENT)
    description = models.CharField(max_length=200)
    due_date = models.DateField(db_index=True)
    amount = models.DecimalField(**MONEY)
    income_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='late_fees', help_text='The charge a late fee is for.')
    is_void = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['due_date', 'id']
        constraints = [models.UniqueConstraint(fields=['rule', 'unit', 'due_date'], condition=models.Q(rule__isnull=False), name='uniq_rule_unit_due')]

    @property
    def paid(self):
        return sum((a.amount for a in self.applications.all()), ZERO)

    @property
    def balance(self):
        return self.amount - self.paid


class Receipt(models.Model):
    class Method(models.TextChoices):
        CHECK = 'check', 'Check'
        ONLINE = 'online', 'Online bill pay'
        ACH = 'ach', 'Bank transfer'
        CARD = 'card', 'Card'
        LOCKBOX = 'lockbox', 'Lockbox'
        CASH = 'cash', 'Cash'
        OTHER = 'other', 'Other'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='receipts')
    owner = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='receipts')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='receipts')
    date = models.DateField(db_index=True)
    amount = models.DecimalField(**MONEY)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.CHECK)
    reference = models.CharField(max_length=80, blank=True)
    memo = models.CharField(max_length=200, blank=True)
    bank_account = models.ForeignKey('BankAccount', null=True, blank=True, on_delete=models.SET_NULL, related_name='receipts')
    entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    cash_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    is_void = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'id']

    @property
    def applied(self):
        return sum((a.amount for a in self.applications.all()), ZERO)

    @property
    def unapplied(self):
        return self.amount - self.applied


class Application(models.Model):
    """Part of a receipt used to pay part of a charge."""
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name='applications')
    charge = models.ForeignKey(Charge, on_delete=models.CASCADE, related_name='applications')
    amount = models.DecimalField(**MONEY)
    date = models.DateField()


# ---------------------------------------------------------------- bills and what we pay

class VendorTerms(models.Model):
    """How one association deals with one vendor (a core.Contact of type Vendor)."""
    class Pay(models.TextChoices):
        CHECK = 'check', 'We write a check'
        ACH = 'ach', 'Automatic bank payment'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='vendor_terms')
    vendor = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='+')
    default_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    pay_method = models.CharField(max_length=6, choices=Pay.choices, default=Pay.CHECK)
    is_1099 = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['association', 'vendor'], name='uniq_vendor_terms')]


class RecurringBill(models.Model):
    class Frequency(models.TextChoices):
        MONTHLY = 'monthly', 'Every month'
        QUARTERLY = 'quarterly', 'Every 3 months'
        SEMIANNUAL = 'semiannual', 'Twice a year'
        ANNUAL = 'annual', 'Once a year'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='recurring_bills')
    vendor = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='+')
    description = models.CharField(max_length=200)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='+')
    amount = models.DecimalField(null=True, blank=True, **MONEY, help_text='Blank when the amount changes each time (a utility): it is read from the bank statement.')
    frequency = models.CharField(max_length=12, choices=Frequency.choices, default=Frequency.MONTHLY)
    next_date = models.DateField()
    pay_method = models.CharField(max_length=6, choices=VendorTerms.Pay.choices, default=VendorTerms.Pay.CHECK)
    statement_match = models.CharField(max_length=80, blank=True, help_text='Text the bank statement line contains, to recognise the payment.')
    post_on_month_end = models.BooleanField(default=False, help_text='Billed in arrears: the bill is dated the last day of the month it covers, and posts that day.')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['description']

    @property
    def is_fixed(self):
        return self.amount is not None


class JournalRule(models.Model):
    """A scheduled journal entry. For now one kind: the monthly reserve contribution, split by the approved budget's reserve lines (see scheduled.py)."""
    class Kind(models.TextChoices):
        RESERVE = 'reserve', 'Reserve contribution'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='journal_rules')
    name = models.CharField(max_length=120, default='Reserve contribution')
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.RESERVE)
    day_of_month = models.PositiveSmallIntegerField(default=27, help_text='The day the transfer to the reserve account happens; 31 means the last day of the month.')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Bill(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'To review'
        AWAITING_BOARD = 'awaiting_board', 'Waiting for the board'
        APPROVED = 'approved', 'Ready to pay'
        PAID = 'paid', 'Paid'
        VOID = 'void', 'Void'

    class Kind(models.TextChoices):
        ROUTINE = 'routine', 'Routine'
        EXCEPTION = 'exception', 'One-off'
        RESERVE = 'reserve', 'Reserve spending'

    class Source(models.TextChoices):
        MANUAL = 'manual', 'Entered by hand'
        UPLOAD = 'upload', 'Uploaded invoice'
        EMAIL = 'email', 'Read from the invoice inbox'
        RECURRING = 'recurring', 'Recurring'
        STATEMENT = 'statement', 'From the bank statement'
        OPENING = 'opening', 'Balance forward'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='bills')
    vendor = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    vendor_text = models.CharField(max_length=200, blank=True, help_text='The name on the invoice, when it is not matched to a vendor yet.')
    number = models.CharField(max_length=60, blank=True)
    date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(**MONEY)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.DRAFT)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.EXCEPTION)
    reserve = models.BooleanField(default=False, help_text='Paid from the reserves: always needs the board.')
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    recurring = models.ForeignKey(RecurringBill, null=True, blank=True, on_delete=models.SET_NULL, related_name='bills')
    file = models.FileField(upload_to='bills/%Y/%m/', blank=True, storage=DocumentStorage())
    email_message_id = models.CharField(max_length=120, blank=True, db_index=True)
    memo = models.CharField(max_length=300, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    board_approved_by = models.CharField(max_length=120, blank=True)
    board_approved_on = models.DateField(null=True, blank=True)
    entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'id']

    @property
    def payee(self):
        return self.vendor.name if self.vendor_id else (self.vendor_text or 'Unknown vendor')

    @property
    def paid(self):
        return sum((p.amount for p in self.payments.all()), ZERO)

    @property
    def balance(self):
        return self.amount - self.paid


class BillLine(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='+')
    amount = models.DecimalField(**MONEY)
    memo = models.CharField(max_length=200, blank=True)


class BankAccount(models.Model):
    class Kind(models.TextChoices):
        OPERATING = 'operating', 'Operating'
        RESERVE = 'reserve', 'Reserves'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='bank_accounts')
    name = models.CharField(max_length=80)
    bank_name = models.CharField(max_length=80, blank=True)
    last4 = models.CharField(max_length=4, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OPERATING)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='+', help_text='The cash account in the books this bank account is.')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['kind', 'name']

    def __str__(self):
        return f'{self.name} ({self.bank_name} {self.last4})'.strip()


class Payment(models.Model):
    """Money we paid out: a check we wrote, or an automatic bank payment recorded. Opening checks (written before the books began) have no entry."""
    class Method(models.TextChoices):
        CHECK = 'check', 'Check'
        ACH = 'ach', 'Automatic bank payment'
        CARD = 'card', 'Card'

    class Status(models.TextChoices):
        OUTSTANDING = 'outstanding', 'Not cleared'
        CLEARED = 'cleared', 'Cleared'
        VOID = 'void', 'Void'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='payments')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='payments')
    number = models.CharField(max_length=20, blank=True)
    date = models.DateField(db_index=True)
    vendor = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    payee_text = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(**MONEY)
    method = models.CharField(max_length=6, choices=Method.choices, default=Method.CHECK)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OUTSTANDING)
    cleared_on = models.DateField(null=True, blank=True)
    memo = models.CharField(max_length=200, blank=True)
    opening = models.BooleanField(default=False)
    printed_at = models.DateTimeField(null=True, blank=True)
    entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    cash_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'id']

    @property
    def payee(self):
        return self.vendor.name if self.vendor_id else (self.payee_text or 'Unknown payee')


class PaymentBill(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='bill_links')
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(**MONEY)


class BankOpeningItem(models.Model):
    """A deposit not yet in the bank, or some other reconciling item, that was already on the books when they moved here (an outstanding check is a Payment instead).
    It has no journal entry: the opening balances cover it. Plus is money the books have and the bank does not yet, minus is the reverse."""
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='opening_items')
    date = models.DateField()
    description = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(**MONEY, help_text='Plus: in the books, not yet at the bank. Minus: in the books as money out, not yet taken by the bank.')
    memo = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'id']


# ---------------------------------------------------------------- bank statements and reconciliation

class BankStatement(models.Model):
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='statements')
    period_end = models.DateField()
    begin_balance = models.DecimalField(null=True, blank=True, **MONEY)
    end_balance = models.DecimalField(**MONEY)
    file = models.FileField(upload_to='bank_statements/%Y/%m/', blank=True, storage=DocumentStorage())
    read_by = models.CharField(max_length=20, blank=True, help_text='How the lines were read: csv, claude or seed.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_end']
        constraints = [models.UniqueConstraint(fields=['bank_account', 'period_end'], name='uniq_statement_period')]


class BankLine(models.Model):
    class Kind(models.TextChoices):
        CHECK = 'check', 'Check'
        WITHDRAWAL = 'withdrawal', 'Withdrawal'
        DEPOSIT = 'deposit', 'Deposit'
        INTEREST = 'interest', 'Interest'
        FEE = 'fee', 'Bank fee'

    statement = models.ForeignKey(BankStatement, on_delete=models.CASCADE, related_name='lines')
    date = models.DateField()
    description = models.CharField(max_length=300)
    amount = models.DecimalField(**MONEY, help_text='Plus for money in, minus for money out.')
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.WITHDRAWAL)
    check_number = models.CharField(max_length=20, blank=True)
    match = models.ForeignKey('ReconMatch', null=True, blank=True, on_delete=models.SET_NULL, related_name='bank_lines')

    class Meta:
        ordering = ['date', 'id']


class ReconMatch(models.Model):
    """Some bank lines and some ledger items that are the same money."""
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='matches')
    month = models.DateField(help_text='First day of the month the reconciliation was done for.')
    auto = models.BooleanField(default=True)
    how = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ReconLedgerItem(models.Model):
    """The ledger side of a match: a journal line on the cash account, or an opening check that had no entry."""
    match = models.ForeignKey(ReconMatch, on_delete=models.CASCADE, related_name='ledger_items')
    line = models.OneToOneField(JournalLine, null=True, blank=True, on_delete=models.CASCADE, related_name='recon_item')
    payment = models.OneToOneField(Payment, null=True, blank=True, on_delete=models.CASCADE, related_name='recon_item')
    opening_item = models.OneToOneField(BankOpeningItem, null=True, blank=True, on_delete=models.CASCADE, related_name='recon_item')


class ReconAcceptance(models.Model):
    """A ledger item still not on the statement that a person says is only timing (an outstanding check, a deposit in transit)."""
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='acceptances')
    month = models.DateField()
    line = models.ForeignKey(JournalLine, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    opening_item = models.ForeignKey(BankOpeningItem, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    note = models.CharField(max_length=200, blank=True)
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)


class Reconciliation(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        CLOSED = 'closed', 'Closed'

    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='reconciliations')
    month = models.DateField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    statement = models.ForeignKey(BankStatement, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    snapshot = models.JSONField(default=dict, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['bank_account', 'month'], name='uniq_reconciliation_month')]


# ---------------------------------------------------------------- the invoice inbox

class InvoiceInbox(models.Model):
    """Settings for reading invoices out of a Gmail label. One row."""
    enabled = models.BooleanField(default=False)
    label = models.CharField(max_length=80, default='Invoices')
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=300, blank=True)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]


class SeenEmail(models.Model):
    message_id = models.CharField(max_length=120, unique=True)
    bill = models.ForeignKey(Bill, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    subject = models.CharField(max_length=300, blank=True)
    sender = models.CharField(max_length=200, blank=True)
    outcome = models.CharField(max_length=200, blank=True)
    needs_home = models.BooleanField(default=False, help_text='An invoice was found but not which association it is for.')
    created_at = models.DateTimeField(auto_now_add=True)


# ---------------------------------------------------------------- collections: owners who are behind

class DelinquencyCase(models.Model):
    """One owner who is behind, from the day their first late letter is made until the balance is paid. It is reviewed every 30 days: each review is a compliance item."""
    class Stage(models.TextChoices):
        LETTER = 'letter', 'Letter sent'
        FINAL = 'final', 'Final notice'
        ATTORNEY = 'attorney', 'With the attorney'
        PLAN = 'plan', 'Payment plan'
        RESOLVED = 'resolved', 'Resolved'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='delinquency_cases')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='delinquency_cases')
    owner = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='delinquency_cases')
    stage = models.CharField(max_length=10, choices=Stage.choices, default=Stage.LETTER)
    opened_on = models.DateField()
    next_review = models.DateField(null=True, blank=True)
    resolved_on = models.DateField(null=True, blank=True)
    attorney_name = models.CharField(max_length=120, blank=True)
    review_line = models.ForeignKey('worksessions.SessionLine', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-opened_on', '-pk']

    @property
    def is_open(self):
        return self.resolved_on is None


class CaseEvent(models.Model):
    """One thing that happened on a case: the timeline."""
    class Kind(models.TextChoices):
        LETTER = 'letter', 'Letter'
        CALL = 'call', 'Call'
        NOTE = 'note', 'Note'
        STAGE = 'stage', 'Step'
        FEE = 'fee', 'Fee or interest'
        REVIEW = 'review', 'Reviewed'
        PLAN = 'plan', 'Payment plan'
        ATTORNEY = 'attorney', 'Attorney'
        RESOLVED = 'resolved', 'Resolved'

    case = models.ForeignKey(DelinquencyCase, on_delete=models.CASCADE, related_name='events')
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.NOTE)
    date = models.DateField()
    summary = models.CharField(max_length=500)
    document = models.ForeignKey('legalforms.GeneratedDocument', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-pk']


# ---------------------------------------------------------------- contracts

class Contract(models.Model):
    """A contract of the association: the record the law makes us keep and show, and what drives its recurring bill and its alerts."""
    class Frequency(models.TextChoices):
        MONTHLY = 'monthly', 'Every month'
        QUARTERLY = 'quarterly', 'Every 3 months'
        SEMIANNUAL = 'semiannual', 'Twice a year'
        ANNUAL = 'annual', 'Once a year'
        ONCE = 'once', 'One time'

    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='contracts')
    name = models.CharField(max_length=200, help_text='What it is, e.g. "Landscaping" or "Elevator service".')
    vendor = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    vendor_name = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True, help_text='When the current term ends.')
    auto_renews = models.BooleanField(default=False)
    renewal_months = models.PositiveSmallIntegerField(default=12, help_text='How long each renewal lasts.')
    notice_days = models.PositiveSmallIntegerField(default=60, help_text='Days before the term ends that cancelling must be in writing.')
    amount = models.DecimalField(null=True, blank=True, **MONEY)
    frequency = models.CharField(max_length=12, choices=Frequency.choices, default=Frequency.MONTHLY)
    expense_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    recurring_bill = models.ForeignKey('RecurringBill', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    document = models.ForeignKey(AssociationDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    board_approved_on = models.DateField(null=True, blank=True)
    visible_to_owners = models.BooleanField(default=True, help_text='Owners may ask to see it; it is on the official-records list.')
    is_active = models.BooleanField(default=True)
    confirmed = models.BooleanField(default=True, help_text='False while the fields were read from the file and nobody has checked them.')
    notes = models.TextField(blank=True)
    alerts_made = models.JSONField(default=list, blank=True, help_text='The alerts already put on the compliance screen, so none is made twice.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class MasterAccount(models.Model):
    """The master chart of accounts: the list a new association starts from. Each association has its own copy (Account); this is where the company keeps the standard."""
    code = models.CharField(max_length=8, unique=True)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Account.Kind.choices)
    is_active = models.BooleanField(default=True, help_text='Off: new associations do not start with it. Existing associations keep theirs.')
    budget_section = models.CharField(max_length=14, blank=True, default='', help_text="Where an expense account sits in every association's budget: Administrative, Utilities or Operating.")

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} {self.name}'


class LedgerDetail(models.Model):
    """One line of the old system's general ledger, kept as detail behind the year-to-date figures we carried across as a single number. It is not a journal entry and changes no balance."""
    association = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='ledger_detail')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='ledger_detail')
    book = models.CharField(max_length=8, choices=[('cash', 'Cash'), ('accrual', 'Accrual')])
    date = models.DateField()
    period = models.DateField(help_text='The first of the month the old system counted it in.')
    who = models.CharField(max_length=200, blank=True)
    ctrl = models.CharField(max_length=30, blank=True, help_text="The old system's control number, like K-1529 or R-2846.")
    reference = models.CharField(max_length=80, blank=True)
    debit = models.DecimalField(default=ZERO, **MONEY)
    credit = models.DecimalField(default=ZERO, **MONEY)
    remarks = models.CharField(max_length=500, blank=True)
    batch = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ['date', 'id']
        indexes = [models.Index(fields=['association', 'account', 'book'], name='ledger_detail_lookup')]

    def __str__(self):
        return f'{self.date} {self.account.code} {self.who}'
