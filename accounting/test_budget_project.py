import io
import json
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from core.models import Unit
from legalforms.models import GeneratedDocument

from . import budget_project as bp
from . import budgeting as bg
from . import ledger
from .models import Budget, BudgetLine, BudgetProject, ChargeRule, JournalRule, OwnerProfile
from .tests import close, make_assoc, owner_of, spend

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def pdf_pages(data):
    return PdfReader(io.BytesIO(data)).pages


def pdf_text(data):
    return '\n'.join(p.extract_text() or '' for p in pdf_pages(data))


@override_settings(STORAGES=STORAGES)
class BudgetProjectBase(TestCase):
    """An association with four units and owners, six closed months of 2026 spending, and the 2027 budget at the Board draft."""

    def setUp(self):
        call_command('seed_legal_templates', verbosity=0)
        self.prop = make_assoc('Project Condos')
        self.prop.street, self.prop.city, self.prop.state, self.prop.zip_code = '100 Main Street', 'Delray Beach', 'FL', '33444'
        self.prop.save()
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')
        close(self.prop, range(1, 7))
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)
        self.pk, self.year = self.prop.pk, 2027
        self.units = {u.label: u for u in Unit.objects.filter(property=self.prop)}
        self.owners = {l: owner_of(self.prop, u, f'Owner {l}') for l, u in self.units.items()}
        self.project_url = reverse('accounting:budget_project', args=[self.pk, self.year])
        self.packet_url = reverse('accounting:budget_packet', args=[self.pk, self.year])
        self.load_url = reverse('accounting:budget_load', args=[self.pk, self.year])
        self.edit_url = reverse('accounting:budget_edit', args=[self.pk, self.year])

    def act(self, action, **data):
        return self.client.post(self.project_url, dict(action=action, **data), follow=True)

    def bud(self):
        return Budget.objects.get(association=self.prop, year=self.year)

    def have(self):
        return bg.versions(self.bud())

    def project(self):
        return BudgetProject.objects.get(budget=self.bud())

    def to_board(self):
        self.act('start')
        self.act('to_board')

    def details(self, when=None):
        when = when or date(2027, 1, 20)
        self.act('details', meeting_date=when.isoformat(), meeting_time='6:00 p.m.', meeting_place='The clubhouse, 100 Main Street')

    def make_notice(self, kind='Condominium'):
        return self.client.post(self.packet_url, {'action': 'notice', 'association_type': kind}, follow=True)

    def mail(self, on=date(2026, 12, 20), **kw):
        data = dict(mailed_on=on.isoformat(), method='mailed by first class mail', by='Justin Browne', title='Property Manager')
        data.update(kw)
        return self.act('mailed', **data)

    def statuses(self):
        s = bp.step_list(self.prop, self.year, self.bud() if Budget.objects.filter(association=self.prop, year=self.year).exists() else None, self.have() if Budget.objects.filter(association=self.prop, year=self.year).exists() else {},
                         BudgetProject.objects.filter(budget__association=self.prop, budget__year=self.year).first())
        return {x['key']: x['status'] for x in s}


