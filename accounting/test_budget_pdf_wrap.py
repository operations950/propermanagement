from datetime import date

from django.test import SimpleTestCase
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Paragraph

from . import budget_export as be


def _line(name, a, b, c):
    d = c - a
    return {'name': name, 'prior': a, 'projected': b, 'proposed': c, 'd': d, 'p': None if not a else d / a * 100, 'tone': '', 'ptone': ''}


class NothingBleedsIntoAnotherColumnTests(SimpleTestCase):
    """On the printed sheet a long account name wraps onto the next line inside its own column, and a figure too wide for its column is set smaller; neither runs into the next column."""

    def test_a_name_is_a_wrapping_paragraph_with_its_text_escaped(self):
        cell = be._label_cell('Insurance - Hazard, Liability & Property <master policy>')
        self.assertIsInstance(cell, Paragraph)
        cell.wrapOn(None, be.COLS[0] - be.PAD, 100)
        self.assertGreater(len(cell.blPara.lines), 1)                      # too long for one line, so it took more than one
        self.assertIn('&amp;', cell.text)

    def test_a_figure_that_fits_stays_as_it_is_and_one_that_does_not_is_made_smaller(self):
        self.assertEqual(be._fit('$16,800', be.COLS[1]), '$16,800')
        wide = '$3,456,789,012,345.00'
        self.assertGreater(stringWidth(wide, 'Helvetica', 8.5), be.COLS[1] - be.PAD)
        cell = be._fit(wide, be.COLS[1])
        self.assertIsInstance(cell, Paragraph)
        self.assertLessEqual(stringWidth(wide, 'Helvetica', cell.style.fontSize), be.COLS[1] - be.PAD + 0.01)

    def test_the_whole_sheet_builds_with_very_long_names_in_both_tables(self):
        lines = [_line('Estimated Prior-Year Operating Fund Balance Appropriated and Carried Forward Into The Coming Fiscal Year', 0, 0, 40000), _line('Management Fees', 16800, 16800, 16800)]
        total = _line('Total administrative', 16800, 16800, 56800)
        sheet = {'assoc': 'Test Condos', 'year': 2027, 'stage_label': 'Board draft', 'units': 10, 'proj_word': 'projected', 'cents': False, 'on': date(2026, 11, 14),
                 'sections': [dict(total, name='Administrative', key='Administrative', lines=lines)], 'op': total, 'total': total,
                 'calc': [{'header': 'All units', 'units': '10 units', 'rows': [{'label': 'Per unit per month, including the reserve contribution and every one-time item on this sheet', 'old': 100.0, 'new': 110.0,
                                                                                'd': 10.0, 'p': 10.0, 'tone': '', 'ptone': '', 'bold': True}]}]}
        data = be.pdf(sheet)
        self.assertTrue(data.startswith(b'%PDF'))
        be.xlsx(sheet)                                                       # the Excel still builds (its name column wraps too)
