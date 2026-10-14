"""Galaxy Cinemas - ER diagram generator (no libraries, pure string SVG).

    python make_er.py            ->  writes static/er.svg and validates it

What it draws
    * 17 entity boxes with every attribute, marked  PK  /  FK  /  U
    * crow's-foot cardinalities (one = single bar, many = three-toed foot,
      optional = small circle) on 20 relationships, each with a verb label
    * a legend explaining every marker

It then CHECKS the drawing and fails loudly (exit code 1) if
    * any connector segment crosses an entity box, or
    * any relationship label overlaps a box or another label.
The router tries several candidate paths per relationship (straight line, Z in
the gap between columns, a detour through the free channel above or below the
grid) and keeps the first one that is completely clean.
"""

from __future__ import annotations

import html
import pathlib
import sys
from collections import OrderedDict


def xml_escape(s: str) -> str:
    """Escape text that will be placed inside an SVG <text> element.

    The crow's-foot glyphs (``|--<``, ``o--<``) and any ``<``/``>`` in
    attribute names or verb labels would otherwise produce malformed XML.
    """
    return html.escape(s, quote=False)

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "static" / "er.svg"

# ---------------------------------------------------------------- geometry ---
BOX_W = 306
ROW_H = 19
TITLE_H = 32
PAD_BOTTOM = 9
COL_X = [40, 486, 932, 1378, 1824]
COLUMN_GAP = 60
TOP_MARGIN = 118
COLUMN_Y = 118
BOX_GAP = 76

# --------------------------------------------------------------- entities ----
# (column column index, attribute, marker)   marker in {PK, FK, U, combo, ''}
ENTITIES = OrderedDict([
    ("cinema", (0, [
        ("cinema_id", "PK"), ("cinema_name", "U"), ("city", "U+U"), ("address", ""),
        ("phone", "")])),
    ("screen", (0, [
        ("screen_id", "PK"), ("cinema_id", "FK"), ("screen_name", "U"),
        ("screen_type", "CHECK")])),
    ("seat", (0, [
        ("seat_id", "PK"), ("screen_id", "FK"), ("row_label", "U+U"),
        ("seat_no", "U+U"), ("seat_type", "CHECK")])),
    ("movie", (1, [
        ("movie_id", "PK"), ("title", "U+U"), ("genre", ""), ("language", "U+U"),
        ("duration_min", "CHECK"), ("certificate", "CHECK"), ("release_date", "U+U")])),
    ("screening", (1, [
        ("screening_id", "PK"), ("movie_id", "FK"), ("screen_id", "FK"),
        ("start_time", "U+U"), ("end_time", ""), ("base_price", "CHECK"),
        ("status", "CHECK")])),
    ("booking", (1, [
        ("booking_id", "PK"), ("customer_id", "FK"), ("screening_id", "FK"),
        ("staff_id", "FK*"), ("booking_time", ""), ("status", "CHECK")])),
    ("customer", (2, [
        ("customer_id", "PK"), ("full_name", ""), ("email", "U"),
        ("phone", "CHECK"), ("created_at", "")])),
    ("booked_seat", (2, [
        ("booked_seat_id", "PK"), ("booking_id", "FK"), ("screening_id", "FK"),
        ("seat_id", "FK"), ("status", "CHECK")])),
    ("ticket", (2, [
        ("ticket_id", "PK"), ("booked_seat_id", "FK+U"), ("ticket_code", "U"),
        ("price", "CHECK"), ("issued_at", ""), ("status", "CHECK")])),
    ("cancellation", (2, [
        ("cancellation_id", "PK"), ("booking_id", "FK+U"), ("cancelled_at", ""),
        ("cancelled_by", "CHECK"), ("reason", "")])),
    ("staff", (3, [
        ("staff_id", "PK"), ("cinema_id", "FK"), ("full_name", ""),
        ("username", "U"), ("password_hash", ""), ("role", "CHECK")])),
    ("refund", (3, [
        ("refund_id", "PK"), ("cancellation_id", "FK"), ("payment_id", "FK"),
        ("refund_amount", "CHECK"), ("refund_status", "CHECK"), ("refunded_at", "")])),
    ("payment", (3, [
        ("payment_id", "PK"), ("booking_id", "FK*"), ("order_id", "FK*"),
        ("amount", "CHECK"), ("method", "CHECK"), ("status", "CHECK"),
        ("paid_at", ""), ("transaction_ref", "U")])),
    ("concession_order", (3, [
        ("order_id", "PK"), ("booking_id", "FK*"), ("staff_id", "FK*"),
        ("order_time", ""), ("status", "CHECK")])),
    ("concession_item", (4, [
        ("item_id", "PK"), ("item_name", "U"), ("category", "CHECK"),
        ("unit_price", "CHECK"), ("stock_qty", "CHECK")])),
    ("order_item", (4, [
        ("order_id", "PK+FK"), ("item_id", "PK+FK"), ("quantity", "CHECK"),
        ("unit_price", "CHECK")])),
    ("cancellation_policy", (4, [
        ("policy_id", "PK"), ("min_hours_before", "U"), ("refund_percent", "CHECK")])),
])