class TheStepsTests(BudgetProjectBase):
    def test_nothing_started_only_the_first_step_is_open(self):
        r = self.client.get(self.project_url)
        self.assertEqual(r.status_code, 200)
        steps = r.context['steps']
        self.assertEqual([s['key'] for s in steps], ['draft', 'board', 'mailing', 'mailed', 'signed', 'meeting', 'locked', 'loaded'])
        self.assertEqual([s['status'] for s in steps], ['todo'] * 8)
        self.assertEqual([s['enabled'] for s in steps], [True] + [False] * 7)
        self.assertContains(r, 'Start the 2027 budget')

    def test_the_steps_follow_the_budget_from_the_draft_to_the_loaded_year(self):
        self.act('start')
        self.assertEqual(self.statuses()['draft'], 'doing')
        self.act('to_board')
        st = self.statuses()
        self.assertEqual((st['draft'], st['board']), ('done', 'doing'))
        self.act('final')
        st = self.statuses()
        self.assertEqual((st['board'], st['mailing']), ('done', 'doing'))
        self.details()
        self.make_notice()
        st = self.statuses()
        self.assertEqual((st['mailing'], st['mailed']), ('done', 'doing'))
        self.mail()
        st = self.statuses()
        self.assertEqual((st['mailed'], st['meeting']), ('done', 'doing'))
        self.act('to_meeting')
        self.act('held', held_on='2027-01-20')
        self.assertEqual(self.statuses()['meeting'], 'done')
        self.act('lock')
        st = self.statuses()
        self.assertEqual((st['locked'], st['loaded']), ('done', 'doing'))
        self.client.post(self.load_url, {'confirm': '1', 'do_assessment': '1'})
        self.assertEqual(set(self.statuses().values()), {'done'})
        r = self.client.get(reverse('accounting:budgets') + '?year=2027')
        row = next(x for x in r.context['rows'] if x['assoc'].pk == self.pk)
        self.assertEqual((row['status'], row['label']), ('done', 'Complete'))

    def test_steps_open_in_order_and_a_done_step_stays_open(self):
        self.to_board()
        by = {s['key']: s for s in self.client.get(self.project_url).context['steps']}
        self.assertTrue(by['draft']['enabled'] and by['board']['enabled'])          # one done, one in progress
        self.assertTrue(by['mailing']['enabled'])                                    # the packet opens as soon as the Board draft exists: no separate "mark it final" first
        self.assertFalse(by['mailed']['enabled'] or by['meeting']['enabled'])       # the ones after it wait for the notice and the mailing
        self.act('final')
        by = {s['key']: s for s in self.client.get(self.project_url).context['steps']}
        self.assertTrue(by['mailing']['enabled'])
        self.assertTrue(by['draft']['enabled'])                                      # and the first is still open to go back to

    def test_the_packet_can_be_made_without_marking_the_board_draft_final_and_making_it_is_what_makes_it_final(self):
        self.to_board()
        packet = self.client.get(self.project_url)
        self.assertContains(packet, 'Make the packet')                              # the button is there with the Board draft not yet marked final
        self.details()
        self.make_notice()
        by = {s['key']: s for s in self.client.get(self.project_url).context['steps']}
        self.assertEqual((by['board']['status'], by['mailing']['status']), ('done', 'done'))
        self.assertIsNotNone(BudgetProject.objects.get(budget__association_id=self.pk, budget__year=2027).final_at)

    def test_the_meeting_details_can_be_entered_on_the_packet_page_itself(self):
        self.to_board()
        r = self.client.get(self.packet_url)
        self.assertContains(r, 'name="meeting_place"')
        self.client.post(self.packet_url, {'action': 'details', 'meeting_date': '2027-02-10', 'meeting_time': '6:00 p.m.', 'meeting_place': 'Clubhouse'})
        project = BudgetProject.objects.get(budget__association_id=self.pk, budget__year=2027)
        self.assertEqual((str(project.meeting_date), project.meeting_time, project.meeting_place), ('2027-02-10', '6:00 p.m.', 'Clubhouse'))
        self.assertContains(self.client.get(self.packet_url), 'value="Clubhouse"')

    def test_the_budgets_list_opens_the_project_and_names_the_next_step(self):
        self.to_board()
        r = self.client.get(reverse('accounting:budgets') + '?year=2027')
        row = next(x for x in r.context['rows'] if x['assoc'].pk == self.pk)
        self.assertEqual((row['status'], row['label'], row['detail']), ('attention', 'Step 2 of 8', 'Board draft'))
        self.assertContains(r, self.project_url)

    def test_the_meeting_and_the_lock_wait_for_what_comes_before(self):
        self.to_board()
        r = self.act('to_meeting')
        self.assertContains(r, 'Record the mailing first')
        self.assertNotIn('meeting', self.have())
        self.act('final')
        self.details()
        self.make_notice()
        self.mail()
        self.act('to_meeting')
        r = self.act('lock')
        self.assertContains(r, 'Mark the meeting over first')
        self.assertNotIn('approved', self.have())

    def test_reopening_the_approved_budget_takes_back_the_load(self):
        self.to_board()
        self.act('final')
        self.details()
        self.make_notice()
        self.mail()
        self.act('to_meeting')
        self.act('held', held_on='2027-01-20')
        self.act('lock')
        self.client.post(self.load_url, {'confirm': '1'})
        self.assertIsNotNone(self.project().loaded_at)
        self.act('reopen')
        self.assertNotIn('approved', self.have())
        self.assertIsNone(self.project().loaded_at)

    def test_a_budget_approved_on_the_old_screens_is_complete_and_looking_at_the_load_records_nothing(self):
        budget_url = reverse('accounting:budget_year', args=[self.pk, self.year])
        self.client.post(budget_url, {'action': 'start'})
        for _ in range(3):
            self.client.post(budget_url, {'action': 'advance'})
        self.assertFalse(BudgetProject.objects.exists())
        by = {s['key']: s for s in self.client.get(self.project_url).context['steps']}
        self.assertEqual((by['locked']['status'], by['loaded']['status']), ('done', 'done'))
        self.assertIn('before the steps were tracked', by['loaded']['detail'])
        row = next(x for x in self.client.get(reverse('accounting:budgets') + '?year=2027').context['rows'] if x['assoc'].pk == self.pk)
        self.assertEqual(row['label'], 'Complete')
        self.assertEqual(self.client.get(self.load_url).status_code, 200)
        self.assertFalse(BudgetProject.objects.exists())                       # the preview made no record, so the budget is still complete

    def test_the_meeting_opens_full_screen_with_a_way_back(self):
        self.to_board()
        self.act('final')
        self.details()
        self.make_notice()
        self.mail()
        self.act('to_meeting')
        r = self.client.get(self.project_url)
        self.assertContains(r, 'stage=meeting&amp;focus=1')
        page = self.client.get(reverse('accounting:budget_year', args=[self.pk, self.year]) + '?stage=meeting&focus=1')
        html = page.content.decode()
        self.assertTrue(page.context['focus'])
        self.assertIn('.brand-bar, .ws-lines { display: none', html)
        self.assertIn('2027 budget project', html)                                  # the way back
        self.assertIn('Show the menus', html)
        plain = self.client.get(reverse('accounting:budget_year', args=[self.pk, self.year]) + '?stage=meeting')
        self.assertNotIn('.brand-bar, .ws-lines { display: none', plain.content.decode())


