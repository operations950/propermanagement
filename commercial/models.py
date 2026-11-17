"""Commercial buildings: tenants, leases, scheduled rent and tenant invoices.

A commercial property keeps its books exactly as an association does (accounting/): the same ledger, bank accounts, bills, reconciliations and month-end close. What differs is
the person on the unit: a TENANT instead of an owner. A lease is the tenant's record. Saving a lease keeps an accounting.Ownership row for the tenant (billed), so every charge,
payment, aging and statement screen of the books works for tenants without a second copy of the code.

Rent comes from the lease: a base monthly rent, rent steps (increases from a date), seasonal rents (other amounts in certain months), and a monthly CAM estimate. commercial/rent.py
turns that into charges ahead of each due date; commercial/invoices.py makes and sends the tenant's invoice.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from core.models import Contact, Property, Unit
from core.storage import DocumentStorage

MONEY = dict(max_digits=14, decimal_places=2)


class CommercialSettings(models.Model):
    """How a commercial property's rent and invoices run. One per property; made when it is first needed."""
    property = models.OneToOneField(Property, on_delete=models.CASCADE, related_name='commercial_settings')
    rent_post_days_ahead = models.PositiveSmallIntegerField(default=10, help_text='Rent and CAM charges are posted (and the invoice made) this many days before the first of the month they are due.')
    auto_send_invoices = models.BooleanField(default=False, help_text='Email each invoice to the tenant by itself when it is made. Off: you look at them first and send them from the invoices screen.')
    payment_instructions = models.TextField(blank=True, help_text='Printed at the bottom of every invoice: where and how to pay.')

    def __str__(self):
        return f'Commercial settings for {self.property.name}'


class Lease(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Signed, not started'
        ACTIVE = 'active', 'Active'
        ENDED = 'ended', 'Ended'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='leases')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='leases')
    tenant = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='leases')
    ownership = models.OneToOneField('accounting.Ownership', null=True, blank=True, on_delete=models.SET_NULL, related_name='lease', help_text='The tenant row the books bill (kept in step with this lease).')
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    base_rent = models.DecimalField(default=Decimal('0.00'), help_text='The monthly rent before any rent step or seasonal rent.', **MONEY)
    prorate = models.BooleanField(default=True, help_text='A first or last month that is only part of a month is charged by the day.')
    cam_estimate = models.DecimalField(default=Decimal('0.00'), help_text='The monthly CAM (common area maintenance) estimate billed with the rent. The yearly true-up is separate.', **MONEY)
    security_deposit = models.DecimalField(default=Decimal('0.00'), help_text='What the tenant put down. Recorded here only: it is not in the books and not tied to a bank account.', **MONEY)
    deposit_received_on = models.DateField(null=True, blank=True)
    deposit_note = models.CharField(max_length=300, blank=True, help_text='Where the money is held, if anyone knows.')
    billing_emails = models.TextField(blank=True, help_text='Where invoices go: one or more addresses separated by commas or lines. Blank: the tenant contact\'s own email.')
    renewal_notes = models.TextField(blank=True, help_text='Renewal options, notice dates, anything the lease says about the end of the term.')
    notes = models.TextField(blank=True)
    # where this lease's rent is paid (two leases of one tenant can pay into two bank accounts), and when its rent rate runs out
    bank_account = models.ForeignKey('accounting.BankAccount', null=True, blank=True, on_delete=models.SET_NULL, related_name='+', help_text='The bank account this lease is paid into. Blank: the operating account of the property.')
    rate_expires_on = models.DateField(null=True, blank=True, help_text='The date the current rent rate runs out and the rent steps up (the next increase), when the lease says so.')
    # month-end lease renewals step: the end date / rate date a person has looked at and said is okay
    renewal_ok_for = models.DateField(null=True, blank=True, help_text='The lease end date the month-end close was told is okay (it asks again when the end date changes).')
    rate_ok_for = models.DateField(null=True, blank=True, help_text='The rent rate expiry date the month-end close was told is okay.')
    renewal_ok_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    renewal_ok_at = models.DateTimeField(null=True, blank=True)
    # a different late fee for this lease (blank: the property's own terms apply; the grace period is always the property's)
    late_fee_flat = models.DecimalField(null=True, blank=True, **MONEY)
    late_fee_percent = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=3)
    # for the CAM reconciliation to come (nothing here is used yet)
    cam_method = models.CharField(max_length=30, blank=True, help_text='How this tenant\'s share of common costs is worked out (set when the CAM reconciliation is built).')
    cam_share_percent = models.DecimalField(null=True, blank=True, max_digits=8, decimal_places=4)
    cam_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['property__name', 'unit__label', '-start_date']

    def __str__(self):
        return f'{self.tenant.name} in {self.unit.label}'

    def billing_list(self):
        """The addresses an invoice goes to, in order, without repeats."""
        import re
        raw = [e.strip() for e in re.split(r'[,;\s]+', self.billing_emails or '') if e.strip()]
        if not raw and (self.tenant.email or '').strip():
            raw = [self.tenant.email.strip()]
        seen, out = set(), []
        for e in raw:
            if e.lower() not in seen:
                seen.add(e.lower())
                out.append(e)
        return out


