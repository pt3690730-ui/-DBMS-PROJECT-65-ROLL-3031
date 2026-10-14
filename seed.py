"""Galaxy Cinemas - deterministic seed data.

random.seed(65) and all dates relative to "today" (or to the fixed clock
CINEMA_NOW), so the data set is identical on every machine and every run.

What it creates
    2 cinemas in Hyderabad, 3 screens each (2D / 3D / IMAX / 4DX)
    60 seats per screen   (rows A-F x 10: A-B STANDARD, C-E PREMIUM, F RECLINER)
    8 movies              (English / Hindi / Telugu / Tamil, 96-165 min)
    26 customers          (customer_id = 1 is the shared "Walk-in Guest")
    3 staff, 10 concession items in INR, 3 refund policies (24h/2h/0h)
    4 shows per day per screen at 10:00, 13:45, 17:30, 21:15, from 21 days ago
        to 7 days ahead, ~12% skipped (skipping can never create an overlap)
    bookings of 1-4 seats with one ticket per seat (TKT-000123) and a payment
    ~9% of bookings cancelled, refunded at the correct policy tier
    ~45% of bookings with a food order (priced by trigger T5) and a payment
    stock is seeded large, then reset to realistic values at the very end

Everything is inserted with psycopg2 execute_values() batch inserts, so the
network round trips stay low even against a hosted Supabase database.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, time, timedelta

from psycopg2.extras import execute_values

import db

SEED = 65
BATCH = 500
R = random.Random(SEED)

# ------------------------------------------------------------------ shapes ---
SLOTS = [(10, 0), (13, 45), (17, 30), (21, 15)]
DAYS_BACK, DAYS_AHEAD = 21, 7
CLEANING_GAP_MIN = 15
SEATS_PER_ROW = 10
ROWS = [("A", "STANDARD"), ("B", "STANDARD"), ("C", "PREMIUM"),
        ("D", "PREMIUM"), ("E", "PREMIUM"), ("F", "RECLINER")]
SEATS_PER_SCREEN = len(ROWS) * SEATS_PER_ROW
PRICE_BY_TYPE = {"2D": 150.0, "3D": 210.0, "IMAX": 350.0, "4DX": 290.0}

CINEMAS = [
    (1, "Galaxy Cinemas Hitech City", "Hyderabad",
     "Level 4, Inorbit Mall, Madhapur, Hitech City, Hyderabad", "04045678901"),
    (2, "Galaxy Cinemas Kukatpally", "Hyderabad",
     "3rd Floor, Forum Sujana Mall, Kukatpally, Hyderabad", "04045678902"),
]

SCREENS = [  # (screen_id, cinema_id, screen_name, screen_type)
    (1, 1, "Screen 1", "IMAX"), (2, 1, "Screen 2", "3D"), (3, 1, "Screen 3", "2D"),
    (4, 2, "Screen 1", "4DX"), (5, 2, "Screen 2", "2D"), (6, 2, "Screen 3", "3D"),
]

MOVIES = [  # (movie_id, title, genre, language, duration_min, certificate, days_ago)
    (1, "Interstellar Rewind", "Sci-Fi", "English", 165, "UA", 140),
    (2, "Monsoon Express", "Drama", "Hindi", 148, "UA", 96),
    (3, "Raju Garu Rockz", "Comedy", "Telugu", 132, "U", 40),
    (4, "Chennai Nights", "Thriller", "Tamil", 121, "A", 60),
    (5, "The Last Algorithm", "Sci-Fi", "English", 138, "UA", 25),
    (6, "Biryani Brothers", "Comedy", "Telugu", 118, "U", 18),
    (7, "Silent Verdict", "Crime", "Hindi", 152, "A", 70),
    (8, "Little Rocket Club", "Animation", "English", 96, "U", 12),
]

CUSTOMER_NAMES = [
    ("Walk-in Guest", "walkin.guest@galaxycinemas.in"),
    ("Aarav Sharma", "aarav.sharma@example.com"),
    ("Diya Reddy", "diya.reddy@example.com"),
    ("Vihaan Gupta", "vihaan.gupta@example.com"),
    ("Ananya Iyer", "ananya.iyer@example.com"),
    ("Arjun Nair", "arjun.nair@example.com"),
    ("Saanvi Rao", "saanvi.rao@example.com"),
    ("Kabir Malhotra", "kabir.malhotra@example.com"),
    ("Ishita Bose", "ishita.bose@example.com"),
    ("Reyansh Verma", "reyansh.verma@example.com"),
    ("Myra Chowdary", "myra.chowdary@example.com"),
    ("Aditya Menon", "aditya.menon@example.com"),
    ("Kiara Pillai", "kiara.pillai@example.com"),
    ("Rudra Singh", "rudra.singh@example.com"),
    ("Aadhya Krishnan", "aadhya.krishnan@example.com"),
    ("Vivaan Joshi", "vivaan.joshi@example.com"),
    ("Navya Deshmukh", "navya.deshmukh@example.com"),
    ("Ishaan Patel", "ishaan.patel@example.com"),
    ("Sara Fernandes", "sara.fernandes@example.com"),
    ("Dhruv Kulkarni", "dhruv.kulkarni@example.com"),
    ("Riya Chatterjee", "riya.chatterjee@example.com"),
    ("Advait Bhatt", "advait.bhatt@example.com"),
    ("Meera Krishnamurthy", "meera.krishnamurthy@example.com"),
    ("Nikhil Raju", "nikhil.raju@example.com"),
    ("Tanvi Sekhar", "tanvi.sekhar@example.com"),
    ("Harsh Vardhan", "harsh.vardhan@example.com"),
]

STAFF = [  # (staff_id, cinema_id, full_name, username, role)
    (1, 1, "Ananya Rao", "ananya.admin", "ADMIN"),
    (2, 1, "Vikram Shetty", "vikram.boxoffice", "BOX_OFFICE"),
    (3, 2, "Sneha Kulkarni", "sneha.boxoffice", "BOX_OFFICE"),
]

ITEMS = [  # (item_id, item_name, category, unit_price, realistic_stock)
    (1, "Classic Salted Popcorn (Regular)", "POPCORN", 220.00, 120),
    (2, "Classic Salted Popcorn (Large)", "POPCORN", 340.00, 64),
    (3, "Caramel Popcorn (Large)", "POPCORN", 390.00, 8),
    (4, "Pepsi 500ml", "BEVERAGE", 180.00, 240),
    (5, "Coke Zero 500ml", "BEVERAGE", 180.00, 180),
    (6, "Cold Coffee 300ml", "BEVERAGE", 160.00, 90),
    (7, "Nachos with Cheese", "SNACK", 230.00, 45),
    (8, "Punjabi Samosa (2 pc)", "SNACK", 120.00, 150),
    (9, "Peri-Peri French Fries", "SNACK", 190.00, 60),
    (10, "Movie Combo (Large Popcorn + 2 Pepsi)", "COMBO", 560.00, 30),
]
STOCK_LARGE = 6000

POLICIES = [(1, 24, 100), (2, 2, 50), (3, 0, 0)]

REASONS = [
    "Plan changed", "Travelling that day", "Booked the wrong show",
    "Family emergency", "Feeling unwell", "Duplicate booking",
    "Chose a different cinema", "Rescheduled the outing",
]
CANCEL_TIER_WEIGHTS = [(0.50, (24.5, 96.0)), (0.35, (2.5, 23.5)), (0.15, (0.2, 1.9))]
METHOD_WEIGHTS = [("CARD", 0.40), ("UPI", 0.35), ("WALLET", 0.15), ("CASH", 0.10)]


# ------------------------------------------------------------------ helpers --
def clock() -> datetime:
    """'Today' for the seed: CINEMA_NOW when set, otherwise the wall clock."""
    if db.CINEMA_NOW:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(db.CINEMA_NOW, fmt)
            except ValueError:
                continue
    return datetime.now().replace(second=0, microsecond=0)


def password_hash(username: str) -> str:
    """Deterministic demo hash - never a real password, never printed."""
    salt = hashlib.sha256(f"galaxy-cinemas::{username}".encode()).hexdigest()[:16]
    digest = hashlib.pbkdf2_hmac("sha256", b"demo-only", salt.encode(), 120_000)
    return f"pbkdf2_sha256$120000${salt}${digest.hex()}"


def pick_method() -> str:
    roll = R.random()
    acc = 0.0
    for method, weight in METHOD_WEIGHTS:
        acc += weight
        if roll <= acc:
            return method
    return "CARD"


def batch(cur, prefix: str, rows: list) -> None:
    if rows:
        execute_values(cur, prefix, rows, page_size=BATCH)


def set_clock(cur, moment: datetime) -> None:
    """Pin fn_now() for the rest of this transaction (trigger validation)."""
    cur.execute("SELECT set_config('cinema.now', %s, true)",
                (moment.strftime("%Y-%m-%d %H:%M:%S"),))


# --------------------------------------------------------------------- load --
def load(verbose: bool = True) -> dict:
    today = clock().date()
    base = today - timedelta(days=DAYS_BACK)

    # ---- 1. reference data ------------------------------------------------
    with db.transaction(source="seed:reference") as cur:
        batch(cur, "INSERT INTO cinema (cinema_id, cinema_name, city, address, phone) VALUES %s", CINEMAS)
        batch(cur, "INSERT INTO screen (screen_id, cinema_id, screen_name, screen_type) VALUES %s", SCREENS)
        batch(cur, "INSERT INTO movie (movie_id, title, genre, language, duration_min, certificate, release_date) VALUES %s",
              [(mid, title, genre, lang, dur, cert, today - timedelta(days=ago))
               for mid, title, genre, lang, dur, cert, ago in MOVIES])

        seats, seat_meta = [], {}
        for index, (screen_id, _cinema, _name, _type) in enumerate(SCREENS):
            base_seat = index * SEATS_PER_SCREEN
            for row_index, (row_label, seat_type) in enumerate(ROWS):
                for seat_no in range(1, SEATS_PER_ROW + 1):
                    seat_id = base_seat + row_index * SEATS_PER_ROW + seat_no
                    seats.append((seat_id, screen_id, row_label, seat_no, seat_type))
                    seat_meta[seat_id] = (screen_id, row_label, seat_no, seat_type)
        batch(cur, "INSERT INTO seat (seat_id, screen_id, row_label, seat_no, seat_type) VALUES %s", seats)

        customers = []
        for index, (name, email) in enumerate(CUSTOMER_NAMES, start=1):
            phone = "9000000000" if index == 1 else f"9{R.randint(100000000, 999999999)}"
            created = min(clock(), datetime.combine(base, time(9, 0))) - timedelta(days=R.randint(1, 400))
            customers.append((index, name, email, phone, created))
        batch(cur, "INSERT INTO customer (customer_id, full_name, email, phone, created_at) VALUES %s", customers)

        batch(cur, "INSERT INTO staff (staff_id, cinema_id, full_name, username, password_hash, role) VALUES %s",
              [(sid, cid, name, user, password_hash(user), role)
               for sid, cid, name, user, role in STAFF])

        # stock is deliberately huge here so that the historical orders of the
        # next stage cannot fail; it is reset to realistic values at the end.
        batch(cur, "INSERT INTO concession_item (item_id, item_name, category, unit_price, stock_qty) VALUES %s",
              [(iid, name, cat, price, STOCK_LARGE) for iid, name, cat, price, _stock in ITEMS])
        batch(cur, "INSERT INTO cancellation_policy (policy_id, min_hours_before, refund_percent) VALUES %s", POLICIES)

    # ---- 2. screenings ----------------------------------------------------
    screenings = []  # (id, movie_id, screen_id, start, end, base_price)
    screening_id = 0
    for day_offset in range(0, DAYS_BACK + DAYS_AHEAD + 1):
        show_date = base + timedelta(days=day_offset)
        for screen_id, _cinema_id, _name, screen_type in SCREENS:
            for hour, minute in SLOTS:
                if R.random() < 0.12:          # ~12% of slots stay empty
                    continue
                screening_id += 1
                movie = R.choice(MOVIES)
                start = datetime.combine(show_date, time(hour, minute))
                end = start + timedelta(minutes=movie[4] + CLEANING_GAP_MIN)
                price = PRICE_BY_TYPE[screen_type] + R.choice([0, 20, 30, 40, 50])
                if hour >= 17:
                    price += 40                # evening prime-time surcharge
                screenings.append((screening_id, movie[0], screen_id, start, end, float(price)))

    with db.transaction(source="seed:screening") as cur:
        set_clock(cur, datetime.combine(base, time(8, 0)))
        batch(cur, "INSERT INTO screening (screening_id, movie_id, screen_id, start_time, end_time, base_price, status) VALUES %s",
              [(sid, mid, scid, start, end, price, "SCHEDULED")
               for sid, mid, scid, start, end, price in screenings])

    # ---- 3. bookings + seats + tickets + payments --------------------------
    now = clock()
    item_price = {iid: price for iid, _n, _c, price, _s in ITEMS}
    bookings, booked_seats, tickets, payments = [], [], [], []
    booking_meta = {}          # booking_id -> dict(screening_id, start, customer_id, paid, booked_at)
    booking_id = booked_seat_id = ticket_id = payment_id = 0

    for screening_id, movie_id, screen_id, start, end, price in screenings:
        if start < now:                                   # a show that already played
            occupancy = R.uniform(0.15, 0.75)
        else:                                             # 45% sold nothing yet
            roll = R.random()
            occupancy = 0.0 if roll < 0.45 else (
                R.uniform(0.02, 0.12) if roll < 0.80 else R.uniform(0.12, 0.35))
        wanted = int(round(occupancy * SEATS_PER_SCREEN))
        if wanted <= 0:
            continue
        screen_index = next(i for i, s in enumerate(SCREENS) if s[0] == screen_id)
        first_seat = screen_index * SEATS_PER_SCREEN + 1
        chosen = R.sample(range(first_seat, first_seat + SEATS_PER_SCREEN), wanted)
        R.shuffle(chosen)

        cursor = 0
        while cursor < len(chosen):
            size = min(R.randint(1, 4), len(chosen) - cursor)
            group = chosen[cursor:cursor + size]
            cursor += size

            booking_id += 1
            walk_in = R.random() < 0.08
            customer_id = 1 if walk_in else R.randint(2, len(CUSTOMER_NAMES))
            hours_before = R.uniform(0.25, 2.0) if walk_in else R.uniform(1.0, 72.0)
            booked_at = start - timedelta(hours=hours_before)
            if booked_at > now:                            # booked in advance
                booked_at = now - timedelta(hours=R.uniform(1.0, 96.0))
            if booked_at >= start:
                booked_at = start - timedelta(minutes=R.randint(20, 240))

            staff_id = R.choice([2, 3]) if walk_in else (
                R.choice([2, 3]) if R.random() < 0.06 else None)
            bookings.append((booking_id, customer_id, screening_id, staff_id,
                             booked_at, "CONFIRMED"))

            paid = 0.0
            for seat_id in group:
                booked_seat_id += 1
                booked_seats.append((booked_seat_id, booking_id, screening_id, seat_id, "BOOKED"))
                seat_type = seat_meta[seat_id][3]
                price_for_seat = round(price * db.SEAT_MULTIPLIER[seat_type], 2)
                paid += price_for_seat
                ticket_id += 1
                tickets.append((ticket_id, booked_seat_id, f"TKT-{booked_seat_id:06d}",
                                price_for_seat, booked_at, "ISSUED"))

            payment_id += 1
            paid = round(paid, 2)
            payments.append((payment_id, booking_id, None, paid, pick_method(),
                             "SUCCESS", booked_at,
                             f"TXN-{booked_at:%Y%m%d}-{payment_id:06d}"))
            booking_meta[booking_id] = {"screening_id": screening_id, "start": start,
                                       "customer_id": customer_id, "paid": paid,
                                       "booked_at": booked_at}

    with db.transaction(source="seed:booking") as cur:
        set_clock(cur, datetime.combine(base, time(8, 0)))
        batch(cur, "INSERT INTO booking (booking_id, customer_id, screening_id, staff_id, booking_time, status) VALUES %s", bookings)
        batch(cur, "INSERT INTO booked_seat (booked_seat_id, booking_id, screening_id, seat_id, status) VALUES %s", booked_seats)
        batch(cur, "INSERT INTO ticket (ticket_id, booked_seat_id, ticket_code, price, issued_at, status) VALUES %s", tickets)
        batch(cur, "INSERT INTO payment (payment_id, booking_id, order_id, amount, method, status, paid_at, transaction_ref) VALUES %s", payments)

    # ---- 4. cancellations + refunds (~9%) ---------------------------------
    cancel_count = int(len(booking_meta) * 0.09)
    cancelled = sorted(R.sample(sorted(booking_meta), cancel_count))
    cancel_bookings, cancel_times, cancel_by, cancel_reasons = [], [], [], []
    for booking_id in cancelled:
        start = booking_meta[booking_id]["start"]
        roll = R.random()
        for weight, (low, high) in CANCEL_TIER_WEIGHTS:
            if roll <= weight:
                hours_before = R.uniform(low, high)
                break
            roll -= weight
        else:
            hours_before = R.uniform(0.2, 1.9)
        when = start - timedelta(hours=hours_before)
        if when > now:      # a cancellation always happened in the past
            when = now - timedelta(minutes=R.randint(30, 6000))
        actor = "CUSTOMER" if R.random() < 0.8 else ("STAFF" if R.random() < 0.75 else "SYSTEM")
        cancel_bookings.append(booking_id)
        cancel_times.append(when)
        cancel_by.append(actor)
        cancel_reasons.append(R.choice(REASONS))

    with db.transaction(source="seed:cancellation") as cur:
        # Pin the clock to the earliest cancellation: the trigger check "the
        # show has not started yet" then holds for every cancellation at once.
        earliest = min(cancel_times) if cancel_times else clock()
        set_clock(cur, earliest)

        ids = []
        if cancel_bookings:
            cur.execute(
                "INSERT INTO cancellation (booking_id, cancelled_at, cancelled_by, reason) "
                "SELECT * FROM unnest(%s::int[], %s::timestamp[], %s::text[], %s::text[]) "
                "RETURNING cancellation_id",
                (cancel_bookings, cancel_times, cancel_by, cancel_reasons))
            ids = [row["cancellation_id"] for row in cur.fetchall()]

        refunded = 0
        if ids:
            cur.execute(
                """
                INSERT INTO refund (cancellation_id, payment_id, refund_amount,
                                    refund_status, refunded_at)
                SELECT c.cancellation_id, p.payment_id,
                       round(p.amount * fn_refund_percent(c.booking_id, c.cancelled_at) / 100.0, 2),
                       'COMPLETED', LEAST(c.cancelled_at + interval '2 hours', now())
                  FROM cancellation c
                  JOIN payment p ON p.booking_id = c.booking_id AND p.status <> 'FAILED'
                 WHERE c.cancellation_id = ANY(%s)
                   AND round(p.amount * fn_refund_percent(c.booking_id, c.cancelled_at) / 100.0, 2) > 0
                RETURNING refund_id
                """, (ids,))
            refunded = len(cur.fetchall())
            cur.execute(
                """
                UPDATE payment SET status = 'REFUNDED'
                 WHERE payment_id IN (
                       SELECT r.payment_id FROM refund r
                        WHERE r.cancellation_id = ANY(%s))
                """, (ids,))

    # ---- 5. concession orders (~45%) + counter sales ----------------------
    item_ids = [iid for iid, _n, _c, _p, _s in ITEMS]
    orders, order_items, order_payments = [], [], []
    order_id = 0
    order_payment_id = payment_id

    def add_order(target_booking, when, staff, max_lines, max_qty=3):
        nonlocal order_id, order_payment_id
        order_id += 1
        orders.append((order_id, target_booking, staff, when, "PLACED"))
        total = 0.0
        for item_id in R.sample(item_ids, R.randint(1, max_lines)):
            qty = R.randint(1, max_qty)
            # unit_price is left NULL on purpose: trigger T5 fills it from the
            # item master and decrements stock.
            order_items.append((order_id, item_id, qty, None))
            total += qty * item_price[item_id]
        order_payment_id += 1
        method = "CASH" if staff else pick_method()
        order_payments.append((order_payment_id, None, order_id, round(total, 2), method,
                               "SUCCESS", when, f"TXN-{when:%Y%m%d}-{order_payment_id:06d}"))

    upsell = [bid for bid in sorted(booking_meta)
              if bid not in set(cancelled) and R.random() < 0.45]
    for booking_id in upsell:
        booked_at = booking_meta[booking_id]["booked_at"]
        add_order(booking_id, booked_at + timedelta(minutes=R.randint(0, 45)),
                  R.choice([2, 3]) if R.random() < 0.3 else None, 3)
    for _ in range(40):                                    # walk-up counter sales
        when = datetime.combine(base + timedelta(days=R.randint(0, DAYS_BACK)),
                                time(R.randint(11, 22), R.choice([0, 10, 15, 30, 45])))
        add_order(None, when, R.choice([2, 3]), 4, max_qty=2)

    with db.transaction(source="seed:order") as cur:
        set_clock(cur, datetime.combine(base, time(8, 0)))
        batch(cur, "INSERT INTO concession_order (order_id, booking_id, staff_id, order_time, status) VALUES %s", orders)
        batch(cur, "INSERT INTO order_item (order_id, item_id, quantity, unit_price) VALUES %s", order_items)
        batch(cur, "INSERT INTO payment (payment_id, booking_id, order_id, amount, method, status, paid_at, transaction_ref) VALUES %s", order_payments)

        # realistic closing stock (item 3 is deliberately nearly sold out so the
        # "over-stock order rejected" demo is one click away)
        for item_id, _name, _cat, _price, stock in ITEMS:
            cur.execute("UPDATE concession_item SET stock_qty = %s WHERE item_id = %s",
                        (stock, item_id))

    # ---- 6. let history look like history ---------------------------------
    with db.transaction(source="seed:archive") as cur:
        cur.execute("UPDATE screening SET status = 'COMPLETED' "
                    "WHERE end_time < %s AND status = 'SCHEDULED'", (now,))
        cur.execute(
            """
            UPDATE ticket t SET status = 'USED'
              FROM booked_seat bs
              JOIN screening s ON s.screening_id = bs.screening_id
             WHERE bs.booked_seat_id = t.booked_seat_id
               AND s.end_time < %s AND t.status = 'ISSUED'
            """, (now,))

    # ---- 7. identity sequences -------------------------------------------
    sequences = {
        "cinema": "cinema_id", "screen": "screen_id", "seat": "seat_id",
        "movie": "movie_id", "screening": "screening_id", "customer": "customer_id",
        "staff": "staff_id", "concession_item": "item_id", "cancellation_policy": "policy_id",
        "booking": "booking_id", "booked_seat": "booked_seat_id", "ticket": "ticket_id",
        "concession_order": "order_id", "order_item": None, "payment": "payment_id",
        "cancellation": "cancellation_id", "refund": "refund_id",
    }
    for table, pk in sequences.items():
        if pk:
            db.reset_sequence(table, pk)

    counts = db.table_counts()
    if verbose:
        print_counts(counts)
    return counts


def print_counts(counts: dict | None = None, title: str = "Row counts") -> None:
    counts = counts or db.table_counts()
    total = sum(counts.values())
    print(f"\n{title}")
    print("-" * 44)
    for table in db.TABLES:
        print(f"  {table:<22} {counts.get(table, 0):>9,}")
    print("-" * 44)
    print(f"  {'TOTAL':<22} {total:>9,}")


if __name__ == "__main__":
    print(f"Seeding Galaxy Cinemas (seed={SEED}, today={clock():%d %b %Y}) ...")
    load()
    print("\nDone. The data set is identical on every run (random.seed(65)).")