class TheMailingListTests(BudgetProjectBase):
    def profile(self, label, **kw):
        return OwnerProfile.objects.update_or_create(contact=self.owners[label], defaults=kw)[0]

    def row(self, rows, label):
        return next(r for r in rows if r['unit'] == label)

    def test_the_name_as_on_the_deed_and_the_mailing_address(self):
        self.profile('100', legal_name='Pat Q. Owner, Trustee', mailing_address='55 Ocean Blvd\nBoca Raton, FL 33432')
        r = self.row(bp.mailing_rows(self.prop, date(2026, 12, 20)), '100')
        self.assertEqual((r['owner'], r['lines'], r['source']), ('Pat Q. Owner, Trustee', ['55 Ocean Blvd', 'Boca Raton, FL 33432'], 'mailing'))

    def test_the_seasonal_address_is_used_only_in_its_season(self):
        self.profile('101', mailing_address='1 Home St, Albany, NY 12207', seasonal_address='9 Palm Way\nDelray Beach, FL 33444', seasonal_from=date(2026, 11, 1), seasonal_to=date(2027, 4, 30))
        in_season = self.row(bp.mailing_rows(self.prop, date(2026, 12, 20)), '101')
        out = self.row(bp.mailing_rows(self.prop, date(2026, 7, 1)), '101')
        self.assertEqual((in_season['source'], in_season['lines'][0]), ('seasonal', '9 Palm Way'))
        self.assertEqual((out['source'], out['lines']), ('mailing', ['1 Home St', 'Albany, NY 12207']))

    def test_an_owner_with_no_mailing_address_is_sent_to_the_unit_and_flagged(self):
        r = self.row(bp.mailing_rows(self.prop), '102')
        self.assertEqual((r['source'], r['lines']), ('unit', ['100 Main Street Unit 102', 'Delray Beach, FL 33444']))
        self.assertIn('No mailing address on file', r['note'])
        self.assertIn('102', [x['unit'] for x in bp.flagged(bp.mailing_rows(self.prop))])
        self.assertEqual(len(bp.deliverable(bp.mailing_rows(self.prop))), 4)                 # the unit address still gets mail

    def test_a_unit_with_no_owner_and_no_address_at_all_are_flagged_and_not_sent(self):
        Unit.objects.create(property=self.prop, label='200')
        rows = bp.mailing_rows(self.prop)
        self.assertEqual(self.row(rows, '200')['source'], 'noowner')
        self.assertEqual(len(bp.deliverable(rows)), 4)
        self.prop.street = self.prop.city = self.prop.address = ''
        self.prop.save()
        none = self.row(bp.mailing_rows(self.prop), '103')
        self.assertEqual(none['source'], 'none')

    def test_an_address_on_one_line_is_split_for_the_envelope(self):
        self.assertEqual(bp.address_lines('7581 Mackenzie Ct, Lake Worth, FL 33467'), ['7581 Mackenzie Ct', 'Lake Worth, FL 33467'])
        self.assertEqual(bp.address_lines('1 Main St, Apt 4, Boca, FL 33432'), ['1 Main St, Apt 4', 'Boca, FL 33432'])
        self.assertEqual(bp.address_lines('A\n\nB, C'), ['A', 'B, C'])

    def test_the_list_as_a_pdf_and_a_csv(self):
        self.profile('100', mailing_address='55 Ocean Blvd, Boca Raton, FL 33432')
        rows = bp.mailing_rows(self.prop)
        text = pdf_text(bp.mailing_list_pdf(self.prop, 2027, rows))
        for word in ('Mailing list', 'Owner 100', '55 Ocean Blvd', 'Boca Raton, FL 33432', 'No mailing address on file'):
            self.assertIn(word, text)
        r = self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'mailing_csv']))        # before a packet exists the list is still there
        self.assertEqual(r.status_code, 404)                                                                           # (the packet needs a Board draft)
        self.to_board()
        r = self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'mailing_csv']))
        self.assertEqual(r['Content-Type'], 'text/csv; charset=utf-8')
        body = r.content.decode('utf-8-sig')
        self.assertIn('Unit,Owner,Address 1,Address 2,Address 3,Where from', body)
        self.assertIn('Owner 100', body)


class TheEnvelopesTests(BudgetProjectBase):
    def setUp(self):
        super().setUp()
        OwnerProfile.objects.create(contact=self.owners['100'], mailing_address='55 Ocean Blvd\nBoca Raton, FL 33432')
        self.rows = bp.mailing_rows(self.prop)

    def test_size_ten_is_the_envelope_itself_one_page_per_owner(self):
        pages = pdf_pages(bp.envelopes_pdf(self.prop, self.rows, None, 'no10'))
        self.assertEqual(len(pages), 4)
        self.assertEqual((round(float(pages[0].mediabox.width)), round(float(pages[0].mediabox.height))), (684, 297))      # 9.5 x 4.125 inches
        text = pages[0].extract_text()
        self.assertIn('Owner 100', text)
        self.assertIn('55 Ocean Blvd', text)
        self.assertIn('Project Condos', text)                                      # the return address

    def test_full_page_is_a_letter_sheet(self):
        pages = pdf_pages(bp.envelopes_pdf(self.prop, self.rows, None, 'full'))
        self.assertEqual(len(pages), 4)
        self.assertEqual((round(float(pages[0].mediabox.width)), round(float(pages[0].mediabox.height))), (612, 792))

    def test_a_test_page_is_one_page_and_says_so(self):
        pages = pdf_pages(bp.envelopes_pdf(self.prop, self.rows, None, 'no10', test=True))
        self.assertEqual(len(pages), 1)
        self.assertIn('TEST PAGE', pages[0].extract_text())

    def test_nobody_without_an_address_gets_an_envelope(self):
        Unit.objects.create(property=self.prop, label='200')                       # no owner
        self.assertEqual(len(pdf_pages(bp.envelopes_pdf(self.prop, bp.mailing_rows(self.prop), None, 'no10'))), 4)

    def test_the_return_address_is_editable_and_the_size_is_remembered(self):
        self.to_board()
        self.client.post(self.packet_url, {'action': 'return', 'return_address': 'The Board\nP.O. Box 5\nDelray Beach, FL 33444', 'envelope_size': 'full'})
        project = self.project()
        self.assertEqual((project.envelope_size, project.return_address), ('full', 'The Board\nP.O. Box 5\nDelray Beach, FL 33444'))
        r = self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'envelopes']))
        pages = pdf_pages(r.content)
        self.assertEqual(round(float(pages[0].mediabox.height)), 792)               # the saved size
        self.assertIn('P.O. Box 5', pages[0].extract_text())
        small = self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'envelopes']) + '?size=no10&test=1')
        self.assertEqual(len(pdf_pages(small.content)), 1)
        self.assertEqual(round(float(pdf_pages(small.content)[0].mediabox.height)), 297)