class LongTermLease(models.Model):
    """A long-term rental's lease. These properties are not run through our books (the rent goes to the owner's trust account), so this is the record of the tenant, the term and the rent,
    with the rent increases for the life of the lease. The monthly rent in force feeds the month-end rent check."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Signed, not started'
        ACTIVE = 'active', 'Active'
        ENDED = 'ended', 'Ended'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='ltr_leases')
    tenant = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='ltr_leases')
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    monthly_rent = models.DecimalField(default=Decimal('0.00'), help_text='The monthly rent before any rent increase.', **MONEY)
    security_deposit = models.DecimalField(default=Decimal('0.00'), **MONEY)
    rate_expires_on = models.DateField(null=True, blank=True, help_text='The date the current rent rate runs out and the rent steps up.')
    renewal_notes = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    renewal_ok_for = models.DateField(null=True, blank=True)
    rate_ok_for = models.DateField(null=True, blank=True)
    renewal_ok_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    renewal_ok_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['property__name', '-start_date']

    def __str__(self):
        return f'{self.tenant.name} at {self.property.name}'

    def rent_for_month(self, month):
        """The monthly rent for a month: the rent, replaced by the latest increase that has started by then (an increase dated the 1st starts that month, any other date the next)."""
        from datetime import timedelta
        month = month.replace(day=1)
        amount = self.monthly_rent
        steps = []
        for st in self.steps.all():
            d = st.effective_date
            first = d if d.day == 1 else (d.replace(day=1) + timedelta(days=32)).replace(day=1)
            if first <= month:
                steps.append((first, st.pk, st.monthly_rent))
        if steps:
            amount = max(steps)[2]
        return amount

    def covers(self, month):
        """Whether the lease runs in any part of the month."""
        import calendar
        month = month.replace(day=1)
        last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        if self.status == self.Status.ENDED and not self.end_date:
            return False
        if self.start_date and self.start_date > last:
            return False
        return not (self.end_date and self.end_date < month)


class LongTermRentStep(models.Model):
    lease = models.ForeignKey(LongTermLease, on_delete=models.CASCADE, related_name='steps')
    effective_date = models.DateField()
    monthly_rent = models.DecimalField(**MONEY)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['effective_date', 'id']
        constraints = [models.UniqueConstraint(fields=['lease', 'effective_date'], name='uniq_ltr_rent_step_date')]


class LeaseProposal(models.Model):
    """What was read out of an uploaded lease, waiting for a person: the term, the rent, and the rent increases for the life of the lease. Nothing changes the lease until it is applied."""
    class Status(models.TextChoices):
        PROPOSED = 'proposed', 'To review'
        APPLIED = 'applied', 'Applied'
        DISMISSED = 'dismissed', 'Dismissed'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='lease_proposals')
    lease = models.ForeignKey(Lease, null=True, blank=True, on_delete=models.CASCADE, related_name='proposals')
    ltr_lease = models.ForeignKey(LongTermLease, null=True, blank=True, on_delete=models.CASCADE, related_name='proposals')
    document = models.ForeignKey('accounting.AssociationDocument', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PROPOSED)
    data = models.JSONField(default=dict, blank=True, help_text='{term_start, term_end, monthly_rent, cam_monthly, security_deposit, steps: [{date, monthly_rent, quote, page}], renewal, notes}')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class RentStep(models.Model):
    """From this date the monthly rent is this amount (a yearly increase, a negotiated change). It starts with the month the date falls in when it is the 1st, otherwise the next month."""
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='steps')
    effective_date = models.DateField()
    monthly_rent = models.DecimalField(**MONEY)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['effective_date', 'id']
        constraints = [models.UniqueConstraint(fields=['lease', 'effective_date'], name='uniq_rent_step_date')]


class SeasonalRent(models.Model):
    """A different monthly rent in certain months of every year (for example November through April). It replaces the base rent or the rent step in those months."""
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='seasons')
    name = models.CharField(max_length=80, blank=True)
    start_month = models.PositiveSmallIntegerField(help_text='1 to 12')
    end_month = models.PositiveSmallIntegerField(help_text='1 to 12; it may be earlier than the start month (November to April runs over the new year).')
    monthly_rent = models.DecimalField(**MONEY)

    class Meta:
        ordering = ['start_month', 'id']

    def covers(self, month):
        if self.start_month <= self.end_month:
            return self.start_month <= month <= self.end_month
        return month >= self.start_month or month <= self.end_month


class LeaseDocument(models.Model):
    class Kind(models.TextChoices):
        LEASE = 'lease', 'Lease'
        AMENDMENT = 'amendment', 'Amendment'
        INSURANCE = 'insurance', 'Certificate of insurance'
        OTHER = 'other', 'Other'

    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='documents')
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.LEASE)
    name = models.CharField(max_length=200, blank=True)
    file = models.FileField(upload_to='lease_documents/%Y/%m/', storage=DocumentStorage())
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    reader_doc = models.ForeignKey('accounting.AssociationDocument', null=True, blank=True, on_delete=models.SET_NULL, related_name='+', help_text='The same file kept as a document the program reads (a lease or amendment).')

    class Meta:
        ordering = ['-uploaded_at', '-id']


class TenantInvoice(models.Model):
    """One tenant's invoice for one month: what was owed when it was made (lines and the balance before), kept as it was, and whether and when it was sent."""
    class Status(models.TextChoices):
        READY = 'ready', 'Ready to send'
        SENT = 'sent', 'Sent'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='tenant_invoices')
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='invoices')
    month = models.DateField(help_text='First day of the month the invoice is due.')
    due_date = models.DateField()
    number = models.CharField(max_length=40)
    tenant_name = models.CharField(max_length=200)
    unit_label = models.CharField(max_length=100)
    lines = models.JSONField(default=list, blank=True, help_text='[{description, amount}] charged for the month.')
    previous_balance = models.DecimalField(default=Decimal('0.00'), **MONEY)
    total = models.DecimalField(default=Decimal('0.00'), **MONEY)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.READY)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    sent_to = models.JSONField(default=list, blank=True)
    send_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['month', 'unit_label', 'id']
        constraints = [models.UniqueConstraint(fields=['lease', 'month'], name='uniq_tenant_invoice_month')]