# ---------------------------------------------------------- relationships ----
# (from, to, cardinality at `from`, cardinality at `to`, verb)
# cardinality: '1' = exactly one, '0..1' = optional one, 'N' = many, '0..N'
RELATIONSHIPS = [
    ("cinema", "screen", "1", "N", "runs screens"),
    ("screen", "seat", "1", "N", "contains seats"),
    ("screen", "screening", "1", "N", "hosts shows"),
    ("movie", "screening", "1", "N", "is screened as"),
    ("customer", "booking", "1", "N", "makes bookings"),
    ("screening", "booking", "1", "N", "receives bookings"),
    ("staff", "booking", "0..1", "N", "sells at the counter"),
    ("booking", "booked_seat", "1", "N", "holds seats"),
    ("seat", "booked_seat", "1", "N", "is held by"),
    ("booked_seat", "ticket", "1", "1", "entitles to one ticket"),
    ("booking", "cancellation", "1", "0..1", "may be cancelled by"),
    ("cancellation", "refund", "1", "N", "releases refunds"),
    ("payment", "refund", "1", "N", "is reversed by"),
    ("booking", "payment", "1", "N", "is paid by"),
    ("concession_order", "payment", "1", "N", "is paid by"),
    ("booking", "concession_order", "1", "N", "may add food"),
    ("concession_order", "order_item", "1", "N", "contains lines"),
    ("concession_item", "order_item", "1", "N", "is ordered as"),
    ("staff", "concession_order", "1", "N", "takes counter orders"),
    ("cancellation_policy", "cancellation", "1", "N", "prices via fn_refund_percent"),
]

PALETTE = {
    "bg": "#050A14", "panel": "#0B1B30", "panel2": "#09162A", "line": "#1C4770",
    "cyan": "#6FD3F2", "pale": "#A5E8FA", "blue": "#3D8BFF", "text": "#E4EFF8",
    "muted": "#9FB6CA", "good": "#7EE0B0", "warn": "#FF9A8A",
}
FONT = "Oswald, 'Segoe UI', 'DejaVu Sans', system-ui, sans-serif"
MONO = "'JetBrains Mono', 'Consolas', monospace"


# ------------------------------------------------------------------ model ----
class Box:
    def __init__(self, name, column, rows, x, y):
        self.name, self.column, self.rows = name, column, rows
        self.x, self.y = x, y
        self.w = BOX_W
        self.h = TITLE_H + len(rows) * ROW_H + PAD_BOTTOM

    @property
    def left(self):
        return self.x

    @property
    def right(self):
        return self.x + self.w

    @property
    def top(self):
        return self.y

    @property
    def bottom(self):
        return self.y + self.h

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2

    def contains(self, px, py, shrink=0.0):
        return (self.left + shrink < px < self.right - shrink
                and self.top + shrink < py < self.bottom - shrink)


def build() -> dict:
    """Stack the entities of each column without gaps and without overlaps."""
    boxes = {}
    for name, (column, rows) in ENTITIES.items():
        y = COLUMN_Y
        for other in boxes.values():
            if other.column == column:
                y = max(y, other.bottom + BOX_GAP)
        boxes[name] = Box(name, column, rows, COL_X[column], y)
    return boxes