class TheNoticeAndAffidavitTests(BudgetProjectBase):
    def setUp(self):
        super().setUp()
        self.to_board()
        self.act('final')

    def test_the_notice_needs_the_meeting_details(self):
        r = self.make_notice()
        self.assertContains(r, 'Enter the date, time and place of the meeting first')
        self.assertIsNone(self.project().notice_doc)

    def test_the_notice_is_made_final_and_says_what_the_statute_asks(self):
        self.details(date(2027, 1, 20))
        self.make_notice()
        doc = self.project().notice_doc
        self.assertEqual((doc.status, doc.template.slug), ('final', 'budget-meeting-notice'))
        html = doc.body_html
        for word in ('NOTICE OF MEETING TO CONSIDER THE PROPOSED 2027 ANNUAL BUDGET', '§718.112(2)(e), Florida Statutes', 'Wednesday, January 20, 2027', '6:00 p.m.', 'The clubhouse, 100 Main Street',
                     'annual budget and the regular assessments', 'A copy of the proposed annual budget is enclosed', 'not less than 14 days before the meeting', 'The Board of Directors', 'unit owners'):
            self.assertIn(word, html)
        self.assertIn('$7,200', html)                                               # six months at $600, projected to a year
        self.assertIn('$150.00 a month', html)                                      # $7,200 over four units over twelve months
        self.assertEqual(self.project().packet_total, D('7200.00'))
        pdf = self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'notice']))
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertIn('NOTICE OF MEETING', pdf_text(pdf.content))

    def test_a_homeowners_association_gets_its_own_statute_and_word(self):
        self.details()
        self.make_notice('Homeowners association')
        html = self.project().notice_doc.body_html
        self.assertIn('§720.303(6), Florida Statutes', html)
        self.assertIn('members', html)
        self.assertNotIn('unit owners', html)

    def test_a_new_notice_replaces_the_one_for_the_packet_and_keeps_the_old(self):
        self.details()
        self.make_notice()
        first = self.project().notice_doc
        self.details(date(2027, 2, 3))
        self.make_notice()
        self.assertNotEqual(self.project().notice_doc_id, first.pk)
        self.assertTrue(GeneratedDocument.objects.filter(pk=first.pk).exists())

    def test_changing_the_meeting_after_the_notice_is_warned_about(self):
        self.details()
        self.make_notice()
        r = self.act('details', meeting_date='2027-02-03', meeting_time='7:00 p.m.', meeting_place='Pool house')
        self.assertContains(r, 'The notice was made with the old details')

    def test_the_packet_says_when_the_budget_changed_after_the_notice(self):
        self.details()
        self.make_notice()
        self.assertFalse(bp.changed_since_notice(self.prop, self.project(), self.have()))
        line = self.have()['board'].lines.get(account__code='6906')
        self.client.post(self.edit_url + '?stage=board', json.dumps({'op': 'set', 'line': line.pk, 'field': 'custom', 'value': '9000'}), content_type='application/json')
        self.assertTrue(bp.changed_since_notice(self.prop, self.project(), self.have()))
        self.assertContains(self.client.get(self.packet_url), 'The budget changed after this notice was made')

    def test_recording_the_mailing_makes_the_affidavit_with_the_count_and_keeps_the_list(self):
        self.details(date(2027, 1, 20))
        self.make_notice()
        r = self.mail(date(2026, 12, 20), posted_on='2026-12-21')
        self.assertContains(r, 'The mailing is recorded and the affidavit is made')
        project = self.project()
        aff = project.affidavit_doc
        self.assertEqual((aff.status, aff.template.slug, aff.parent_id), ('final', 'budget-mailing-affidavit', project.notice_doc_id))
        for word in ('AFFIDAVIT OF MAILING', 'Justin Browne', 'Property Manager', 'each of the 4 unit owners', 'mailed by first class mail', '12/20/2026', '1/20/2027', 'posted conspicuously', '12/21/2026', 'COUNTY OF PALM BEACH', '2027 annual budget'):
            self.assertIn(word, aff.body_html)
        self.assertEqual((project.mailed_on, project.mailed_count, project.mailed_by), (date(2026, 12, 20), 4, 'Justin Browne'))
        self.assertEqual(len(project.mailing_snapshot), 4)
        self.assertEqual(project.notice_doc.mailed_on, date(2026, 12, 20))
        self.assertEqual(bp.days_before(project), 31)
        pdf = self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'affidavit']))
        self.assertIn('AFFIDAVIT OF MAILING', pdf_text(pdf.content))

    def test_the_affidavit_signs_as_whoever_sent_it(self):
        self.details()
        self.make_notice()
        self.mail(by='Alexis McCarthy', title='Assistant Manager', count='3')
        project = self.project()
        self.assertEqual(project.mailed_count, 3)
        self.assertIn('Alexis McCarthy', project.affidavit_doc.body_html)
        self.assertIn('Assistant Manager', project.affidavit_doc.body_html)
        self.assertIn('each of the 3 unit owners', project.affidavit_doc.body_html)

    def test_mailing_less_than_fourteen_days_before_is_warned_about_not_blocked(self):
        self.details(date(2027, 1, 20))
        self.make_notice()
        r = self.mail(date(2027, 1, 10))
        self.assertContains(r, 'That is 10 days before the meeting')
        self.assertIsNotNone(self.project().mailed_on)

    def test_the_mailing_list_stays_as_it_went_out(self):
        self.details()
        self.make_notice()
        self.mail()
        self.owners['100'].name = 'Somebody Else'
        self.owners['100'].save()
        pdf = self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'mailing_list']))
        self.assertIn('Owner 100', pdf_text(pdf.content))
        self.assertNotIn('Somebody Else', pdf_text(pdf.content))
        self.assertContains(self.client.get(self.packet_url), 'As it went out on')

    def test_nobody_to_mail_to_is_refused(self):
        self.details()
        self.make_notice()
        from .models import Ownership
        Ownership.objects.all().delete()
        r = self.mail()
        self.assertContains(r, 'There is no owner with an address')
        self.assertIsNone(self.project().mailed_on)

    def test_the_packet_prints_in_two_groups_notice_and_budget_then_envelopes_and_no_affidavit(self):
        self.details()
        self.make_notice()
        g1 = reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'group1'])
        text1 = '\n'.join(p.extract_text() or '' for p in pdf_pages(self.client.get(g1).content))
        for word in ('NOTICE OF MEETING', 'Regular assessments'):                           # group 1: the notice, then the budget
            self.assertIn(word, text1)
        self.assertLess(text1.index('NOTICE OF MEETING'), text1.index('Regular assessments'))
        self.assertNotIn('AFFIDAVIT OF MAILING', text1)
        self.mail()
        self.assertEqual(self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'envelopes'])).status_code, 200)     # group 2 is its own file
        for gone in ('group2', 'everything'):                                              # the affidavit has its own step: there is no group for it
            self.assertEqual(self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, gone])).status_code, 404)
        html = self.client.get(self.packet_url).content.decode()
        self.assertIn('Print in two groups', html)
        self.assertNotIn('Affidavit of mailing', html)                                      # no affidavit card on the packet any more
        self.assertNotIn('Print in three groups', html)

    def test_the_pieces_that_need_the_notice_are_not_there_until_it_is_made(self):
        for what in ('notice', 'group1', 'group2', 'affidavit'):
            self.assertEqual(self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, what])).status_code, 404)
        self.assertEqual(self.client.get(reverse('accounting:budget_packet_file', args=[self.pk, self.year, 'budget'])).status_code, 200)       # the budget itself always is

    def test_the_packet_page_lists_every_piece(self):
        self.details()
        r = self.client.get(self.packet_url)
        html = r.content.decode()
        for word in ('Notice of the budget meeting', 'The proposed budget', 'Mailing list', 'Envelopes', 'Print in two groups', 'Notice and budget', 'Print one test page', 'Return address'):
            self.assertIn(word, html)
        self.assertEqual(r.context['sent'], 4)

    def test_the_packet_waits_for_a_board_draft(self):
        other = make_assoc('No Draft Condos')
        r = self.client.get(reverse('accounting:budget_packet', args=[other.pk, 2027]), follow=True)
        self.assertContains(r, 'which is not started yet')