# ---------------------------------------------------------------- CAM (common area maintenance)
# What the landlord spends to run the shared parts of the building is charged back to the tenants. Each month the lease's cam_estimate is billed; once a year the real cost is
# worked out, divided between the tenants by the method each lease says, and the difference from the estimates is billed or credited (commercial/cam.py, camcalc.py).

class CamEstimateStep(models.Model):
    """From this date the monthly CAM estimate is this amount (set by a reconciliation, or by hand). Like a rent step: it starts with the month the date falls in when it is the 1st, otherwise the next month."""
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='cam_steps')
    effective_date = models.DateField()
    monthly_amount = models.DecimalField(**MONEY)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['effective_date', 'id']
        constraints = [models.UniqueConstraint(fields=['lease', 'effective_date'], name='uniq_cam_step_date')]


class CamPool(models.Model):
    """A pool of shared costs for one year: which expense accounts are recoverable, what is added on top (an administration fee, a gross-up) and how the square feet are counted.
    A building may have several pools (the building, the parking, taxes and insurance), each divided its own way."""
    class Denominator(models.TextChoices):
        ALL = 'all', 'All the leasable square feet of the building'
        OCCUPIED = 'occupied', 'Only the square feet of the tenants in this pool'
        MANUAL = 'manual', 'A square-foot total I type'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='cam_pools')
    name = models.CharField(max_length=100)
    year = models.PositiveSmallIntegerField(help_text='The year the fiscal year starts in (the calendar year when the books run January to December).')
    basis = models.CharField(max_length=7, choices=[('accrual', 'Accrual book'), ('cash', 'Cash book')], default='accrual')
    accounts = models.ManyToManyField('accounting.Account', blank=True, related_name='+', help_text='The expense accounts whose cost is recovered from tenants.')
    excluded_accounts = models.ManyToManyField('accounting.Account', blank=True, related_name='+', help_text='Accounts left out of the pool even when listed above (for every tenant).')
    gross_up_accounts = models.ManyToManyField('accounting.Account', blank=True, related_name='+', help_text='The variable costs that are grossed up when occupancy is low. Blank: the whole pool.')
    admin_fee_percent = models.DecimalField(default=Decimal('0.00'), max_digits=6, decimal_places=3, help_text='An administration or management fee added on the pool, as a percent of it.')
    gross_up_percent = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=3, help_text='Variable costs are worked out as if the building were this percent occupied (for example 95) when it was less. Blank: no gross-up.')
    occupancy_override = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=3, help_text='The occupancy percent to use for the gross-up. Blank: worked out from the leases and their square feet.')
    denominator = models.CharField(max_length=8, choices=Denominator.choices, default=Denominator.ALL)
    denominator_sf = models.DecimalField(null=True, blank=True, max_digits=12, decimal_places=2, help_text='The square-foot total, when you choose to type it.')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['year', 'name']
        constraints = [models.UniqueConstraint(fields=['property', 'name', 'year'], name='uniq_cam_pool_name_year')]

    def __str__(self):
        return f'{self.name} {self.year}'