# -------------------------------------------------------------- utilities ----
def segment_rect_hit(x1, y1, x2, y2, box: Box, shrink=3.0) -> bool:
    """Liang-Barsky: does the segment enter the (shrunk) rectangle?"""
    left, right = box.left + shrink, box.right - shrink
    top, bottom = box.top + shrink, box.bottom - shrink
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - left), (dx, right - x1), (-dy, y1 - top), (dy, bottom - y1)):
        if p == 0:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    return t0 <= t1


def path_hits(points, boxes, ignore=()) -> list:
    hits = []
    for index in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[index], points[index + 1]
        for name, box in boxes.items():
            if name in ignore:
                continue
            if segment_rect_hit(x1, y1, x2, y2, box):
                hits.append((name, index))
    return hits


def length(points) -> float:
    total = 0.0
    for index in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[index], points[index + 1]
        total += abs(x2 - x1) + abs(y2 - y1)
    return total


def channel_y(boxes, slots: dict, side: str):
    """A free horizontal corridor above (or below) every box, staggered so two
    relationships never share the same lane. Returns None when it is full."""
    top = min(box.top for box in boxes.values())
    bottom = max(box.bottom for box in boxes.values())
    if side == "top":
        slots["top"] = slots.get("top", 0) + 1
        y = top - 44 - 13 * (slots["top"] - 1)
        return y if y > 30 else None
    slots["bottom"] = slots.get("bottom", 0) + 1
    return bottom + 36 + 13 * (slots["bottom"] - 1)


def candidate_paths(a: Box, b: Box, boxes, slots):
    """Every routing we are willing to try; the shortest clean one wins."""
    paths = []
    if a.column == b.column:                       # stacked in one column
        upper, lower = (a, b) if a.cy <= b.cy else (b, a)
        mid = (upper.bottom + lower.top) / 2
        paths.append([(upper.cx, upper.bottom), (upper.cx, mid),
                      (lower.cx, mid), (lower.cx, lower.top)])
    if a.column < b.column:
        paths.append([(a.right, a.cy), (b.left, b.cy)])                      # straight
        corridor = (a.right + b.left) / 2
        paths.append([(a.right, a.cy), (corridor, a.cy), (corridor, b.cy),  # Z in the gap
                      (b.left, b.cy)])
        paths.append([(a.right, a.cy), (a.right + 18, a.cy), (a.right + 18, b.cy),
                      (b.left, b.cy)])
    elif a.column > b.column:
        paths.append([(a.left, a.cy), (b.right, b.cy)])
        corridor = (b.right + a.left) / 2
        paths.append([(a.left, a.cy), (corridor, a.cy), (corridor, b.cy), (b.right, b.cy)])
        paths.append([(a.left, a.cy), (a.left - 18, a.cy), (a.left - 18, b.cy),
                      (b.right, b.cy)])

    for side in ("top", "bottom"):                 # detour around whole columns
        y = channel_y(boxes, slots, side)
        if y is None:
            continue
        if a.column < b.column:
            start_x, end_x = a.right, b.left
            out_a, out_b = a.right + 24, b.left - 24
        else:
            start_x, end_x = a.left, b.right
            out_a, out_b = a.left - 24, b.right + 24
        paths.append([(start_x, a.cy), (out_a, a.cy), (out_a, y), (out_b, y),
                      (out_b, b.cy), (end_x, b.cy)])
    paths.sort(key=length)
    return paths


def crow_foot(points, cardinality, at_start: bool) -> str:
    """Single bar ('1'), small circle ('0..1'), three toes ('N' / '0..N')."""
    if at_start:
        (px, py), (nx, ny) = points[0], points[1]
    else:
        (px, py), (nx, ny) = points[-1], points[-2]
    dx, dy = nx - px, ny - py
    size = max((dx * dx + dy * dy) ** 0.5, 1e-6)
    dx, dy = dx / size, dy / size
    ux, uy = -dy, dx
    parts = []
    if cardinality in ("N", "0..N"):
        base_x, base_y = px + dx * 15, py + dy * 15
        parts.append(f'<path d="M {base_x:.1f} {base_y:.1f} L {px:.1f} {py:.1f} '
                     f'M {base_x:.1f} {base_y:.1f} L {px + ux * 7:.1f} {py + uy * 7:.1f} '
                     f'M {base_x:.1f} {base_y:.1f} L {px - ux * 7:.1f} {py - uy * 7:.1f}"/>')
    if cardinality in ("0..1", "0..N"):
        parts.append(f'<circle cx="{px + dx * 21:.1f}" cy="{py + dy * 21:.1f}" r="5"/>')
    if cardinality in ("1", "0..1"):
        parts.append(f'<path d="M {px + dx * 9 + ux * 8:.1f} {py + dy * 9 + uy * 8:.1f} '
                     f'L {px + dx * 9 - ux * 8:.1f} {py + dy * 9 - uy * 8:.1f}"/>')
    return "".join(parts)