class LoadingTheBudgetTests(BudgetProjectBase):
    def approve(self, reserves=False):
        self.act('start')
        if reserves:
            acct = ledger.account(self.prop, '3815')
            r = self.client.post(self.edit_url + '?stage=draft', json.dumps({'op': 'add', 'account': acct.pk}), content_type='application/json')
            line = next(x for x in r.json()['rows'] if x['code'] == '3815')
            self.client.post(self.edit_url + '?stage=draft', json.dumps({'op': 'set', 'line': line['id'], 'field': 'custom', 'value': '1200'}), content_type='application/json')
        self.act('to_board')
        self.act('final')
        self.details()
        self.make_notice()
        self.mail()
        self.act('to_meeting')
        self.act('held', held_on='2027-01-20')
        self.act('lock')

    def old_rule(self):
        return ChargeRule.objects.create(association=self.prop, name='Regular assessment', frequency='monthly', first_due=date(2026, 1, 1), method='flat', amount=D('120.00'),
                                         income_account=ledger.account(self.prop, '4500'), post_days_ahead=10)

    def test_it_cannot_be_loaded_before_the_budget_is_locked(self):
        self.to_board()
        r = self.client.get(self.load_url, follow=True)
        self.assertContains(r, 'Approve and lock the budget first')

    def test_the_preview_shows_the_numbers_and_changes_nothing(self):
        self.approve(reserves=True)
        self.old_rule()
        before = (ChargeRule.objects.count(), JournalRule.objects.count())
        r = self.client.get(self.load_url)
        plan = r.context['plan']
        self.assertEqual(plan['basis'], 'equal')
        self.assertEqual((plan['groups'][0]['monthly'], plan['groups'][0]['annual']), (D('175.00'), D('2100.00')))             # (7,200 + 1,200 reserves) over 4 units, 12 months
        self.assertEqual(plan['rule']['amount'], D('175.00'))
        self.assertEqual((plan['rule']['frequency'], plan['first_due'], plan['ends_on']), ('monthly', date(2027, 1, 1), date(2026, 12, 31)))
        self.assertEqual((plan['reserve']['total'], plan['reserve']['will_create']), (D('100.00'), True))
        html = r.content.decode()
        for word in ('$175.00', 'Jan 1, 2027', 'ends on Dec 31, 2026', 'This is what I mean to load', '$100.00'):
            self.assertIn(word, html)
        self.assertEqual((ChargeRule.objects.count(), JournalRule.objects.count()), before)
        self.assertIsNone(self.project().loaded_at)

    def test_nothing_happens_without_the_confirm_box(self):
        self.approve()
        self.old_rule()
        r = self.client.post(self.load_url, {'do_assessment': '1'}, follow=True)
        self.assertContains(r, 'Tick the box to confirm')
        self.assertEqual(ChargeRule.objects.count(), 1)
        self.assertIsNone(self.project().loaded_at)

    def test_loading_sets_the_year_s_assessment_and_the_reserve_and_ends_the_old_rule(self):
        self.approve(reserves=True)
        old = self.old_rule()
        r = self.client.post(self.load_url, {'confirm': '1', 'do_assessment': '1', 'do_reserve': '1'}, follow=True)
        self.assertContains(r, 'Loaded:')
        old.refresh_from_db()
        self.assertEqual(old.end_date, date(2026, 12, 31))
        new = ChargeRule.objects.exclude(pk=old.pk).get()
        self.assertEqual((new.first_due, new.amount, new.method, new.frequency, new.income_account.code), (date(2027, 1, 1), D('175.00'), 'flat', 'monthly', '4500'))
        self.assertEqual(JournalRule.objects.filter(association=self.prop, kind='reserve').count(), 1)
        project = self.project()
        self.assertIsNotNone(project.loaded_at)
        self.assertIn('$175.00 a unit', project.loaded_note)
        self.assertTrue(BudgetLine.objects.filter(budget=self.bud(), account__code='6906').exists())                          # the budget is the year's budget in the books
        again = self.client.get(self.load_url)
        self.assertTrue(again.context['plan']['already'])                                                                     # and a second load does not make another rule
        self.client.post(self.load_url, {'confirm': '1', 'do_assessment': '1', 'do_reserve': '1'})
        self.assertEqual(ChargeRule.objects.count(), 2)
        self.assertEqual(JournalRule.objects.filter(association=self.prop).count(), 1)

    def test_each_part_can_be_left_out(self):
        self.approve(reserves=True)
        old = self.old_rule()
        self.client.post(self.load_url, {'confirm': '1'})
        self.assertEqual(ChargeRule.objects.count(), 1)
        old.refresh_from_db()
        self.assertIsNone(old.end_date)
        self.assertEqual(JournalRule.objects.count(), 0)
        self.assertIsNotNone(self.project().loaded_at)                                                                        # the budget itself is loaded

    def test_a_quarterly_rule_gets_a_quarterly_amount_and_a_share_rule_stays_by_share(self):
        self.approve()
        ChargeRule.objects.create(association=self.prop, name='Regular assessment', frequency='quarterly', first_due=date(2026, 1, 1), method='flat', amount=D('300.00'), income_account=ledger.account(self.prop, '4500'))
        self.assertEqual(self.client.get(self.load_url).context['plan']['rule']['amount'], D('450.00'))                         # 7,200 over 4 quarters over 4 units
        from . import owners
        from .models import UnitShare
        kind = owners.default_share_kind(self.prop)
        for u, pct in zip(Unit.objects.filter(property=self.prop).order_by('label'), ('40', '30', '20', '10')):
            UnitShare.objects.update_or_create(unit=u, kind=kind, defaults={'percent': D(pct)})
        ChargeRule.objects.filter(association=self.prop).update(method='share')
        plan = self.client.get(self.load_url).context['plan']
        self.assertEqual(plan['basis'], 'share')
        self.assertEqual((plan['rule']['method'], plan['rule']['amount']), ('share', D('1800.00')))                              # the quarter's total, to be split
        self.assertEqual(len(plan['groups']), 4)

    def test_no_units_is_said_clearly_not_a_crash(self):
        self.approve()
        Unit.objects.filter(property=self.prop).update(is_active=False)
        r = self.client.get(self.load_url)
        self.assertContains(r, 'has no units yet')
        self.assertContains(self.client.post(self.load_url, {'confirm': '1'}, follow=True), 'no units yet')

    def test_a_budget_that_needs_nothing_loads_without_a_rule(self):
        self.approve()
        v = self.have()['approved']
        v.lines.update(method='none', adjust='-3600', meeting_amount=None)          # the 3,600 spent so far taken back out: the budget comes to nothing
        plan = self.client.get(self.load_url).context['plan']
        self.assertTrue(plan['problems'])
        self.assertIn('needs no assessments', plan['problems'][0])