class CamShare(models.Model):
    """How one lease takes part in one pool: the method that gives its share, and the limits the lease puts on it."""
    class Method(models.TextChoices):
        SQFT = 'sqft', 'Square feet (pro rata)'
        PERCENT = 'percent', 'A fixed percent'
        EQUAL_TENANTS = 'equal_tenants', 'Equal split between the tenants'
        EQUAL_UNITS = 'equal_units', 'Equal split between the units'
        WEIGHT = 'weight', 'A weight I set'
        FIXED = 'fixed', 'A fixed amount for the year'
        NONE = 'none', 'Not charged'

    pool = models.ForeignKey(CamPool, on_delete=models.CASCADE, related_name='shares')
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='cam_shares')
    method = models.CharField(max_length=14, choices=Method.choices, default=Method.SQFT)
    percent = models.DecimalField(null=True, blank=True, max_digits=8, decimal_places=4, help_text='The lease percent, when the method is a fixed percent.')
    weight = models.DecimalField(null=True, blank=True, max_digits=10, decimal_places=4, help_text='The weight, when the method is a weight: a share is the weight over the total of the weights in the pool.')
    fixed_amount = models.DecimalField(null=True, blank=True, help_text='The year amount, when the method is a fixed amount.', **MONEY)
    prorate = models.BooleanField(default=True, help_text='A tenant who had the space only part of the year pays that part of the year, by the day.')
    cap_percent = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=3, help_text='The most this tenant share of the pool may rise over what it was last year, as a percent.')
    max_per_sf = models.DecimalField(null=True, blank=True, max_digits=8, decimal_places=3, help_text='The most a square foot of this tenant space pays for the year in this pool.')
    base_year = models.PositiveSmallIntegerField(null=True, blank=True, help_text='A base-year stop: the tenant pays only what is above its share in this earlier year (taken from that year reconciliation here).')
    base_amount = models.DecimalField(null=True, blank=True, help_text='Or type the yearly amount the tenant does not pay (a base-year stop).', **MONEY)
    excluded_accounts = models.ManyToManyField('accounting.Account', blank=True, related_name='+', help_text='Costs this tenant does not pay (its lease leaves them out). The landlord keeps them.')
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['lease__unit__label', 'id']
        constraints = [models.UniqueConstraint(fields=['pool', 'lease'], name='uniq_cam_share')]