def label_box(text, x, y, size=12.0):
    width = len(text) * size * 0.58 + 10
    return (x - width / 2, y - 10, x + width / 2, y + 8)


def boxes_overlap(one, two) -> bool:
    return not (one[2] <= two[0] or two[2] <= one[0] or one[3] <= two[1] or two[3] <= one[1])


def rect_box_overlap(rect, box: Box) -> bool:
    return boxes_overlap(rect, (box.left, box.top, box.right, box.bottom))


def label_candidates(points):
    """Possible label spots: along the longest segment first, then the others."""
    segments = []
    for index in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[index], points[index + 1]
        segments.append((abs(x2 - x1) + abs(y2 - y1), (x1, y1), (x2, y2)))
    primary = max(segments, key=lambda item: item[0])
    ordered = [primary] + [segment for segment in segments if segment is not primary]
    candidates = []
    for importance, (_length, (x1, y1), (x2, y2)) in enumerate(ordered):
        dx, dy = x2 - x1, y2 - y1
        if abs(dx) >= abs(dy):
            offsets = [(0, -9), (0, 14), (0, -20), (0, 25), (0, -31), (0, 36)]
        else:
            offsets = [(20, 0), (-20, 0), (32, 0), (-32, 0), (44, 0), (-44, 0)]
        for t in (0.5, 0.35, 0.65, 0.22, 0.78):
            for ox, oy in offsets:
                x, y = x1 + dx * t + ox, y1 + dy * t + oy
                weight = importance * 40 + abs(ox) + abs(oy) + abs(t - 0.5) * 60
                candidates.append((weight, x, y))
    candidates.sort(key=lambda item: item[0])
    return [(x, y) for _weight, x, y in candidates]


def place_labels(routes, boxes):
    """Give every relationship verb a free spot next to its own line."""
    placed, problems = [], []
    # long/diagonal connection lines carry their label first, they have less room
    for route in sorted(routes, key=lambda item: length(item["points"]), reverse=True):
        text = route["verb"]
        chosen = None
        for x, y in label_candidates(route["points"]):
            rect = label_box(text, x, y)
            if any(rect_box_overlap(rect, box) for box in boxes.values()):
                continue
            if any(boxes_overlap(rect, other) for other in placed):
                continue
            chosen = (x, y, rect)
            break
        if chosen is None:
            problems.append(text)
            x, y = route["points"][0]
            chosen = (x + 30, y - 12, label_box(text, x + 30, y - 12))
        placed.append(chosen[2])
        route["label"] = (chosen[0], chosen[1])
    return problems


# ------------------------------------------------------------------- draw ----
def marker_spans(marker: str) -> str:
    tokens = []
    for part in marker.split("+") if marker else []:
        if part == "PK":
            tokens.append('<tspan fill="#7EE0B0" font-weight="700">PK </tspan>')
        elif part == "FK":
            tokens.append('<tspan fill="#6FD3F2" font-weight="700">FK </tspan>')
        elif part == "FK*":
            tokens.append('<tspan fill="#6FD3F2" font-weight="700">FK </tspan>'
                          '<tspan fill="#9FB6CA" font-size="10">null </tspan>')
        elif part == "U":
            tokens.append('<tspan fill="#FF9A8A" font-weight="700">U </tspan>')
        elif part == "CHECK":
            tokens.append('<tspan fill="#9FB6CA" font-size="10">chk </tspan>')
    return "".join(tokens)


