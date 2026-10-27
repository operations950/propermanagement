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
        CONTRACT = 'contract', 'Management contract'
        BUDGET = 'budget', 'Budget'
        OTHER = 'other', 'Other'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='assoc_documents')
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    name = models.CharField(max_length=200, blank=True)
    file = models.FileField(upload_to='association_documents/%Y/%m/', storage=DocumentStorage())
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['kind', '-uploaded_at']

    def __str__(self):
        return f'{self.get_kind_display()} - {self.property}'


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
    asked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


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
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['description']

    @property
    def is_fixed(self):
        return self.amount is not None


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


class ReconAcceptance(models.Model):
    """A ledger item still not on the statement that a person says is only timing (an outstanding check, a deposit in transit)."""
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='acceptances')
    month = models.DateField()
    line = models.ForeignKey(JournalLine, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
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
