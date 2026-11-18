"""How the boxes of the Properties screen are dealt into columns so the columns come out about the same height, like a justified newspaper.

Every box is a header plus one row for each property, so its height is simply its number of rows (a header counts as a little more than a row, and there is a gap under each box). With only a
handful of boxes every way of dealing them into the columns can be tried, so nothing here is a guess: the best deal wins. Best means, in this order:
  1. the columns are as even as possible (a difference of less than a row counts as even);
  2. when they cannot be exactly even, the extra length is in the first column, then the second (the first column is never shorter than the second, nor the second than the third);
  3. the plain difference in height;
  4. the boxes stay as close to their usual order (Associations first) as the above allows.
Inside a column the boxes keep their usual order. Nothing about a box (its title, its properties) is changed.
"""
from itertools import combinations_with_replacement, product

HEADER_ROWS = 1.6          # a box's title bar, in rows
GAP_ROWS = 0.7             # the space under a box, in rows


def box_height(box):
    return len(box['rows']) + HEADER_ROWS + GAP_ROWS


def _heights(deal, boxes, columns):
    totals = [0.0] * columns
    for index, col in enumerate(deal):
        totals[col] += box_height(boxes[index])
    return totals


def _score(deal, boxes, columns):
    heights = _heights(deal, boxes, columns)
    spread = max(heights) - min(heights)
    # the extra length belongs in the first column: how far the columns are from running longest to shortest, left to right
    out_of_order = sum(max(0.0, heights[i + 1] - heights[i]) for i in range(columns - 1))
    reading = sorted(range(len(deal)), key=lambda i: (deal[i], i))          # the boxes in the order a person reads the columns
    inversions = sum(1 for a in range(len(reading)) for b in range(a + 1, len(reading)) if reading[a] > reading[b])
    return (int(spread + 0.9999), round(out_of_order, 6), round(spread, 6), inversions)


def pack_columns(boxes, columns=3):
    """The boxes dealt into `columns` lists (each in the boxes' usual order) so that the columns are about the same height, with any extra length in the first column."""
    if columns < 2 or len(boxes) <= 1:
        return [list(boxes)] + [[] for _ in range(max(columns, 1) - 1)]
    best = min(product(range(columns), repeat=len(boxes)), key=lambda deal: _score(deal, boxes, columns))
    out = [[] for _ in range(columns)]
    for index, col in enumerate(best):
        out[col].append(boxes[index])
    return out


def pack_sequential(boxes, columns=3):
    """The boxes dealt into `columns` lists WITHOUT reordering them: the first run of boxes goes in column one, the next run in column two, and so on, like a newspaper's text running
    down one column and on to the next. For screens whose boxes have a meaning in order (Urgent, Overdue, Today ... ; Daily, Weekly, Monthly ...) where reading down then across must keep that
    order. The cut points are chosen like pack_columns chooses a deal: as even as possible, the extra length in the first column, then the plain difference."""
    columns = max(columns, 1)
    if columns == 1 or len(boxes) <= 1:
        return [list(boxes)] + [[] for _ in range(columns - 1)]
    best, best_score = None, None
    for cuts in combinations_with_replacement(range(len(boxes) + 1), columns - 1):
        edges = (0,) + cuts + (len(boxes),)
        parts = [boxes[edges[i]:edges[i + 1]] for i in range(columns)]
        heights = [sum(box_height(b) for b in part) for part in parts]
        spread = max(heights) - min(heights)
        out_of_order = sum(max(0.0, heights[i + 1] - heights[i]) for i in range(columns - 1))
        score = (int(spread + 0.9999), round(out_of_order, 6), round(spread, 6))
        if best_score is None or score < best_score:
            best, best_score = parts, score
    return [list(part) for part in best]