def render(boxes, routes, width, height) -> str:
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
           f'width="{width:.0f}" height="{height:.0f}" font-family="{FONT}">']
    svg.append(f'<rect width="100%" height="100%" fill="{PALETTE["bg"]}"/>')
    svg.append('<defs><radialGradient id="glow" cx="92%" cy="0%" r="70%">'
               f'<stop offset="0%" stop-color="{PALETTE["line"]}" stop-opacity="0.55"/>'
               '<stop offset="100%" stop-color="#050A14" stop-opacity="0"/></radialGradient>'
               '<filter id="soft" x="-30%" y="-30%" width="160%" height="160%">'
               '<feGaussianBlur stdDeviation="3" result="b"/><feMerge>'
               '<feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>')
    svg.append(f'<rect width="100%" height="100%" fill="url(#glow)"/>')

    svg.append(f'<text x="40" y="52" fill="{PALETTE["pale"]}" font-size="30" '
               f'letter-spacing="3" font-weight="600">GALAXY CINEMAS - ENTITY RELATIONSHIP '
               'DIAGRAM</text>')
    svg.append(f'<text x="42" y="78" fill="{PALETTE["blue"]}" font-size="13" '
               'letter-spacing="3.4">PROJECT 65 | 17 ENTITIES | 20 RELATIONSHIPS | '
               'SUPABASE POSTGRESQL | 3NF WITH TWO DOCUMENTED EXCEPTIONS</text>')

    # relationship paths first, so boxes are drawn on top of the lines
    for route in routes:
        points = route["points"]
        colour = PALETTE["warn"] if route["policy"] else PALETTE["line"]
        d = " ".join(f'{"M" if i == 0 else "L"} {x:.1f} {y:.1f}'
                     for i, (x, y) in enumerate(points))
        dash = ' stroke-dasharray="7 5"' if route["policy"] else ""
        svg.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="2"'
                   f'{dash} opacity="0.95"/>')
        svg.append(f'<g stroke="{PALETTE["blue"]}" stroke-width="2" fill="none">'
                   + crow_foot(points, route["from_card"], True)
                   + crow_foot(points, route["to_card"], False) + '</g>')
        lx, ly = route["label"]
        rect = label_box(route["verb"], lx, ly)
        svg.append(f'<rect x="{rect[0]:.1f}" y="{rect[1]:.1f}" width="{rect[2] - rect[0]:.1f}" '
                   f'height="{rect[3] - rect[1]:.1f}" rx="6" fill="{PALETTE["bg"]}" '
                   'opacity="0.92"/>')
        svg.append(f'<text x="{lx:.1f}" y="{ly:.1f}" fill="{PALETTE["muted"]}" font-size="12" '
                   f'text-anchor="middle">{xml_escape(route["verb"])}</text>')

    for name, box in boxes.items():
        svg.append(f'<g><rect x="{box.left:.0f}" y="{box.top:.0f}" width="{box.w}" '
                   f'height="{box.h:.0f}" rx="12" fill="{PALETTE["panel"]}" '
                   f'stroke="{PALETTE["line"]}" stroke-width="1.4"/>'
                   f'<path d="M {box.left + 12:.0f} {box.top:.0f} '
                   f'H {box.right - 12:.0f} A 12 12 0 0 1 {box.right:.0f} {box.top + 12:.0f} '
                   f'V {box.top + TITLE_H:.0f} H {box.left:.0f} V {box.top + 12:.0f} '
                   f'A 12 12 0 0 1 {box.left + 12:.0f} {box.top:.0f}" '
                   f'fill="{PALETTE["panel2"]}"/>'
                   f'<text x="{box.left + 14:.0f}" y="{box.top + 21:.0f}" '
                   f'fill="{PALETTE["pale"]}" font-size="15" letter-spacing="1.6">'
                   f'{name.upper()}</text></g>')
        for index, (attribute, marker) in enumerate(box.rows):
            y = box.top + TITLE_H + 14 + index * ROW_H
            svg.append(f'<text x="{box.left + 12:.0f}" y="{y:.0f}" font-size="10.5" '
                       f'font-family="{MONO}">{marker_spans(marker)}</text>')
            fill = PALETTE["text"] if marker else PALETTE["muted"]
            svg.append(f'<text x="{box.left + 52:.0f}" y="{y:.0f}" fill="{fill}" '
                       f'font-size="12.5">{xml_escape(attribute)}</text>')

    # legend
    legend_y = height - 66
    svg.append(f'<rect x="40" y="{legend_y - 24:.0f}" width="{width - 80:.0f}" height="62" '
               f'rx="12" fill="{PALETTE["panel"]}" stroke="{PALETTE["line"]}"/>')
    entries = [
        ("#7EE0B0", "PK", "primary key"),
        ("#6FD3F2", "FK", "foreign key (FK* = nullable)"),
        ("#FF9A8A", "U", "unique constraint"),
        ("#9FB6CA", "chk", "CHECK / allowed values"),
        ("#3D8BFF", "|--<", "exactly one  to  many"),
        ("#3D8BFF", "o--<", "optional one  to  many"),
        ("#FF9A8A", "- - -", "policy link (no foreign key)"),
    ]
    x = 62
    for colour, glyph, text in entries:
        svg.append(f'<text x="{x}" y="{legend_y + 2:.0f}" fill="{colour}" font-size="13" '
                   f'font-weight="700" font-family="{MONO}">{xml_escape(glyph)}</text>')
        svg.append(f'<text x="{x + 42}" y="{legend_y + 2:.0f}" fill="{PALETTE["muted"]}" '
                   f'font-size="12">{xml_escape(text)}</text>')
        x += 62 + len(text) * 6.6 + 46
    desc = ('Denormalisation kept on purpose: screening.end_time (needed by the EXCLUDE '
            'constraint ex_screening_no_overlap) and booked_seat.screening_id (needed by the '
            'partial unique index ux_active_seat).')
    svg.append(f'<text x="42" y="{legend_y + 30:.0f}" fill="{PALETTE["muted"]}" font-size="11.5">'
               f'{xml_escape(desc)}</text>')
    svg.append('</svg>')
    return "\n".join(svg)


