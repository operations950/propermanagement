"""The shared pieces of the "newspaper" screens (Tickets, Compliance, Projects, and the model they copy, Properties): small boxes, each a titled list of compact rows, flowing down three columns.

A screen builds a list of boxes (`box(...)`), deals them into columns with `deal(boxes)` and renders core/_np_paper.html (with its own row template). The look is static/css/newspaper.css, the
header / tabs / search-and-filters row are the partials core/_np_header.html, _np_tabs.html and _np_controls.html, and the little script is static/js/newspaper.js, so the three screens
cannot drift apart. The Properties screen keeps its own (older) markup; it shares core/property_layout.py, which deals its boxes.
"""
from .property_layout import pack_sequential


def box(title, rows, hint='', key='', tone=''):
    """One box of a newspaper screen. `tone` ('urgent', 'late') tints its title bar; `hint` is a small line beside the title (a date range)."""
    return {'title': title, 'rows': list(rows), 'hint': hint, 'key': key or title.lower().replace(' ', '-'), 'tone': tone}


def deal(boxes, columns=3):
    """The boxes dealt into `columns` lists, kept IN ORDER (they have a meaning in order: Urgent, Overdue, Today ...; Daily, Weekly ...) so that reading down the first column and on to the
    next follows it, with the columns as even as that allows and any extra length in the first. Each box also carries its `order`, which a phone uses to stack them in one column."""
    for index, one in enumerate(boxes):
        one['order'] = index
    return pack_sequential(boxes, columns)