class MeetingNoticeFromTheChecklistTests(TheStepsTests):
    """The notice of the budget meeting is made right on the checklist (an association), and a commercial property only needs a meeting date and time."""

    def test_the_checklist_offers_the_notice_once_the_meeting_details_are_in(self):
        self.to_board()
        html = self.client.get(self.project_url).content.decode()
        self.assertIn('Enter the meeting date, time and place above first.', html)
        self.assertNotIn('Make the meeting notice', html)
        self.act('details', meeting_date='2027-01-20', meeting_time='6:00 p.m.', meeting_place='Clubhouse')
        html = self.client.get(self.project_url).content.decode()
        self.assertIn('Make the meeting notice', html)
        self.assertIn('Meeting notice and final draft for mailing', html)

    def test_pressing_it_makes_the_notice_and_it_can_be_viewed_from_the_checklist(self):
        self.to_board()
        self.act('details', meeting_date='2027-01-20', meeting_time='6:00 p.m.', meeting_place='Clubhouse')
        r = self.act('notice', association_type='Condominium')
        self.assertContains(r, 'The notice is made')
        from .models import BudgetProject
        self.assertTrue(BudgetProject.objects.get().notice_doc_id)
        html = self.client.get(self.project_url).content.decode()
        self.assertIn('View the notice', html)
        self.assertIn('Make a new notice', html)

    def test_it_is_refused_until_the_meeting_has_a_date_time_and_place(self):
        self.to_board()
        r = self.act('notice')
        self.assertContains(r, 'Enter the date, time and place of the meeting first')


