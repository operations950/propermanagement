"""The shared column dealing of the newspaper screens: boxes keep their ORDER (it has a meaning), flow down column one, then two, then three, as even as that allows, any extra length in column one."""
from django.test import SimpleTestCase

from core.newspaper import box, deal
from core.property_layout import box_height, pack_sequential


def fake(title, n):
    return {'title': title, 'rows': [object()] * n}


def heights(cols):
    return [round(sum(box_height(b) for b in col), 1) for col in cols]


class PackSequentialTests(SimpleTestCase):
    def test_the_boxes_keep_their_order_down_and_across(self):
        boxes = [fake(f'B{i}', n) for i, n in enumerate([2, 9, 3, 4, 8, 1, 6])]
        cols = pack_sequential(boxes)
        self.assertEqual([b['title'] for col in cols for b in col], [b['title'] for b in boxes])         # reading column by column is the original order

    def test_the_columns_come_out_about_even(self):
        cols = pack_sequential([fake(f'B{i}', n) for i, n in enumerate([5, 5, 5, 5, 5, 5, 5, 5, 5])])
        h = heights(cols)
        self.assertLessEqual(max(h) - min(h), 1.0)

    def test_extra_length_goes_in_the_first_column(self):
        for sizes in ([4, 4, 4, 4], [3, 3, 3, 3, 3, 3, 3, 3, 3, 3], [10, 2, 2, 2, 2]):
            h = heights(pack_sequential([fake(f'B{i}', n) for i, n in enumerate(sizes)]))
            self.assertGreaterEqual(h[0] + 0.01, h[1], (sizes, h))
            self.assertGreaterEqual(h[1] + 0.01, h[2], (sizes, h))

    def test_a_long_box_stays_whole_and_the_others_flow_around_it(self):
        cols = pack_sequential([fake('Urgent', 2), fake('Overdue', 14), fake('Today', 3), fake('Tomorrow', 2), fake('This week', 6)])
        self.assertEqual([b['title'] for b in cols[0]][0], 'Urgent')
        self.assertEqual(sum(len(c) for c in cols), 5)

    def test_few_boxes_or_none(self):
        self.assertEqual(pack_sequential([]), [[], [], []])
        self.assertEqual([len(c) for c in pack_sequential([fake('only', 4)])], [1, 0, 0])
        two = pack_sequential([fake('a', 6), fake('b', 6)])
        self.assertEqual(sorted(len(c) for c in two), [0, 1, 1])

    def test_two_columns_and_one_column(self):
        boxes = [fake(f'B{i}', 5) for i in range(4)]
        self.assertEqual([len(c) for c in pack_sequential(boxes, 2)], [2, 2])
        self.assertEqual([len(c) for c in pack_sequential(boxes, 1)], [4])

    def test_many_boxes_are_dealt_quickly(self):
        # a ticket list can have a dozen boxes: cut points are tried, not every possible deal
        cols = pack_sequential([fake(f'B{i}', (i * 7) % 9 + 1) for i in range(30)])
        self.assertEqual(sum(len(c) for c in cols), 30)


class DealTests(SimpleTestCase):
    def test_deal_numbers_the_boxes_for_the_phone_and_keeps_them_in_order(self):
        boxes = [box('Urgent', [1], tone='urgent'), box('Overdue', [1, 2, 3]), box('Today', [1])]
        cols = deal(boxes)
        self.assertEqual([b['order'] for b in boxes], [0, 1, 2])
        self.assertEqual([b['title'] for c in cols for b in c], ['Urgent', 'Overdue', 'Today'])

    def test_box_makes_a_key_from_the_title_and_keeps_the_tone_and_hint(self):
        b = box('This week', [], hint='Nov 15 – Nov 21', tone='late')
        self.assertEqual((b['key'], b['tone'], b['hint']), ('this-week', 'late', 'Nov 15 – Nov 21'))
