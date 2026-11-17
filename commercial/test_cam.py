"""The CAM reconciliation: the arithmetic (pure), the books in and the charges and credits out, undoing, estimates, statements and the screens."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounting import charges, ledger
from accounting.models import Charge, Receipt
from core.models import Contact, Property, Unit

from . import cam, camcalc, rent
from .invoices import InvoiceError
from .models import CamEstimateStep, CamLine, CamPool, CamReconciliation, CamShare, Lease
from .tests import LOCMEM, STORAGES, bank_for, make_commercial


def pool_spec(**kw):
    spec = {'actual': {'6410': D('12000')}, 'excluded': set(), 'gross_up_codes': set(), 'gross_up_percent': None, 'occupancy_percent': None, 'admin_fee_percent': 0, 'denominator_sf': D('5000'),
            'equal_count': 0, 'unit_count': 4, 'weights_total': D('0')}
    spec.update(kw)
    return spec


def tenant(**kw):
    t = {'sf': D('2000'), 'year_fraction': D('1'), 'method': 'sqft', 'prorate': True}
    t.update(kw)
    return t


class AllocationArithmeticTests(SimpleTestCase):
    def test_square_feet_pro_rata(self):
        r = camcalc.allocate_tenant(pool_spec(), tenant(sf=D('2000')))
        self.assertEqual(r['allocated'], D('4800.00'))                                # 12,000 x 2000/5000

    def test_a_fixed_percent_ignores_the_square_feet(self):
        r = camcalc.allocate_tenant(pool_spec(), tenant(method='percent', percent=D('12.5')))
        self.assertEqual(r['allocated'], D('1500.00'))

    def test_equal_split_between_tenants_and_between_units(self):
        self.assertEqual(camcalc.allocate_tenant(pool_spec(equal_count=3), tenant(method='equal_tenants'))['allocated'], D('4000.00'))
        self.assertEqual(camcalc.allocate_tenant(pool_spec(unit_count=4), tenant(method='equal_units'))['allocated'], D('3000.00'))

    def test_a_weight_is_a_share_of_the_weights(self):
        r = camcalc.allocate_tenant(pool_spec(weights_total=D('10')), tenant(method='weight', weight=D('2.5')))
        self.assertEqual(r['allocated'], D('3000.00'))

    def test_a_fixed_amount_does_not_depend_on_the_pool(self):
        r = camcalc.allocate_tenant(pool_spec(), tenant(method='fixed', fixed=D('2400')))
        self.assertEqual(r['allocated'], D('2400.00'))

    def test_not_charged_is_zero(self):
        self.assertEqual(camcalc.allocate_tenant(pool_spec(), tenant(method='none'))['allocated'], D('0.00'))

    def test_a_part_year_tenant_pays_by_the_day_and_a_switch_turns_it_off(self):
        half = D('182.5') / D('365')
        self.assertEqual(camcalc.allocate_tenant(pool_spec(), tenant(year_fraction=half))['allocated'], D('2400.00'))
        self.assertEqual(camcalc.allocate_tenant(pool_spec(), tenant(year_fraction=half, prorate=False))['allocated'], D('4800.00'))

    def test_the_admin_fee_is_a_percent_of_the_pool(self):
        r = camcalc.allocate_tenant(pool_spec(admin_fee_percent=D('10')), tenant())
        self.assertEqual((r['cost']['admin_fee'], r['cost']['total'], r['allocated']), (D('1200.00'), D('13200.00'), D('5280.00')))

    def test_gross_up_applies_only_to_the_variable_costs_and_only_when_less_occupied(self):
        spec = pool_spec(actual={'6410': D('10000'), '6420': D('2000')}, gross_up_codes={'6410'}, gross_up_percent=D('95'), occupancy_percent=D('80'))
        cost = camcalc.pool_cost(spec['actual'], gross_up_codes=spec['gross_up_codes'], gross_up_percent=D('95'), occupancy_percent=D('80'))
        self.assertEqual((cost['recoverable'], cost['gross_up'], cost['total']), (D('12000.00'), D('1875.00'), D('13875.00')))       # 10,000 x (95/80 - 1)
        full = camcalc.pool_cost(spec['actual'], gross_up_codes={'6410'}, gross_up_percent=D('95'), occupancy_percent=D('97'))
        self.assertEqual(full['gross_up'], D('0.00'))
        whole = camcalc.pool_cost({'6410': D('10000')}, gross_up_percent=D('100'), occupancy_percent=D('50'))
        self.assertEqual(whole['total'], D('20000.00'))                                                                                  # no accounts named: the whole pool

    def test_an_excluded_account_is_left_out_for_everyone_or_for_one_tenant(self):
        spec = pool_spec(actual={'6410': D('10000'), '6420': D('2000')}, excluded={'6420'})
        self.assertEqual(camcalc.allocate_tenant(spec, tenant(sf=D('5000')))['allocated'], D('10000.00'))
        spec = pool_spec(actual={'6410': D('10000'), '6420': D('2000')})
        self.assertEqual(camcalc.allocate_tenant(spec, tenant(sf=D('5000'), excluded={'6420'}))['allocated'], D('10000.00'))
        self.assertEqual(camcalc.allocate_tenant(spec, tenant(sf=D('5000')))['allocated'], D('12000.00'))

    def test_a_base_year_stop_charges_only_the_part_above_the_base(self):
        r = camcalc.allocate_tenant(pool_spec(), tenant(base_amount=D('4000')))
        self.assertEqual((r['gross_share'], r['allocated']), (D('4800.00'), D('800.00')))
        self.assertEqual(camcalc.allocate_tenant(pool_spec(), tenant(base_amount=D('9000')))['allocated'], D('0.00'))      # never below nothing

    def test_a_cap_limits_the_rise_over_last_years_share(self):
        r = camcalc.allocate_tenant(pool_spec(), tenant(cap_percent=D('5'), prior_annual=D('4000')))
        self.assertEqual(r['allocated'], D('4200.00'))
        self.assertTrue(any('Capped' in s for s in r['steps']))
        self.assertEqual(camcalc.allocate_tenant(pool_spec(), tenant(cap_percent=D('5'), prior_annual=None))['allocated'], D('4800.00'))   # no last year: no cap

    def test_a_most_per_square_foot_limits_the_share(self):
        r = camcalc.allocate_tenant(pool_spec(), tenant(max_per_sf=D('2.00')))
        self.assertEqual(r['allocated'], D('4000.00'))                                # 2000 sq ft x 2.00

    def test_the_true_up_is_owed_or_a_credit(self):
        self.assertEqual(camcalc.true_up(D('6000'), D('4800')), D('1200.00'))
        self.assertEqual(camcalc.true_up(D('4800'), D('6000')), D('-1200.00'))
        self.assertEqual(camcalc.true_up(D('4800'), D('4800')), D('0.00'))

    def test_the_new_monthly_estimate_is_the_year_over_twelve_with_growth(self):
        self.assertEqual(camcalc.new_monthly_estimate(D('6000')), D('500.00'))
        self.assertEqual(camcalc.new_monthly_estimate(D('6000'), D('4')), D('520.00'))

    def test_no_square_feet_to_divide_by_gives_zero_and_says_why(self):
        r = camcalc.allocate_tenant(pool_spec(denominator_sf=D('0')), tenant())
        self.assertEqual(r['allocated'], D('0.00'))
        self.assertIn('no square feet', r['steps'][0])


class Base(TestCase):
    """A building with two tenants (3,000 and 2,000 sq ft of 5,000), a year of CAM estimates billed, and a year of building costs in the books."""
    def setUp(self):
        self.prop = make_commercial('Del Park LLC')
        bank_for(self.prop)
        self.a_unit = Unit.objects.create(property=self.prop, label='A', square_footage=3000)
        self.b_unit = Unit.objects.create(property=self.prop, label='B', square_footage=2000)
        self.a = self.lease(self.a_unit, 'Alpha Cafe', cam='100')
        self.b = self.lease(self.b_unit, 'Beta Shop', cam='500')
        for m in range(1, 13):
            for l in (self.a, self.b):
                rent.post_lease_month(l, date(2026, m, 1))
        self.spend(date(2026, 3, 15), '6410', '8000')
        self.spend(date(2026, 9, 15), '6410', '4000')
        self.pool = CamPool.objects.create(property=self.prop, name='Building', year=2026)
        self.pool.accounts.set([ledger.account(self.prop, '6410')])
        cam.ensure_shares(self.pool)

    def lease(self, unit, name, cam='0', start=date(2026, 1, 1), end=None):
        person = Contact.objects.create(name=name, contact_type=Contact.ContactType.TENANT, email=f'{name.split()[0].lower()}@tenant.example')
        lease = Lease.objects.create(property=self.prop, unit=unit, tenant=person, start_date=start, end_date=end, base_rent=D('1000'), cam_estimate=D(cam))
        rent.ensure_ownership(lease)
        return lease

    def spend(self, day, code, amount):
        ledger.post_entry(self.prop, day, [{'account': code, 'debit': D(amount)}, {'account': '1110', 'credit': D(amount)}], memo='test spend')

    def recon(self, **kw):
        r = cam.draft_for(self.prop, 2026)
        for k, v in kw.items():
            setattr(r, k, v)
        r.save()
        return r


class ReconciliationFromTheBooksTests(Base):
    def test_the_cost_comes_from_the_ledger_and_the_estimates_from_the_charges(self):
        plan = cam.compute(self.prop, 2026)
        rows = {r['tenant']: r for r in plan['lines']}
        self.assertEqual(plan['pools'][0]['cost']['total'], D('12000.00'))
        self.assertEqual((rows['Alpha Cafe']['allocated'], rows['Alpha Cafe']['estimates'], rows['Alpha Cafe']['true_up']), (D('7200.00'), D('1200.00'), D('6000.00')))
        self.assertEqual((rows['Beta Shop']['allocated'], rows['Beta Shop']['estimates'], rows['Beta Shop']['true_up']), (D('4800.00'), D('6000.00'), D('-1200.00')))
        self.assertEqual(plan['totals']['left_with_landlord'], D('0.00'))

    def test_the_cash_book_can_be_chosen(self):
        self.pool.basis = 'cash'
        self.pool.save()
        self.assertEqual(cam.compute(self.prop, 2026)['pools'][0]['actual'], {'6410': D('12000.00')})       # the test spend is in both books

    def test_a_vacant_space_leaves_its_share_with_the_landlord(self):
        Lease.objects.filter(pk=self.b.pk).update(status='ended', end_date=date(2025, 12, 31))
        plan = cam.compute(self.prop, 2026)
        self.assertEqual([r['tenant'] for r in plan['lines']], ['Alpha Cafe'])
        self.assertEqual(plan['pools'][0]['left_with_landlord'], D('4800.00'))

    def test_a_tenant_who_left_mid_year_pays_for_the_part_he_had(self):
        Lease.objects.filter(pk=self.a.pk).update(end_date=date(2026, 6, 30))
        row = [r for r in cam.compute(self.prop, 2026)['lines'] if r['tenant'] == 'Alpha Cafe'][0]
        self.assertEqual(row['allocated'], D('3570.41'))                                  # by square feet 7,200, for 181 of 365 days
        self.assertEqual(D(row['year_fraction']).quantize(D('0.0001')), D('0.4959'))

    def test_two_pools_add_up_per_tenant(self):
        self.spend(date(2026, 5, 1), '6420', '1000')
        second = CamPool.objects.create(property=self.prop, name='Parking', year=2026, denominator='occupied')
        second.accounts.set([ledger.account(self.prop, '6420')])
        cam.ensure_shares(second)
        row = {r['tenant']: r for r in cam.compute(self.prop, 2026)['lines']}['Alpha Cafe']
        self.assertEqual([p['name'] for p in row['pools']], ['Building', 'Parking'])
        self.assertEqual(row['allocated'], D('7200.00') + D('600.00'))                    # parking is split over the 5,000 occupied sq ft

    def test_a_tenant_percent_comes_from_the_lease_when_the_share_has_none(self):
        CamShare.objects.filter(pool=self.pool, lease=self.a).update(method='percent', percent=None)
        Lease.objects.filter(pk=self.a.pk).update(cam_share_percent=D('50'))
        row = {r['tenant']: r for r in cam.compute(self.prop, 2026)['lines']}['Alpha Cafe']
        self.assertEqual(row['allocated'], D('6000.00'))

    def test_gross_up_uses_the_occupancy_worked_out_from_the_leases(self):
        Lease.objects.filter(pk=self.b.pk).update(status='ended', end_date=date(2025, 12, 31))        # 3,000 of 5,000 sq ft occupied = 60%
        CamPool.objects.filter(pk=self.pool.pk).update(gross_up_percent=D('100'))
        plan = cam.compute(self.prop, 2026)
        self.assertEqual(plan['pools'][0]['cost']['total'], D('20000.00'))                # 12,000 / 0.60

    def test_no_pools_warns(self):
        CamPool.objects.all().delete()
        self.assertTrue(any('no CAM pools' in w for w in cam.compute(self.prop, 2026)['warnings']))

    def test_a_tenant_billed_an_estimate_but_in_no_pool_is_pointed_out(self):
        CamShare.objects.filter(pool=self.pool, lease=self.b).delete()
        self.assertTrue(any('Beta Shop' in w and 'not in any pool' in w for w in cam.compute(self.prop, 2026)['warnings']))


class ApproveTests(Base):
    def test_approving_charges_the_tenant_who_owes_and_credits_the_one_who_overpaid(self):
        recon = cam.approve(self.recon(), user=None, approval_date=date(2027, 1, 20))
        self.assertEqual(recon.status, 'approved')
        lines = {l.tenant_name: l for l in recon.lines.all()}
        owed = lines['Alpha Cafe']
        self.assertEqual((owed.true_up, owed.charge.amount, owed.charge.kind, owed.charge.income_account.code, owed.charge.due_date), (D('6000.00'), D('6000.00'), 'special', '4110', date(2027, 2, 19)))
        self.assertEqual(owed.charge.entry.date, date(2027, 1, 20))
        credit = lines['Beta Shop']
        self.assertEqual((credit.true_up, credit.receipt.amount, credit.receipt.method, credit.charge), (D('-1200.00'), D('1200.00'), 'other', None))
        self.assertEqual((recon.total_allocated, recon.total_estimates, recon.total_true_up), (D('12000.00'), D('7200.00'), D('4800.00')))

    def test_the_credit_is_held_for_the_tenant_and_aging_is_untouched(self):
        charges.receive_payment(self.prop, self.b_unit, self.b.tenant, date(2026, 12, 31), D('18000'), bank_account=self.prop.bank_accounts.first())       # nothing is owed when the credit comes
        recon = cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        credit = recon.lines.get(tenant_name='Beta Shop')
        accounts = {a['unit'].label: a for a in charges.accounts(self.prop)}
        self.assertEqual(accounts['B']['balance'], D('-1200.00'))                          # a credit, shown as paid ahead
        self.assertEqual(credit.receipt.unapplied, D('1200.00'))
        aging = charges.aging(self.prop, date(2027, 1, 31))
        self.assertTrue(all(r['unit'].label != 'B' or r['total'] >= 0 for r in aging['rows']))
        accts = {l.account.code: (l.debit, l.credit) for l in credit.receipt.entry.lines.select_related('account')}
        self.assertEqual(accts, {'4110': (D('1200.00'), D('0.00')), '2210': (D('0.00'), D('1200.00'))})

    def test_a_credit_is_used_at_once_on_what_the_tenant_owes(self):
        rent.post_lease_month(self.b, date(2027, 1, 1))                                    # January rent and CAM are open when the credit comes
        recon = cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        credit = recon.lines.get(tenant_name='Beta Shop').receipt
        self.assertEqual(credit.unapplied, D('0.00'))
        self.assertTrue(credit.applications.exists())

    def test_approving_twice_posts_nothing_a_second_time(self):
        recon = cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        count = (Charge.objects.count(), Receipt.objects.count(), recon.lines.count())
        with self.assertRaises(cam.CamError):
            cam.approve(recon, approval_date=date(2027, 1, 21))
        self.assertEqual((Charge.objects.count(), Receipt.objects.count(), recon.lines.count()), count)

    def test_a_closed_month_stops_the_approval_and_leaves_no_half_posting(self):
        ledger.lock_month(self.prop, date(2027, 1, 1))
        before = (Charge.objects.count(), Receipt.objects.count())
        with self.assertRaises(ledger.LedgerError):
            cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        self.assertEqual((Charge.objects.count(), Receipt.objects.count(), CamLine.objects.count()), (before[0], before[1], 0))
        self.assertEqual(CamReconciliation.objects.get(property=self.prop, year=2026).status, 'draft')

    def test_the_estimates_can_be_set_to_the_new_amount_from_a_date(self):
        recon = cam.approve(self.recon(update_estimates=True, estimates_effective=date(2027, 2, 1), growth_percent=D('4')), approval_date=date(2027, 1, 20))
        step = CamEstimateStep.objects.get(lease=self.a)
        self.assertEqual((step.effective_date, step.monthly_amount), (date(2027, 2, 1), D('624.00')))        # 7,200 x 1.04 / 12
        self.assertEqual(rent.rent_for_month(self.a, date(2027, 1, 1))['cam'], D('100.00'))
        self.assertEqual(rent.rent_for_month(self.a, date(2027, 2, 1))['cam'], D('624.00'))
        self.assertEqual(recon.lines.get(tenant_name='Alpha Cafe').new_estimate, D('624.00'))

    def test_the_estimates_are_left_alone_unless_asked(self):
        cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        self.assertFalse(CamEstimateStep.objects.exists())

    def test_an_approved_year_keeps_its_figures_when_the_books_change(self):
        recon = cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        self.spend(date(2026, 12, 20), '6410', '3000')
        line = recon.lines.get(tenant_name='Alpha Cafe')
        self.assertEqual(CamLine.objects.get(pk=line.pk).allocated, D('7200.00'))

    def test_nothing_to_reconcile_is_refused(self):
        CamPool.objects.all().delete()
        with self.assertRaises(cam.CamError):
            cam.approve(self.recon())


class UndoTests(Base):
    def test_undoing_voids_the_charge_and_the_credit_and_reopens_the_year(self):
        recon = cam.approve(self.recon(update_estimates=True, estimates_effective=date(2027, 2, 1)), approval_date=date(2027, 1, 20))
        charge_pk = recon.lines.get(tenant_name='Alpha Cafe').charge_id
        credit_pk = recon.lines.get(tenant_name='Beta Shop').receipt_id
        cam.undo(recon)
        recon.refresh_from_db()
        self.assertEqual((recon.status, recon.lines.count(), CamEstimateStep.objects.count()), ('draft', 0, 0))
        self.assertTrue(Charge.objects.get(pk=charge_pk).is_void)
        self.assertTrue(Receipt.objects.get(pk=credit_pk).is_void)
        again = cam.approve(recon, approval_date=date(2027, 1, 25))                           # it can be approved again
        self.assertEqual(again.lines.count(), 2)

    def test_a_paid_charge_blocks_the_undo(self):
        bank = self.prop.bank_accounts.first()
        charges.receive_payment(self.prop, self.a_unit, self.a.tenant, date(2026, 12, 31), D('13200'), bank_account=bank)       # the year's rent and estimates are paid
        recon = cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        charges.receive_payment(self.prop, self.a_unit, self.a.tenant, date(2027, 2, 1), D('6000'), bank_account=bank)
        with self.assertRaises(cam.CamError):
            cam.undo(recon)
        self.assertEqual(CamReconciliation.objects.get(pk=recon.pk).status, 'approved')


class NextYearTests(Base):
    def test_a_tenant_left_out_on_purpose_stays_out_and_a_new_tenant_joins(self):
        CamShare.objects.filter(pool=self.pool, lease=self.b).delete()
        c_unit = Unit.objects.create(property=self.prop, label='C', square_footage=500)
        newcomer = self.lease(c_unit, 'Gamma Gym', start=date(2027, 3, 1))
        cam.copy_pools(self.prop, 2026, 2027)
        new = CamPool.objects.get(property=self.prop, year=2027)
        self.assertEqual(set(new.shares.values_list('lease__tenant__name', flat=True)), {'Alpha Cafe', 'Gamma Gym'})

    def test_the_pools_and_shares_copy_forward_once(self):
        CamShare.objects.filter(pool=self.pool, lease=self.a).update(method='percent', percent=D('60'), cap_percent=D('5'))
        self.assertEqual(cam.copy_pools(self.prop, 2026, 2027), 1)
        new = CamPool.objects.get(property=self.prop, year=2027, name='Building')
        self.assertEqual(set(new.accounts.values_list('code', flat=True)), {'6410'})
        share = new.shares.get(lease=self.a)
        self.assertEqual((share.method, share.percent, share.cap_percent), ('percent', D('60'), D('5')))
        self.assertEqual(cam.copy_pools(self.prop, 2026, 2027), 0)

    def test_a_cap_uses_last_years_approved_share_and_a_base_year_stop_uses_that_years_gross(self):
        cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        cam.copy_pools(self.prop, 2026, 2027)
        new = CamPool.objects.get(property=self.prop, year=2027)
        CamShare.objects.filter(pool=new, lease=self.a).update(cap_percent=D('5'))
        CamShare.objects.filter(pool=new, lease=self.b).update(base_year=2026)
        self.spend(date(2027, 4, 1), '6410', '24000')                                       # costs double
        rows = {r['tenant']: r for r in cam.compute(self.prop, 2027)['lines']}
        self.assertEqual(rows['Alpha Cafe']['allocated'], D('7560.00'))                    # 7,200 last year +5%
        self.assertEqual(rows['Beta Shop']['allocated'], D('4800.00'))                     # 9,600 less the 4,800 base year share

    def test_a_cap_with_no_prior_year_warns_instead_of_guessing(self):
        CamShare.objects.filter(pool=self.pool, lease=self.a).update(cap_percent=D('5'))
        self.assertTrue(any('cap on its rise' in w for w in cam.compute(self.prop, 2026)['warnings']))

    def test_the_close_sheet_reminds_in_january_until_the_year_is_approved(self):
        self.assertEqual(cam.close_note(self.prop, date(2027, 1, 1)), 'CAM reconciliation for 2026 not approved.')
        self.assertEqual(cam.close_note(self.prop, date(2027, 8, 1)), '')
        self.assertEqual(cam.close_note(self.prop, date(2026, 11, 1)), '')
        cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        self.assertEqual(cam.close_note(self.prop, date(2027, 1, 1)), '')


@override_settings(EMAIL_BACKEND=LOCMEM)
class StatementTests(Base):
    def setUp(self):
        super().setUp()
        self.recon_obj = cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        self.line = self.recon_obj.lines.get(tenant_name='Alpha Cafe')

    def test_the_statement_is_a_pdf(self):
        data = cam.statement_pdf(self.line)
        self.assertTrue(data.startswith(b'%PDF'))
        self.assertGreater(len(data), 1500)

    def test_sending_emails_it_from_admin_with_the_pdf_and_records_it(self):
        cam.send_statement(self.line)
        self.line.refresh_from_db()
        msg = mail.outbox[0]
        self.assertEqual((msg.to, msg.from_email, self.line.send_count, self.line.sent_to), (['alpha@tenant.example'], 'Proper Realty Co <admin@proper-realty.com>', 1, ['alpha@tenant.example']))
        self.assertEqual(msg.attachments[0][2], 'application/pdf')

    def test_a_second_send_is_refused_unless_asked_for(self):
        cam.send_statement(self.line)
        with self.assertRaises(InvoiceError):
            cam.send_statement(self.line)
        self.assertEqual(len(mail.outbox), 1)
        cam.send_statement(self.line, again=True)
        self.assertEqual((len(mail.outbox), CamLine.objects.get(pk=self.line.pk).send_count), (2, 2))

    def test_no_address_sends_and_records_nothing(self):
        self.a.tenant.email = ''
        self.a.tenant.save()
        with self.assertRaises(InvoiceError):
            cam.send_statement(self.line)
        self.assertEqual((len(mail.outbox), CamLine.objects.get(pk=self.line.pk).sent_at), (0, None))

    def test_a_draft_year_cannot_send_statements(self):
        CamReconciliation.objects.filter(pk=self.recon_obj.pk).update(status='draft')
        self.line.reconciliation.refresh_from_db()
        with self.assertRaises(InvoiceError):
            cam.send_statement(CamLine.objects.get(pk=self.line.pk))


@override_settings(STORAGES=STORAGES)
class ScreenTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def url(self, name, *args):
        return reverse(f'commercial:{name}', args=[self.prop.pk, *args])

    def test_the_cam_page_lists_the_pools_and_the_history(self):
        r = self.client.get(self.url('cam') + '?year=2026')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Building')

    def test_a_pool_is_made_and_its_shares_saved_from_the_form(self):
        parking = ledger.account(self.prop, '6420')
        post = {'year': '2026', 'name': 'Parking', 'basis': 'accrual', 'admin_fee_percent': '10', 'gross_up_percent': '', 'occupancy_override': '', 'denominator': 'all', 'denominator_sf': '',
                'accounts': [str(parking.pk)], f'in_{self.a.pk}': '1', f'm_{self.a.pk}': 'percent', f'v_{self.a.pk}': '60', f'pr_{self.a.pk}': 'on', f'cap_{self.a.pk}': '5', f'in_{self.b.pk}': '1', f'm_{self.b.pk}': ''}
        r = self.client.post(self.url('cam_pool_new') + '?year=2026', post)
        pool = CamPool.objects.get(property=self.prop, name='Parking')
        self.assertRedirects(r, self.url('cam_pool', pool.pk))
        self.assertEqual(pool.admin_fee_percent, D('10'))
        share = pool.shares.get(lease=self.a)
        self.assertEqual((share.method, share.percent, share.cap_percent), ('percent', D('60'), D('5')))
        self.assertFalse(pool.shares.filter(lease=self.b).exists())
        self.assertEqual(self.client.get(self.url('cam_pool', pool.pk)).status_code, 200)

    def test_a_percent_method_without_a_percent_is_refused_with_a_message(self):
        r = self.client.post(self.url('cam_pool_new') + '?year=2026', {'year': '2026', 'name': 'X', f'in_{self.a.pk}': '1', f'm_{self.a.pk}': 'percent', f'v_{self.a.pk}': ''}, follow=True)
        self.assertContains(r, 'type the percent')
        self.assertFalse(CamPool.objects.filter(name='X').exists())

    def test_the_reconcile_screen_shows_the_figures_and_approving_posts_once(self):
        r = self.client.get(self.url('cam_reconcile') + '?year=2026')
        self.assertContains(r, 'Alpha Cafe')
        self.assertContains(r, '6,000.00')
        post = {'year': '2026', 'action': 'approve', 'approval_date': '2027-01-20', 'due_days': '30', 'growth_percent': '0', 'confirm': 'yes'}
        self.client.post(self.url('cam_reconcile') + '?year=2026', post)
        recon = CamReconciliation.objects.get(property=self.prop, year=2026)
        self.assertEqual((recon.status, recon.lines.count()), ('approved', 2))
        self.client.post(self.url('cam_reconcile') + '?year=2026', post)
        self.assertEqual((Charge.objects.filter(description='CAM reconciliation 2026').count(), recon.lines.count()), (1, 2))

    def test_approving_needs_the_box_ticked(self):
        self.client.post(self.url('cam_reconcile') + '?year=2026', {'year': '2026', 'action': 'approve', 'approval_date': '2027-01-20', 'due_days': '30', 'growth_percent': '0'})
        self.assertEqual(CamReconciliation.objects.get(property=self.prop, year=2026).status, 'draft')

    def test_an_approved_year_shows_statements_and_the_pdf_opens(self):
        recon = cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        r = self.client.get(self.url('cam_reconcile') + '?year=2026')
        self.assertContains(r, 'Statement')
        line = recon.lines.first()
        pdf = self.client.get(self.url('cam_statement', line.pk))
        self.assertEqual((pdf.status_code, pdf['Content-Type']), (200, 'application/pdf'))

    def test_an_approved_year_cannot_have_its_pools_changed(self):
        cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        self.client.post(self.url('cam_pool', self.pool.pk), {'year': '2026', 'name': 'Renamed'})
        self.assertEqual(CamPool.objects.get(pk=self.pool.pk).name, 'Building')

    def test_undo_from_the_screen(self):
        cam.approve(self.recon(), approval_date=date(2027, 1, 20))
        self.client.post(self.url('cam_reconcile') + '?year=2026', {'year': '2026', 'action': 'undo'})
        self.assertEqual(CamReconciliation.objects.get(property=self.prop, year=2026).status, 'draft')

    def test_only_an_administrator_may_open_it(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url('cam')).status_code, 302)
        self.client.force_login(get_user_model().objects.create_user('clerk', 'c@example.com', 'pw12345678'))
        self.assertEqual(self.client.get(self.url('cam_reconcile')).status_code, 302)
        self.assertEqual(self.client.get(self.url('cam_pool_new')).status_code, 302)

    def test_an_association_has_no_cam_pages(self):
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        self.assertEqual(self.client.get(reverse('commercial:cam', args=[hoa.pk])).status_code, 404)

    def test_the_property_card_links_to_it(self):
        self.assertContains(self.client.get(reverse('property_detail', args=[self.prop.pk])), reverse('commercial:cam', args=[self.prop.pk]))