# ------------------------------------------------------------------ check ----
def build_routes(boxes):
    slots = {}
    routes, crossings = [], []
    for source, target, from_card, to_card, verb in RELATIONSHIPS:
        a, b = boxes[source], boxes[target]
        best, best_hits = None, None
        for path in candidate_paths(a, b, boxes, slots):
            hits = path_hits(path, boxes, ignore=(source, target))
            if not hits:
                best, best_hits = path, hits
                break
            if best_hits is None or len(hits) < len(best_hits) or (
                    len(hits) == len(best_hits) and length(path) < length(best)):
                best, best_hits = path, hits
        routes.append({"points": best, "verb": verb, "from_card": from_card,
                       "to_card": to_card, "names": (source, target),
                       "policy": "cancellation_policy" in (source, target)})
        if best_hits:
            crossings.append((f"{source} -> {target}", best_hits))
    return routes, crossings


def main() -> int:
    boxes = build()
    width = COL_X[-1] + BOX_W + 40
    height = max(box.bottom for box in boxes.values()) + 170
    routes, crossings = build_routes(boxes)
    label_problems = place_labels(routes, boxes)

    # final verification pass on the geometry that is actually written out
    line_problems = []
    for route in routes:
        hits = path_hits(route["points"], boxes, ignore=route["names"])
        if hits:
            line_problems.append((route["verb"], hits))

    svg = render(boxes, routes, width, height)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(svg, encoding="utf-8")

    print("=" * 74)
    print("  ER diagram written by hand ->", OUT.relative_to(ROOT))
    print("=" * 74)
    print(f"  entities        : {len(boxes)}")
    print(f"  relationships   : {len(routes)}")
    print(f"  attributes      : {sum(len(box.rows) for box in boxes.values())}")
    print(f"  canvas          : {width:.0f} x {height:.0f}")
    print(f"  labels placed   : {len(routes) - len(label_problems)}/{len(routes)} without overlap")
    problems = 0
    for verb, hits in line_problems:
        for name, _segment in hits:
            print(f"  LINE CROSSES BOX: '{verb}' passes through {name}")
            problems += 1
    for verb in label_problems:
        print(f"  LABEL OVERLAP   : '{verb}' could not be placed in a free spot")
    if problems or label_problems:
        print(f"\n  RESULT: {problems} line problem(s), {len(label_problems)} label problem(s).")
        return 1
    print("  checks          : no line crosses a box, no label overlaps anything")
    print("  RESULT: clean diagram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