class CamReconciliation(models.Model):
    """One year's CAM reconciliation of a property. It is a draft (settings only; the figures are worked out from the books each time it is opened) until it is approved: then the lines
    are kept as they were, one charge or credit per tenant is in the books, and it cannot be approved again."""
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        APPROVED = 'approved', 'Approved'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='cam_reconciliations')
    year = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    due_days = models.PositiveSmallIntegerField(default=30, help_text='A balance owed is due this many days after the approval date.')
    growth_percent = models.DecimalField(default=Decimal('0.00'), max_digits=6, decimal_places=3, help_text='What the new monthly estimate adds on top of the year cost, as a percent (costs usually rise).')
    update_estimates = models.BooleanField(default=False, help_text='On approval, set each tenant monthly CAM estimate to the new amount.')
    estimates_effective = models.DateField(null=True, blank=True)
    approval_date = models.DateField(null=True, blank=True, help_text='The date the charges and credits are posted.')
    pools = models.JSONField(default=list, blank=True, help_text='What each pool came to when it was approved.')
    total_allocated = models.DecimalField(default=Decimal('0.00'), **MONEY)
    total_estimates = models.DecimalField(default=Decimal('0.00'), **MONEY)
    total_true_up = models.DecimalField(default=Decimal('0.00'), **MONEY)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-year']
        constraints = [models.UniqueConstraint(fields=['property', 'year'], name='uniq_cam_reconciliation_year')]


class CamLine(models.Model):
    """One tenant result in an approved reconciliation, as it was approved."""
    reconciliation = models.ForeignKey(CamReconciliation, on_delete=models.CASCADE, related_name='lines')
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='cam_lines')
    tenant_name = models.CharField(max_length=200)
    unit_label = models.CharField(max_length=100)
    square_feet = models.DecimalField(default=Decimal('0.00'), max_digits=12, decimal_places=2)
    allocated = models.DecimalField(default=Decimal('0.00'), **MONEY)
    estimates = models.DecimalField(default=Decimal('0.00'), **MONEY)
    true_up = models.DecimalField(default=Decimal('0.00'), help_text='Plus: the tenant owes it. Minus: the tenant is credited.', **MONEY)
    new_estimate = models.DecimalField(default=Decimal('0.00'), **MONEY)
    detail = models.JSONField(default=dict, blank=True)
    charge = models.ForeignKey('accounting.Charge', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    receipt = models.ForeignKey('accounting.Receipt', null=True, blank=True, on_delete=models.SET_NULL, related_name='+', help_text='The credit given to the tenant when the true-up is a refund.')
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    sent_to = models.JSONField(default=list, blank=True)
    send_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['unit_label', 'id']
        constraints = [models.UniqueConstraint(fields=['reconciliation', 'lease'], name='uniq_cam_line_lease')]