class CommercialBudgetChecklistTests(TestCase):
    """A commercial property: no mailed notice, packet, affidavit or place; just a meeting date and time."""

    def setUp(self):
        from core.models import Property
        from .tests import close, make_assoc, spend
        self.prop = make_assoc('Commercial Plaza')
        Property.objects.filter(pk=self.prop.pk).update(property_type=Property.Type.COMMERCIAL)
        self.prop.refresh_from_db()
        from datetime import date
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')
        close(self.prop, range(1, 7))
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:budget_project', args=[self.prop.pk, 2027])

    def test_it_has_five_steps_and_none_about_mailing(self):
        from . import budget_project as bp
        from . import budgeting as bg
        bud = bg.get_budget(self.prop, 2027)
        steps = bp.step_list(self.prop, 2027, bud, bg.versions(bud), bp.get_project(bud))
        self.assertEqual([s['key'] for s in steps], ['draft', 'board', 'meeting', 'locked', 'loaded'])
        self.assertEqual([s['title'] for s in steps][:3], ['Management draft', 'Owner draft', 'The meeting'])

    def test_the_page_asks_only_for_a_date_and_a_time(self):
        self.client.post(self.url, {'action': 'start'})
        html = self.client.get(self.url).content.decode()
        self.assertIn('name="meeting_date"', html)
        self.assertIn('name="meeting_time"', html)
        self.assertNotIn('name="meeting_place"', html)
        for gone in ('Make the meeting notice', 'Make the packet', 'Mailed to the owners', 'Affidavit'):
            self.assertNotIn(gone, html)

    def test_the_meeting_version_needs_no_mailing_and_there_is_no_notice_or_packet(self):
        self.client.post(self.url, {'action': 'start'})
        self.client.post(self.url, {'action': 'to_board'})
        self.client.post(self.url, {'action': 'details', 'meeting_date': '2027-01-20', 'meeting_time': '6:00 p.m.'})
        r = self.client.post(self.url, {'action': 'to_meeting'}, follow=True)
        self.assertContains(r, 'The meeting version is ready')
        r = self.client.post(self.url, {'action': 'notice'}, follow=True)
        self.assertContains(r, 'no mailed notice')
        r = self.client.get(reverse('accounting:budget_packet', args=[self.prop.pk, 2027]), follow=True)
        self.assertContains(r, 'no mailing packet')

    def test_the_meeting_step_shows_the_date_and_time(self):
        from . import budget_project as bp
        from . import budgeting as bg
        self.client.post(self.url, {'action': 'start'})
        self.client.post(self.url, {'action': 'to_board'})
        self.client.post(self.url, {'action': 'details', 'meeting_date': '2027-01-20', 'meeting_time': '6:00 p.m.'})
        self.client.post(self.url, {'action': 'to_meeting'})
        bud = bg.get_budget(self.prop, 2027)
        steps = {s['key']: s for s in bp.step_list(self.prop, 2027, bud, bg.versions(bud), bp.get_project(bud))}
        self.assertIn('Jan 20, 2027, 6:00 p.m.', steps['meeting']['detail'])


class UnlockingAFrozenVersionTests(TheStepsTests):
    """The first stage is the management draft. A version is frozen once the next stage is started from it; unlocking it takes the later versions away so it can be changed."""

    def board_url(self):
        return reverse('accounting:budget_year', args=[self.prop.pk, self.year])

    def test_the_first_stage_is_called_the_management_draft_everywhere(self):
        from . import budgeting as bg
        self.assertEqual(bg.STAGE_LABEL['draft'], 'Management draft')
        self.to_board()
        html = self.client.get(self.project_url).content.decode()
        self.assertIn('Management draft', html)
        self.assertIn('Open the management draft', html)
        self.assertNotIn('Management company draft', html)

    def test_a_frozen_management_draft_has_an_unlock_button_that_says_what_it_removes(self):
        self.to_board()
        html = self.client.get(self.board_url() + '?stage=draft').content.decode()
        self.assertIn('This version is frozen', html)
        self.assertIn('Unlock the Management draft', html)
        self.assertIn('This removes the Board draft that was started from it', html)

    def test_an_open_version_has_no_unlock_button(self):
        self.to_board()
        html = self.client.get(self.board_url() + '?stage=board').content.decode()
        self.assertNotIn('Unlock the', html)

    def test_unlocking_the_management_draft_removes_the_board_draft_and_makes_it_editable(self):
        from . import budgeting as bg
        from .models import Budget
        self.to_board()
        r = self.client.post(self.board_url(), {'action': 'unlock', 'stage': 'draft'}, follow=True)
        self.assertContains(r, 'Management draft is open again')
        have = bg.versions(Budget.objects.get(association=self.prop, year=2027))
        self.assertEqual(list(have), ['draft'])
        self.assertTrue(bg.is_editable(have['draft'], have))
        self.client.post(self.project_url, {'action': 'to_board'})                          # and the Board draft can be started again from the changed draft
        self.assertEqual(list(bg.versions(Budget.objects.get(association=self.prop, year=2027))), ['draft', 'board'])

    def test_unlocking_the_board_draft_removes_the_meeting_version_and_the_held_date(self):
        from . import budgeting as bg
        from .models import Budget, BudgetProject
        self.to_board()
        self.act('details', meeting_date='2027-01-20', meeting_time='6:00 p.m.', meeting_place='Clubhouse')
        self.act('notice', association_type='Condominium')
        self.mail()
        self.act('to_meeting')
        BudgetProject.objects.update(held_on=date(2027, 1, 20))
        r = self.client.post(self.board_url(), {'action': 'unlock', 'stage': 'board'}, follow=True)
        self.assertContains(r, 'Board draft is open again')
        have = bg.versions(Budget.objects.get(association=self.prop, year=2027))
        self.assertEqual(list(have), ['draft', 'board'])
        self.assertIsNone(BudgetProject.objects.get().held_on)

    def test_a_board_draft_already_mailed_cannot_be_taken_away(self):
        from . import budgeting as bg
        from .models import Budget
        self.to_board()
        self.act('details', meeting_date='2027-01-20', meeting_time='6:00 p.m.', meeting_place='Clubhouse')
        self.act('notice', association_type='Condominium')
        self.mail()
        r = self.client.post(self.board_url(), {'action': 'unlock', 'stage': 'draft'}, follow=True)
        self.assertContains(r, 'already mailed to the owners')
        self.assertEqual(list(bg.versions(Budget.objects.get(association=self.prop, year=2027))), ['draft', 'board'])

    def test_the_project_page_has_the_unlock_too_and_marking_final_is_undone(self):
        from . import budgeting as bg
        from .models import Budget, BudgetProject
        self.to_board()
        self.act('final')
        self.assertIsNotNone(BudgetProject.objects.get().final_at)
        self.assertIn('Unlock it', self.client.get(self.project_url).content.decode())
        r = self.act('unlock', stage='draft')
        self.assertContains(r, 'Management draft is open again')
        self.assertIsNone(BudgetProject.objects.get().final_at)
        self.assertEqual(list(bg.versions(Budget.objects.get(association=self.prop, year=2027))), ['draft'])


class SignedAffidavitStepTests(TheNoticeAndAffidavitTests):
    """After the mailing is recorded: print the affidavit, sign it before a notary, scan it and upload the signed copy. That is its own step, after "Mailed to the owners"."""

    def scan(self, name='signed.pdf', size=2000):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, b'%PDF-1.4 ' + b'x' * size, content_type='application/pdf')

    def upload(self, file):
        return self.client.post(self.project_url, {'action': 'signed_affidavit', 'file': file}, follow=True)

    def steps(self):
        return {s['key']: s for s in bp.step_list(self.prop, self.year, self.bud(), self.have(), BudgetProject.objects.filter(budget__association=self.prop, budget__year=self.year).first())}

    def ready(self):
        self.details()
        self.make_notice()
        self.mail()

    def test_the_step_comes_right_after_mailed_and_waits_for_the_mailing(self):
        self.to_board()
        keys = list(self.steps())
        self.assertEqual(keys.index('signed'), keys.index('mailed') + 1)
        self.assertEqual(self.steps()['signed']['status'], 'todo')
        self.ready()
        st = self.steps()
        self.assertEqual(st['signed']['status'], 'doing')
        self.assertEqual(st['signed']['title'], 'Signed and notarized affidavit')
        self.assertIn('Print the affidavit, sign it before a notary', st['signed']['detail'])

    def test_the_page_offers_the_print_and_the_upload_once_the_affidavit_is_made(self):
        self.ready()
        html = self.client.get(self.project_url).content.decode()
        self.assertIn('Print the affidavit', html)
        self.assertIn('name="action" value="signed_affidavit"', html)
        self.assertIn('Upload the signed copy', html)

    def test_uploading_the_scan_completes_the_step_and_keeps_the_file(self):
        self.ready()
        r = self.upload(self.scan())
        self.assertContains(r, 'The signed affidavit is saved')
        project = BudgetProject.objects.get()
        self.assertTrue(project.signed_affidavit)
        self.assertIsNotNone(project.signed_uploaded_at)
        self.assertEqual(self.steps()['signed']['status'], 'done')
        html = self.client.get(self.project_url).content.decode()
        self.assertIn('View the signed copy', html)
        self.assertIn('Replace the signed copy', html)

    def test_it_is_refused_before_the_mailing_and_for_the_wrong_kind_of_file_or_a_file_that_is_too_big(self):
        self.to_board()
        self.details()
        self.make_notice()
        r = self.upload(self.scan())
        self.assertContains(r, 'Record the mailing first')
        self.mail()
        self.assertContains(self.upload(self.scan('notes.docx')), 'PDF, a picture or a text file' if False else 'is not a PDF')
        self.assertContains(self.upload(self.scan('notes.txt')), 'Upload the scan as a PDF or a picture')
        from . import documents
        big = self.scan('huge.pdf', documents.STORE_LIMIT + 10)
        self.assertContains(self.upload(big), 'Scan it again at a lower quality')
        self.assertFalse(BudgetProject.objects.get().signed_affidavit)

    def test_the_meeting_does_not_wait_for_the_signed_copy(self):
        self.ready()
        st = self.steps()
        self.assertTrue(st['meeting']['enabled'])                                         # the notary can take their time
        r = self.act('to_meeting')
        self.assertContains(r, 'The meeting version is ready')

    def test_the_replacement_is_kept_and_an_approved_budget_never_nags_for_it(self):
        self.ready()
        self.upload(self.scan('first.pdf'))
        first = BudgetProject.objects.get().signed_affidavit.name
        self.upload(self.scan('second.pdf'))
        self.assertNotEqual(BudgetProject.objects.get().signed_affidavit.name, first)
        self.assertEqual(self.steps()['signed']['status'], 'done')
