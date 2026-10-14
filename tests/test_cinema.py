"""Galaxy Cinemas - automated test suite (plain unittest).

    python tests/test_cinema.py              # human readable
    python tests/test_cinema.py --json       # one JSON line (used by POST /api/tests/run)

Everything runs against a THROW-AWAY TEST SCHEMA that this file creates at the
start and drops at the end, so the demo data in `public` is never touched:

    CINEMA_TEST_SCHEMA (default: cinema_test_<pid>)

The database clock is pinned with CINEMA_NOW, which makes every time dependent
rule (booking closed, cancellation window, refund tiers) deterministic.

The suite checks the positive path AND the negative path of each rule, and it
proves that a rejected statement really rolled back: after a refused double
booking the free seat of the same request must still be free.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------- environment
SCHEMA = os.environ.get("CINEMA_TEST_SCHEMA") or f"cinema_test_{os.getpid()}"
FIXED_NOW = os.environ.get("CINEMA_TEST_NOW") or os.environ.get("CINEMA_NOW") \
    or "2026-06-15 12:00"
os.environ["CINEMA_SCHEMA"] = SCHEMA
os.environ["CINEMA_NOW"] = FIXED_NOW
# DATABASE_URL / DATABASE_SSLMODE keep whatever .env or the caller provided.

import psycopg2                                            # noqa: E402
import psycopg2.extras                                     # noqa: E402
import psycopg2.errors                                     # noqa: E402

import db                                                  # noqa: E402
import app as application                                  # noqa: E402

NOW = datetime.strptime(FIXED_NOW, "%Y-%m-%d %H:%M")
API = application.app.test_client()

SCHEMA_FILE = ROOT / "supabase" / "schema.sql"


def dsn(**extra):
    return db.database_url()


def connect(schema: str | None = None, autocommit: bool = True):
    options = {}
    if schema:
        options["options"] = f"-c search_path={schema},public,extensions"
    conn = psycopg2.connect(dsn(), cursor_factory=psycopg2.extras.RealDictCursor, **options)
    conn.autocommit = autocommit
    return conn


def sql(statement, params=None, schema=None):
    """Run one statement as the test itself (not through the API)."""
    conn = connect(schema)
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def one(statement, params=None):
    rows = sql(statement, params, schema=SCHEMA)
    return rows[0] if rows else None


def value(statement, params=None):
    row = one(statement, params)
    return next(iter(row.values())) if row else None


SEQUENCES = {
    "cinema": "cinema_id", "screen": "screen_id", "seat": "seat_id",
    "movie": "movie_id", "screening": "screening_id", "customer": "customer_id",
    "staff": "staff_id", "concession_item": "item_id",
    "cancellation_policy": "policy_id", "booking": "booking_id",
    "booked_seat": "booked_seat_id", "ticket": "ticket_id",
    "concession_order": "order_id", "payment": "payment_id",
    "cancellation": "cancellation_id", "refund": "refund_id",
}


# ---------------------------------------------------------------- fixtures --
def build_schema() -> None:
    admin = connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
            cur.execute(f'CREATE SCHEMA "{SCHEMA}"')
            cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    finally:
        admin.close()
    conn = connect(SCHEMA)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    finally:
        conn.close()


def seed_fixture() -> None:
    """A small, deterministic data set - completely separate from seed.py."""
    conn = connect(SCHEMA)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO cinema (cinema_id, cinema_name, city, address, phone) "
                    "VALUES (1, 'Test Cinemas', 'Hyderabad', '1 Test Road, Hyderabad', '04012345678')")
        cur.execute("INSERT INTO screen (screen_id, cinema_id, screen_name, screen_type) "
                    "VALUES (1, 1, 'Screen 1', '2D'), (2, 1, 'Screen 2', 'IMAX')")
        seats = []
        for screen_id, base in ((1, 0), (2, 100)):
            for row_index, (row, seat_type) in enumerate(
                    [("A", "STANDARD"), ("B", "STANDARD"), ("C", "PREMIUM"), ("D", "PREMIUM"),
                     ("E", "PREMIUM"), ("F", "RECLINER")]):
                for seat_no in range(1, 11):
                    seats.append((base + row_index * 10 + seat_no, screen_id, row, seat_no, seat_type))
        psycopg2.extras.execute_values(
            cur, "INSERT INTO seat (seat_id, screen_id, row_label, seat_no, seat_type) VALUES %s", seats)
        cur.execute("INSERT INTO movie (movie_id, title, genre, language, duration_min, "
                    "                    certificate, release_date) VALUES "
                    "(1, 'Test Feature', 'Drama', 'English', 120, 'UA', '2026-01-01'), "
                    "(2, 'Test Short', 'Comedy', 'Hindi', 90, 'U', '2026-02-01')")
        cur.execute("INSERT INTO customer (customer_id, full_name, email, phone) VALUES "
                    "(1, 'Walk-in Guest', 'walkin@example.com', '9000000000'), "
                    "(2, 'Asha Rao', 'asha.rao@example.com', '9876543210'), "
                    "(3, 'Bilal Khan', 'bilal.khan@example.com', '9812345678')")
        cur.execute("INSERT INTO staff (staff_id, cinema_id, full_name, username, password_hash, role) "
                    "VALUES (1, 1, 'Admin User', 'admin.test', 'hash', 'ADMIN'), "
                    "(2, 1, 'Counter User', 'counter.test', 'hash', 'BOX_OFFICE')")
        cur.execute("INSERT INTO concession_item (item_id, item_name, category, unit_price, stock_qty) "
                    "VALUES (1, 'Test Popcorn', 'POPCORN', 200.00, 50), "
                    "(2, 'Test Cola', 'BEVERAGE', 100.00, 4), "
                    "(3, 'Test Nachos', 'SNACK', 150.00, 0)")
        cur.execute("INSERT INTO cancellation_policy (policy_id, min_hours_before, refund_percent) "
                    "VALUES (1, 24, 100), (2, 2, 50), (3, 0, 0)")

        # screenings: +1h (0% tier), +6h (50%), +72h (100%), started, cancelled, +50h
        shows = [
            (1, 1, 1, NOW + timedelta(hours=1), "SCHEDULED"),
            (2, 1, 1, NOW + timedelta(hours=6), "SCHEDULED"),
            (3, 1, 1, NOW + timedelta(hours=72), "SCHEDULED"),
            (4, 1, 1, NOW - timedelta(hours=3), "SCHEDULED"),
            (5, 1, 1, NOW + timedelta(hours=48), "CANCELLED"),
            (6, 1, 1, NOW + timedelta(hours=50), "SCHEDULED"),
        ]
        for screening_id, movie_id, screen_id, start, status in shows:
            end = start + timedelta(minutes=(120 if movie_id == 1 else 90) + 15)
            cur.execute("INSERT INTO screening (screening_id, movie_id, screen_id, start_time, "
                        "                        end_time, base_price, status) "
                        "VALUES (%s, %s, %s, %s, %s, 100.00, %s)",
                        (screening_id, movie_id, screen_id, start, end, status))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    # the fixture inserted explicit ids, so the identity sequences must catch up
    # (otherwise the next INSERT would collide with the rows above)
    for table, primary_key in SEQUENCES.items():
        db.reset_sequence(table, primary_key)


def setUpModule():
    """unittest calls this before the first test, whichever runner is used."""
    build_schema()
    seed_fixture()


def tearDownModule():
    """Drop the throw-away schema and close every pooled connection."""
    db.close_pool()
    admin = connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    finally:
        admin.close()


# ------------------------------------------------------------------ helpers -
_next_show = [0]


def fresh_show(hours_from_now: float = 200, screen_id: int = 1, movie_id: int = 1,
               status: str = "SCHEDULED") -> int:
    """A brand-new screening far in the future, so tests never share seats."""
    _next_show[0] += 1
    start = NOW + timedelta(hours=hours_from_now + _next_show[0])
    end = start + timedelta(minutes=120 + 15)
    row = one("INSERT INTO screening (movie_id, screen_id, start_time, end_time, base_price, status) "
              "VALUES (%s, %s, %s, %s, 100.00, %s) RETURNING screening_id",
              (movie_id, screen_id, start, end, status))
    return row["screening_id"]


def seat_id(screen_id: int, row: str, number: int) -> int:
    return value("SELECT fn_seat_id(%s, %s, %s) AS seat", (screen_id, row, number))


def book(seat_ids, screening_id=None, customer_id=2, method="CARD", staff_id=None):
    body = {"customer_id": customer_id, "screening_id": screening_id, "seat_ids": list(seat_ids),
            "method": method}
    if staff_id:
        body["staff_id"] = staff_id
    return API.post("/api/bookings", json=body)


def book_ok(seat_ids, screening_id=None, **kwargs):
    response = book(seat_ids, screening_id, **kwargs)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["booking"]


def seats_free(screening_id: int, seat_ids) -> int:
    return value("SELECT count(*) AS n FROM booked_seat WHERE screening_id = %s "
                 "AND seat_id = ANY(%s) AND status = 'BOOKED'",
                 (screening_id, list(seat_ids)))


# =============================================================================
#  1. schema / catalog
# =============================================================================
class SchemaTests(unittest.TestCase):
    def test_seventeen_tables_exist(self):
        self.assertEqual(len(db.TABLES), 17)
        for table in db.TABLES:
            with self.subTest(table=table):
                self.assertIsNotNone(value(
                    "SELECT to_regclass(%s) AS reg", (f'"{SCHEMA}".{table}',)))

    def test_five_triggers_exist(self):
        rows = sql("SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                   "JOIN pg_namespace n ON n.oid = c.relnamespace "
                   "WHERE n.nspname = %s AND NOT t.tgisinternal", (SCHEMA,))
        self.assertEqual(sorted(row["tgname"] for row in rows), sorted(db.TRIGGERS))

    def test_six_views_exist(self):
        rows = sql("SELECT table_name FROM information_schema.views WHERE table_schema = %s",
                   (SCHEMA,))
        self.assertEqual(sorted(row["table_name"] for row in rows), sorted(db.VIEWS))

    def test_eight_indexes_exist(self):
        rows = sql("SELECT indexname FROM pg_indexes WHERE schemaname = %s", (SCHEMA,))
        names = {row["indexname"] for row in rows}
        self.assertEqual(len(db.INDEXES), 8)
        for index in db.INDEXES:
            self.assertIn(index, names)

    def test_row_level_security_on_all_tables_without_policies(self):
        rows = sql("SELECT relname, relrowsecurity FROM pg_class c JOIN pg_namespace n "
                   "ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'r'", (SCHEMA,))
        self.assertEqual(len(rows), 17)
        self.assertTrue(all(row["relrowsecurity"] for row in rows),
                        "RLS must be enabled on every table")
        self.assertEqual(len(sql("SELECT * FROM pg_policies WHERE schemaname = %s", (SCHEMA,))), 0)

    def test_btree_gist_extension_is_installed(self):
        self.assertIsNotNone(value("SELECT extname FROM pg_extension WHERE extname = 'btree_gist'"))

    def test_sql_functions_exist(self):
        rows = sql("SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                   "WHERE n.nspname = %s", (SCHEMA,))
        names = {row["proname"] for row in rows}
        for expected in ("fn_now", "fn_seat_id", "fn_refund_percent", "fn_schedule_screening"):
            self.assertIn(expected, names)

    def test_all_six_views_are_queryable(self):
        for view in db.VIEWS:
            with self.subTest(view=view):
                self.assertIsNotNone(sql(f"SELECT count(*) AS n FROM {view}", schema=SCHEMA))

    def test_api_schema_endpoint_reports_the_same_objects(self):
        payload = API.get("/api/schema").get_json()
        self.assertEqual(payload["summary"]["tables"], 17)
        self.assertEqual(payload["summary"]["triggers"], 5)
        self.assertEqual(payload["summary"]["views"], 6)
        self.assertEqual(payload["summary"]["indexes"], 8)


# =============================================================================
#  2. scheduling and the EXCLUDE constraint
# =============================================================================
class SchedulingTests(unittest.TestCase):
    def test_schedule_show_sets_end_time_with_cleaning_gap(self):
        start = NOW + timedelta(hours=300)
        response = API.post("/api/screenings", json={
            "movie_id": 1, "screen_id": 1, "start_time": start.strftime("%Y-%m-%d %H:%M"),
            "base_price": 150})
        self.assertEqual(response.status_code, 201, response.get_json())
        show = response.get_json()["screening"]
        expected_end = start + timedelta(minutes=120 + 15)
        self.assertEqual(show["end_time"], expected_end.strftime("%Y-%m-%d %H:%M:%S"))

    def test_schedule_in_the_past_is_rejected(self):
        response = API.post("/api/screenings", json={
            "movie_id": 1, "screen_id": 1,
            "start_time": (NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"), "base_price": 150})
        self.assertEqual(response.status_code, 409)
        self.assertIn("future", response.get_json()["error"])

    def test_overlapping_show_is_rejected_by_the_database(self):
        screening_id = fresh_show(400)
        row = one("SELECT start_time, end_time FROM screening WHERE screening_id = %s",
                  (screening_id,))
        inside = row["start_time"] + timedelta(minutes=30)
        response = API.post("/api/screenings", json={
            "movie_id": 1, "screen_id": 1, "start_time": inside.strftime("%Y-%m-%d %H:%M"),
            "base_price": 150})
        self.assertEqual(response.status_code, 409)
        self.assertIn("overlap", response.get_json()["error"].lower())

    def test_one_minute_before_the_end_still_overlaps(self):
        screening_id = fresh_show(420)
        row = one("SELECT end_time FROM screening WHERE screening_id = %s", (screening_id,))
        clash = row["end_time"] - timedelta(minutes=1)
        response = API.post("/api/screenings", json={
            "movie_id": 1, "screen_id": 1, "start_time": clash.strftime("%Y-%m-%d %H:%M"),
            "base_price": 150})
        self.assertEqual(response.status_code, 409)

    def test_fifteen_minute_cleaning_gap_boundary_is_enforced(self):
        """end_time already contains the 15 minute gap, so a show starting only
        15 minutes after the previous one started would still overlap."""
        screening_id = fresh_show(440)
        row = one("SELECT start_time FROM screening WHERE screening_id = %s", (screening_id,))
        start = row["start_time"] + timedelta(minutes=15)
        response = API.post("/api/screenings", json={
            "movie_id": 1, "screen_id": 1, "start_time": start.strftime("%Y-%m-%d %H:%M"),
            "base_price": 150})
        self.assertEqual(response.status_code, 409,
                         "a 15 minute gap is not enough for a 120 minute movie")

    def test_starting_exactly_when_the_previous_show_ends_is_allowed(self):
        screening_id = fresh_show(460)
        row = one("SELECT end_time FROM screening WHERE screening_id = %s", (screening_id,))
        response = API.post("/api/screenings", json={
            "movie_id": 2, "screen_id": 1, "start_time": row["end_time"].strftime("%Y-%m-%d %H:%M"),
            "base_price": 120})
        self.assertEqual(response.status_code, 201, response.get_json())

    def test_identical_slot_is_rejected_by_the_unique_constraint(self):
        screening_id = fresh_show(500)
        row = one("SELECT start_time FROM screening WHERE screening_id = %s", (screening_id,))
        response = API.post("/api/screenings", json={
            "movie_id": 1, "screen_id": 1, "start_time": row["start_time"].strftime("%Y-%m-%d %H:%M"),
            "base_price": 150})
        self.assertEqual(response.status_code, 409)

    def test_cancelling_a_show_frees_the_slot_for_the_constraint(self):
        """A cancelled show stops blocking the screen: another show may now run
        across its old slot.  (The exact same start_time is still refused by
        UNIQUE(screen_id, start_time) - that is the next test.)"""
        screening_id = fresh_show(520)                       # a 135 minute block
        row = one("SELECT start_time FROM screening WHERE screening_id = %s", (screening_id,))
        self.assertEqual(API.post(f"/api/screenings/{screening_id}/cancel", json={}).status_code, 200)
        response = API.post("/api/screenings", json={
            "movie_id": 2, "screen_id": 1,
            "start_time": (row["start_time"] + timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M"),
            "base_price": 150})
        self.assertEqual(response.status_code, 201, response.get_json())

    def test_show_with_bookings_cannot_be_cancelled_or_deleted(self):
        screening_id = fresh_show(540)
        book_ok([seat_id(1, "A", 3)], screening_id)
        self.assertEqual(API.post(f"/api/screenings/{screening_id}/cancel", json={}).status_code, 409)
        self.assertEqual(API.delete(f"/api/screenings/{screening_id}").status_code, 409)

    def test_reschedule_moves_a_show_and_recomputes_end_time(self):
        screening_id = fresh_show(560, screen_id=2)
        new_start = (NOW + timedelta(hours=800)).strftime("%Y-%m-%d %H:%M")
        response = API.put(f"/api/screenings/{screening_id}",
                           json={"start_time": new_start, "base_price": 175})
        self.assertEqual(response.status_code, 200, response.get_json())
        show = response.get_json()["screening"]
        self.assertEqual(show["end_time"], (NOW + timedelta(hours=800) + timedelta(minutes=135))
                         .strftime("%Y-%m-%d %H:%M:%S"))


# =============================================================================
#  3. booking, seats and tickets
# =============================================================================
class BookingTests(unittest.TestCase):
    def test_booking_creates_one_ticket_per_seat_and_one_payment(self):
        screening_id = fresh_show(600)
        seats = [seat_id(1, "A", 1), seat_id(1, "A", 2)]
        self.assertEqual(seats_free(screening_id, seats), 0)
        booking = book_ok(seats, screening_id, method="UPI")
        self.assertEqual(booking["status"], "CONFIRMED")
        self.assertEqual(len(booking["seats"]), 2)
        self.assertEqual(len(booking["tickets"]), 2)
        self.assertEqual(len(booking["payments"]), 1)
        self.assertEqual(booking["payments"][0]["method"], "UPI")
        self.assertEqual(Decimal(str(booking["paid"])), Decimal("200.00"))
        self.assertEqual(seats_free(screening_id, seats), 2)
        for seat in booking["seats"]:
            self.assertTrue(seat["ticket_code"].startswith("TKT-"))
            self.assertEqual(seat["ticket_status"], "ISSUED")

    def test_ticket_price_uses_the_seat_multiplier(self):
        screening_id = fresh_show(620)
        seats = [seat_id(1, "A", 4), seat_id(1, "C", 4), seat_id(1, "F", 4)]
        booking = book_ok(seats, screening_id)
        prices = {seat["seat_label"]: float(seat["price"]) for seat in booking["seats"]}
        self.assertEqual(prices["C4"], 130.00)      # PREMIUM  × 1.3 of base 100
        self.assertEqual(prices["F4"], 160.00)      # RECLINER × 1.6
        self.assertEqual(prices["A4"], 100.00)      # STANDARD × 1.0
        self.assertEqual(float(booking["paid"]), 390.00)

    def test_double_booking_is_rejected_and_the_whole_request_rolls_back(self):
        screening_id = fresh_show(640)
        taken = seat_id(1, "B", 1)
        free = seat_id(1, "B", 2)
        book_ok([taken], screening_id, customer_id=2)

        response = book([taken, free], screening_id, customer_id=3)
        self.assertEqual(response.status_code, 409)
        self.assertIn("already booked", response.get_json()["error"])

        # the free seat of the failed request must still be free, and no money kept
        self.assertEqual(seats_free(screening_id, [free]), 0)
        self.assertEqual(value("SELECT count(*) AS n FROM booking WHERE screening_id = %s",
                               (screening_id,)), 1)
        self.assertEqual(value("SELECT count(*) AS n FROM payment p JOIN booking b "
                               "ON b.booking_id = p.booking_id WHERE b.screening_id = %s",
                               (screening_id,)), 1)

    def test_seat_from_another_screen_is_rejected_by_trigger_t1(self):
        screening_id = fresh_show(660)
        response = book([seat_id(2, "C", 5)], screening_id)
        self.assertEqual(response.status_code, 409)
        self.assertIn("screen", response.get_json()["error"].lower())
        self.assertEqual(seats_free(screening_id, [seat_id(2, "C", 5)]), 0)

    def test_booking_a_show_that_has_already_started_is_rejected(self):
        response = book([seat_id(1, "A", 6)], 4)
        self.assertEqual(response.status_code, 409)
        self.assertIn("started", response.get_json()["error"])

    def test_booking_a_cancelled_show_is_rejected(self):
        response = book([seat_id(1, "A", 7)], 5)
        self.assertEqual(response.status_code, 409)

    def test_at_most_ten_seats_per_booking(self):
        screening_id = fresh_show(680)
        eleven = [seat_id(1, "A", n) for n in range(1, 11)] + [seat_id(1, "B", 1)]
        response = book(eleven, screening_id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("10", response.get_json()["error"])

    def test_duplicate_seats_in_one_request_are_rejected(self):
        screening_id = fresh_show(700)
        seat = seat_id(1, "D", 2)
        response = book([seat, seat], screening_id)
        self.assertEqual(response.status_code, 400)

    def test_unknown_seat_is_rejected(self):
        screening_id = fresh_show(720)
        response = book([999999], screening_id)
        self.assertEqual(response.status_code, 400)

    def test_unknown_customer_and_unknown_show_are_rejected(self):
        screening_id = fresh_show(740)
        self.assertEqual(book([seat_id(1, "E", 1)], screening_id, customer_id=999).status_code, 400)
        self.assertEqual(book([seat_id(1, "E", 1)], 999999).status_code, 404)

    def test_invalid_payment_method_is_rejected(self):
        screening_id = fresh_show(760)
        self.assertEqual(book([seat_id(1, "E", 2)], screening_id, method="BITCOIN")
                         .status_code, 400)

    def test_walk_in_booking_records_the_staff_member(self):
        screening_id = fresh_show(780)
        booking = book_ok([seat_id(1, "E", 3)], screening_id, customer_id=1, method="CASH",
                          staff_id=2)
        self.assertEqual(booking["staff_id"], 2)
        self.assertEqual(booking["staff_name"], "Counter User")

    def test_seats_api_marks_booked_seats_as_taken(self):
        screening_id = fresh_show(800)
        taken = seat_id(1, "F", 1)
        book_ok([taken], screening_id)
        payload = API.get(f"/api/screenings/{screening_id}/seats").get_json()
        self.assertEqual(payload["total_seats"], 60)
        self.assertEqual(payload["taken_seats"], 1)
        self.assertEqual(payload["seats_left"], 59)
        marked = [seat for row in payload["rows"] for seat in row["seats"] if seat["seat_id"] == taken]
        self.assertTrue(marked and marked[0]["taken"])

    def test_show_listing_reports_seats_left_and_occupancy(self):
        screening_id = fresh_show(820)
        book_ok([seat_id(1, "A", 8), seat_id(1, "A", 9)], screening_id)
        payload = API.get(f"/api/screenings?limit=500").get_json()
        show = next(item for item in payload["screenings"]
                    if item["screening_id"] == screening_id)
        self.assertEqual(show["booked_seats"], 2)
        self.assertEqual(show["seats_left"], 58)
        self.assertEqual(float(show["occupancy_pct"]), 3.3)


# =============================================================================
#  4. cancellation, refunds, triggers T2/T3/T4
# =============================================================================
class RefundTests(unittest.TestCase):
    def test_refund_tiers_at_their_boundaries(self):
        booking = book_ok([seat_id(1, "A", 5)], 3)          # show in 72 hours
        booking_id = booking["booking_id"]
        cases = [
            (timedelta(hours=25), 100), (timedelta(hours=24), 100),
            (timedelta(hours=23, minutes=59), 50), (timedelta(hours=2), 50),
            (timedelta(hours=1, minutes=59), 0), (timedelta(hours=0), 0),
            (timedelta(hours=-5), 0),
        ]
        for offset, expected in cases:
            with self.subTest(offset=str(offset)):
                at = (NOW + timedelta(hours=72)) - offset
                got = value("SELECT fn_refund_percent(%s, %s) AS pct", (booking_id, at))
                self.assertEqual(got, expected)

    def test_refund_preview_shows_the_tier_and_the_amount(self):
        booking = book_ok([seat_id(1, "B", 5), seat_id(1, "C", 6)], 3)
        payload = API.get(f"/api/bookings/{booking['booking_id']}/refund-preview").get_json()
        self.assertEqual(payload["percent"], 100)
        self.assertTrue(payload["can_cancel"])
        self.assertEqual(payload["refund_amount"], 230.00)      # 100 + 130
        self.assertEqual(len(payload["policies"]), 3)

    def test_preview_of_a_show_under_two_hours_is_zero_percent(self):
        booking = book_ok([seat_id(1, "B", 6)], 1)
        payload = API.get(f"/api/bookings/{booking['booking_id']}/refund-preview").get_json()
        self.assertEqual(payload["percent"], 0)
        self.assertEqual(payload["refund_amount"], 0.0)

    def test_cancelling_frees_seats_voids_tickets_and_refunds(self):
        screening_id = fresh_show(900)
        seats = [seat_id(1, "C", 1), seat_id(1, "D", 1)]
        booking = book_ok(seats, screening_id)
        booking_id = booking["booking_id"]

        response = API.post(f"/api/bookings/{booking_id}/cancel",
                            json={"reason": "Plan changed", "cancelled_by": "CUSTOMER"})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["percent"], 100)
        self.assertEqual(payload["refund_amount"], 260.00)      # C1 + D1 are PREMIUM: 130 + 130
        self.assertEqual(payload["refund_id"] is not None, True)

        self.assertEqual(value("SELECT status FROM booking WHERE booking_id = %s", (booking_id,)),
                         "CANCELLED")
        self.assertEqual(value("SELECT count(*) AS n FROM booked_seat WHERE booking_id = %s "
                               "AND status = 'RELEASED'", (booking_id,)), 2)
        self.assertEqual(value("SELECT count(*) AS n FROM ticket t JOIN booked_seat bs "
                               "ON bs.booked_seat_id = t.booked_seat_id "
                               "WHERE bs.booking_id = %s AND t.status = 'VOID'", (booking_id,)), 2)
        self.assertEqual(value("SELECT status FROM payment WHERE booking_id = %s", (booking_id,)),
                         "REFUNDED")
        self.assertEqual(seats_free(screening_id, seats), 0)
        self.assertEqual(float(value("SELECT refund_amount FROM refund WHERE cancellation_id = %s",
                                     (payload["cancellation_id"],))), 260.00)

    def test_a_released_seat_can_be_booked_again_with_a_new_ticket_code(self):
        screening_id = fresh_show(920)
        seat = seat_id(1, "E", 5)
        first = book_ok([seat], screening_id)
        API.post(f"/api/bookings/{first['booking_id']}/cancel", json={})
        second = book_ok([seat], screening_id, customer_id=3)
        self.assertNotEqual(first["seats"][0]["ticket_code"], second["seats"][0]["ticket_code"])
        self.assertEqual(seats_free(screening_id, [seat]), 1)

    def test_cancelling_twice_is_rejected(self):
        screening_id = fresh_show(940)
        booking = book_ok([seat_id(1, "E", 6)], screening_id)
        self.assertEqual(API.post(f"/api/bookings/{booking['booking_id']}/cancel",
                                  json={}).status_code, 200)
        again = API.post(f"/api/bookings/{booking['booking_id']}/cancel", json={})
        self.assertEqual(again.status_code, 409)
        self.assertIn("cancel", again.get_json()["error"].lower())

    def test_cancelling_a_started_show_is_rejected_by_trigger_t2(self):
        # screen 2 carries no other show, so the show can be moved into the past
        # without disturbing - or being blocked by - the overlap constraint
        screening_id = fresh_show(1140, screen_id=2)
        booking = book_ok([seat_id(2, "A", 10)], screening_id)
        one("UPDATE screening SET start_time = %s, end_time = %s WHERE screening_id = %s",
            (NOW - timedelta(hours=4), NOW - timedelta(hours=2), screening_id))
        response = API.post(f"/api/bookings/{booking['booking_id']}/cancel", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("started", response.get_json()["error"])

    def test_refund_above_the_policy_limit_is_rejected_by_trigger_t4(self):
        screening_id = fresh_show(960)
        booking = book_ok([seat_id(1, "A", 6)], screening_id)     # 100% tier, paid 100
        API.post(f"/api/bookings/{booking['booking_id']}/cancel", json={})
        cancellation_id = value("SELECT cancellation_id FROM cancellation WHERE booking_id = %s",
                                (booking["booking_id"],))
        payment_id = value("SELECT payment_id FROM payment WHERE booking_id = %s",
                           (booking["booking_id"],))
        with self.assertRaises(psycopg2.errors.RaiseException) as caught:
            one("INSERT INTO refund (cancellation_id, payment_id, refund_amount) "
                "VALUES (%s, %s, 150.00) RETURNING refund_id", (cancellation_id, payment_id))
        self.assertIn("Refund limit exceeded", str(caught.exception))
        self.assertEqual(value("SELECT count(*) AS n FROM refund WHERE cancellation_id = %s",
                               (cancellation_id,)), 1)

    def test_refund_of_a_payment_from_another_booking_is_rejected(self):
        other = book_ok([seat_id(1, "B", 8)], fresh_show(980))
        first = book_ok([seat_id(1, "B", 9)], fresh_show(1000))
        API.post(f"/api/bookings/{first['booking_id']}/cancel", json={})
        cancellation_id = value("SELECT cancellation_id FROM cancellation WHERE booking_id = %s",
                                (first["booking_id"],))
        other_payment = value("SELECT payment_id FROM payment WHERE booking_id = %s",
                              (other["booking_id"],))
        with self.assertRaises(psycopg2.errors.RaiseException) as caught:
            one("INSERT INTO refund (cancellation_id, payment_id, refund_amount) "
                "VALUES (%s, %s, 10.00) RETURNING refund_id", (cancellation_id, other_payment))
        self.assertIn("not a successful payment", str(caught.exception))

    def test_partial_refund_tier_is_half(self):
        screening_id = 2            # the fixture show that starts in exactly 6 hours
        booking = book_ok([seat_id(1, "C", 9)], screening_id)      # 6 hours before the show
        response = API.post(f"/api/bookings/{booking['booking_id']}/cancel", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["percent"], 50)
        self.assertEqual(response.get_json()["refund_amount"], 65.00)
        self.assertEqual(float(value("SELECT refund_amount FROM refund r JOIN cancellation c "
                                     "ON c.cancellation_id = r.cancellation_id "
                                     "WHERE c.booking_id = %s", (booking["booking_id"],))), 65.00)

    def test_zero_percent_cancellation_keeps_the_money_but_frees_the_seat(self):
        booking = book_ok([seat_id(1, "D", 9)], 1)                 # show in one hour
        response = API.post(f"/api/bookings/{booking['booking_id']}/cancel", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["refund_amount"], 0.0)
        self.assertIsNone(response.get_json()["refund_id"])
        self.assertEqual(value("SELECT status FROM payment WHERE booking_id = %s",
                               (booking["booking_id"],)), "SUCCESS")
        self.assertEqual(value("SELECT count(*) AS n FROM booked_seat WHERE booking_id = %s "
                               "AND status = 'BOOKED'", (booking["booking_id"],)), 0)


# =============================================================================
#  5. concession orders, trigger T5 and stock
# =============================================================================
class ConcessionTests(unittest.TestCase):
    def test_order_line_takes_its_price_from_the_menu_and_reduces_stock(self):
        before = value("SELECT stock_qty FROM concession_item WHERE item_id = 1")
        response = API.post("/api/orders", json={
            "items": [{"item_id": 1, "quantity": 2}], "method": "CASH", "staff_id": 2})
        self.assertEqual(response.status_code, 201, response.get_json())
        order = response.get_json()["order"]
        self.assertEqual(len(order["items"]), 1)
        self.assertEqual(float(order["items"][0]["unit_price"]), 200.00)
        self.assertEqual(float(order["items"][0]["line_total"]), 400.00)
        self.assertEqual(value("SELECT stock_qty FROM concession_item WHERE item_id = 1"),
                         before - 2)
        self.assertEqual(len(order["payments"]), 1)
        self.assertEqual(float(order["payments"][0]["amount"]), 400.00)

    def test_ordering_more_than_the_stock_is_rejected_and_rolled_back(self):
        stock = value("SELECT stock_qty FROM concession_item WHERE item_id = 2")   # 4 left
        self.assertTrue(stock >= 1)
        response = API.post("/api/orders", json={
            "items": [{"item_id": 2, "quantity": stock + 1}], "method": "CARD"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("stock", response.get_json()["error"].lower())
        self.assertEqual(value("SELECT stock_qty FROM concession_item WHERE item_id = 2"), stock)

    def test_stock_check_constraint_blocks_negative_stock(self):
        with self.assertRaises(psycopg2.errors.CheckViolation):
            one("UPDATE concession_item SET stock_qty = -1 WHERE item_id = 1 RETURNING item_id")

    def test_out_of_stock_item_cannot_be_ordered(self):
        response = API.post("/api/orders", json={
            "items": [{"item_id": 3, "quantity": 1}], "method": "CARD"})
        self.assertEqual(response.status_code, 409)

    def test_quantity_bounds(self):
        self.assertEqual(API.post("/api/orders", json={
            "items": [{"item_id": 1, "quantity": 0}], "method": "CARD"}).status_code, 400)
        self.assertEqual(API.post("/api/orders", json={
            "items": [{"item_id": 1, "quantity": 21}], "method": "CARD"}).status_code, 400)

    def test_unknown_item_is_rejected(self):
        self.assertEqual(API.post("/api/orders", json={
            "items": [{"item_id": 999, "quantity": 1}], "method": "CARD"}).status_code, 400)

    def test_order_can_be_attached_to_a_booking(self):
        screening_id = fresh_show(1040)
        booking = book_ok([seat_id(1, "E", 9)], screening_id)
        response = API.post("/api/orders", json={
            "items": [{"item_id": 1, "quantity": 1}], "method": "UPI",
            "booking_id": booking["booking_id"]})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["order"]["booking_id"], booking["booking_id"])
        detail = API.get(f"/api/bookings/{booking['booking_id']}").get_json()["booking"]
        self.assertEqual(len(detail["orders"]), 1)

    def test_over_selling_is_rolled_back_completely(self):
        """A refused order must leave no order, no line and no payment behind."""
        first = API.post("/api/orders", json={"items": [{"item_id": 2, "quantity": 4}],
                                              "method": "CARD"})
        self.assertEqual(first.status_code, 201)
        orders_before = value("SELECT count(*) AS n FROM concession_order")
        payments_before = value("SELECT count(*) AS n FROM payment")
        second = API.post("/api/orders", json={"items": [{"item_id": 1, "quantity": 1},
                                                         {"item_id": 2, "quantity": 1}],
                                               "method": "CARD"})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(value("SELECT count(*) AS n FROM concession_order"), orders_before)
        self.assertEqual(value("SELECT count(*) AS n FROM payment"), payments_before)


# =============================================================================
#  6. customer data, constraints and referential integrity
# =============================================================================
class IntegrityTests(unittest.TestCase):
    def test_duplicate_email_is_rejected(self):
        response = API.post("/api/customers", json={
            "full_name": "Copy Cat", "email": "asha.rao@example.com", "phone": "9000011111"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("e-mail", response.get_json()["error"])

    def test_email_format_is_rejected(self):
        response = API.post("/api/customers", json={
            "full_name": "Bad Email", "email": "not-an-email", "phone": "9000011111"})
        self.assertEqual(response.status_code, 400)

    def test_phone_format_is_rejected_by_the_check_constraint(self):
        with self.assertRaises(psycopg2.errors.CheckViolation):
            one("INSERT INTO customer (full_name, email, phone) "
                "VALUES ('Bad Phone', 'bad.phone@example.com', '12ab') RETURNING customer_id")

    def test_customer_creation_works(self):
        response = API.post("/api/customers", json={
            "full_name": "New Patron", "email": "new.patron@example.com", "phone": "9123456780"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["customer"]["full_name"], "New Patron")

    def test_movie_with_screenings_cannot_be_deleted(self):
        response = API.delete("/api/movies/1")
        self.assertEqual(response.status_code, 409)
        self.assertIn("foreign key", response.get_json()["error"].lower())

    def test_walk_in_guest_cannot_be_deleted(self):
        response = API.delete("/api/customers/1")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Walk-in", response.get_json()["error"])

    def test_customer_with_bookings_cannot_be_deleted(self):
        customer = API.post("/api/customers", json={
            "full_name": "Booker Person", "email": "booker.person@example.com",
            "phone": "9123400000"}).get_json()["customer"]
        book_ok([seat_id(1, "F", 6)], fresh_show(1060), customer_id=customer["customer_id"])
        response = API.delete(f"/api/customers/{customer['customer_id']}")
        self.assertEqual(response.status_code, 409)

    def test_menu_item_in_an_order_cannot_be_deleted(self):
        response = API.delete("/api/items/1")
        self.assertEqual(response.status_code, 409)

    def test_screen_type_and_certificate_check_constraints(self):
        with self.assertRaises(psycopg2.errors.CheckViolation):
            one("INSERT INTO screen (cinema_id, screen_name, screen_type) "
                "VALUES (1, 'Bad Screen', 'HOLOGRAM') RETURNING screen_id")
        with self.assertRaises(psycopg2.errors.CheckViolation):
            one("INSERT INTO movie (title, genre, language, duration_min, certificate, release_date) "
                "VALUES ('X', 'Drama', 'English', 100, 'ZZ', '2026-01-01') RETURNING movie_id")

    def test_end_time_must_be_after_start_time(self):
        with self.assertRaises(psycopg2.errors.CheckViolation):
            one("INSERT INTO screening (movie_id, screen_id, start_time, end_time, base_price) "
                "VALUES (1, 2, %s, %s, 100) RETURNING screening_id",
                (NOW + timedelta(hours=1200), NOW + timedelta(hours=1199)))

    def test_payment_needs_a_booking_or_an_order(self):
        with self.assertRaises(psycopg2.errors.CheckViolation):
            one("INSERT INTO payment (amount, method, transaction_ref) "
                "VALUES (10, 'CARD', 'TXN-NOBODY') RETURNING payment_id")

    def test_payment_reference_is_unique(self):
        booking = book_ok([seat_id(1, "F", 3)], fresh_show(1160))
        reference = value("SELECT transaction_ref FROM payment WHERE booking_id = %s",
                          (booking["booking_id"],))
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            one("INSERT INTO payment (booking_id, amount, method, transaction_ref) "
                "VALUES (%s, 10, 'CARD', %s) RETURNING payment_id",
                (booking["booking_id"], reference))


# =============================================================================
#  7. reports, audits and the API surface
# =============================================================================
class ReportTests(unittest.TestCase):
    def test_every_named_query_runs_and_returns_metadata(self):
        catalogue = API.get("/api/queries").get_json()
        self.assertGreaterEqual(catalogue["count"], 17)
        for query in catalogue["queries"]:
            with self.subTest(query=query["key"]):
                response = API.get(f"/api/reports/{query['key']}")
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                for field in ("title", "kind", "sql", "columns", "rows", "count"):
                    self.assertIn(field, payload)
                self.assertEqual(payload["count"], len(payload["rows"]))

    def test_unknown_report_key_is_rejected(self):
        self.assertEqual(API.get("/api/reports/DROP_TABLE_users").status_code, 404)

    def test_overlap_audit_returns_no_rows(self):
        payload = API.get("/api/reports/audit_overlapping_shows").get_json()
        self.assertEqual(payload["count"], 0, payload["rows"])

    def test_double_booking_audit_returns_no_rows(self):
        payload = API.get("/api/reports/audit_double_booked_seat").get_json()
        self.assertEqual(payload["count"], 0, payload["rows"])

    def test_dashboard_reports_the_expected_key_metrics(self):
        payload = API.get("/api/dashboard").get_json()
        for key in ("net_revenue", "avg_occupancy_pct", "active_bookings", "cancellations",
                    "upcoming_shows", "customers", "tickets"):
            self.assertIn(key, payload["kpis"], key)
        self.assertTrue(payload["kpis"]["upcoming_shows"] >= 1)
        self.assertTrue(len(payload["trend"]) >= 1)

    def test_meta_endpoint(self):
        payload = API.get("/api/meta").get_json()
        self.assertEqual(payload["database"]["tables"], 17)
        self.assertEqual(payload["database"]["schema"], SCHEMA)
        self.assertEqual(len(payload["cinemas"]), 1)
        self.assertTrue(payload["fixed_clock"])

    def test_bookings_listing_filters_by_customer(self):
        payload = API.get("/api/bookings?customer_id=2&limit=5").get_json()
        self.assertTrue(all(row["customer_id"] == 2 for row in payload["bookings"]))


# =============================================================================
#  8. database page endpoints, source whitelist and the live SQL log
# =============================================================================
class DatabasePageTests(unittest.TestCase):
    def test_db_tables_lists_all_seventeen_tables(self):
        payload = API.get("/api/db/tables").get_json()
        self.assertEqual(payload["count"], 17)
        self.assertEqual({row["name"] for row in payload["tables"]}, set(db.TABLES))

    def test_table_read_returns_structure_and_rows(self):
        payload = API.get("/api/db/tables/screening?limit=5").get_json()
        self.assertEqual(payload["table"], "screening")
        self.assertTrue(payload["columns"])
        self.assertEqual(len(payload["primary_key"]), 1)
        self.assertTrue(len(payload["rows"]) <= 5)
        self.assertIn("CREATE TABLE screening", payload["ddl"])

    def test_non_whitelisted_table_is_rejected(self):
        self.assertEqual(API.get("/api/db/tables/pg_shadow").status_code, 404)
        self.assertEqual(API.get("/api/db/tables/users").status_code, 404)

    def test_sort_column_is_validated(self):
        self.assertEqual(API.get("/api/db/tables/booking?sort=;%20DROP").status_code, 400)
        self.assertEqual(API.get("/api/db/tables/booking?sort=not_a_column").status_code, 400)

    def test_limit_is_capped_at_one_hundred(self):
        payload = API.get("/api/db/tables/seat?limit=500").get_json()
        self.assertLessEqual(len(payload["rows"]), 100)

    def test_rows_can_be_searched(self):
        payload = API.get("/api/db/tables/customer?q=Asha").get_json()
        self.assertGreaterEqual(payload["row_count"], 1)

    def test_sql_console_accepts_a_select(self):
        payload = API.post("/api/db/query", json={
            "sql": "SELECT cinema_name, city FROM cinema"}).get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["columns"], ["cinema_name", "city"])

    def test_sql_console_accepts_with_and_a_trailing_semicolon(self):
        response = API.post("/api/db/query", json={
            "sql": "WITH t AS (SELECT count(*) AS n FROM booking) SELECT * FROM t;"})
        self.assertEqual(response.status_code, 200)

    def test_sql_console_rejects_writes_and_ddl(self):
        for statement in ("INSERT INTO cinema (cinema_name, city, address, phone) "
                          "VALUES ('x','y','z','1')",
                          "UPDATE booking SET status = 'CANCELLED'",
                          "DELETE FROM booking",
                          "DROP TABLE booking",
                          "ALTER TABLE booking ADD COLUMN x int",
                          "TRUNCATE booking",
                          "GRANT ALL ON booking TO PUBLIC",
                          "COPY booking TO '/tmp/x.csv'"):
            with self.subTest(statement=statement.split()[0]):
                response = API.post("/api/db/query", json={"sql": statement})
                self.assertEqual(response.status_code, 400, statement)
                self.assertIn("error", response.get_json())

    def test_sql_console_rejects_multiple_statements(self):
        response = API.post("/api/db/query", json={"sql": "SELECT 1; SELECT 2;"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("ONE statement", response.get_json()["error"])

    def test_sql_console_ignores_keywords_inside_string_literals(self):
        response = API.post("/api/db/query", json={
            "sql": "SELECT count(*) AS n FROM booked_seat WHERE status = 'DROP'"})
        self.assertEqual(response.status_code, 200)

    def test_sql_console_stays_out_of_the_live_log(self):
        API.post("/api/log/clear", json={})
        API.post("/api/db/query", json={"sql": "SELECT count(*) FROM booking"})
        entries = API.get("/api/log?since=0").get_json()["entries"]
        self.assertEqual(entries, [])

    def test_db_objects_lists_the_database_objects(self):
        payload = API.get("/api/db/objects").get_json()
        self.assertEqual(len(payload["views"]), 6)
        self.assertEqual(len(payload["triggers"]), 5)
        self.assertEqual(len(payload["indexes"]), 8)
        self.assertTrue(all("sql" in view for view in payload["views"]))
        self.assertTrue(all("function_sql" in trigger for trigger in payload["triggers"]))


class SourcePageTests(unittest.TestCase):
    def test_source_lists_only_whitelisted_files(self):
        payload = API.get("/api/source").get_json()
        paths = {entry["path"] for group in payload["groups"] for entry in group["files"]}
        self.assertIn("app.py", paths)
        self.assertIn("supabase/schema.sql", paths)
        self.assertIn("tests/test_cinema.py", paths)
        self.assertNotIn(".env", paths)
        self.assertTrue(all(not path.startswith(".") or path == ".env.example" for path in paths))

    def test_whitelisted_file_can_be_read(self):
        payload = API.get("/api/source/app.py").get_json()
        self.assertIn("def main()", payload["content"])
        self.assertTrue(payload["functions"])
        payload = API.get("/api/source/supabase/schema.sql").get_json()
        self.assertIn("CREATE TABLE booking", payload["content"])

    def test_secrets_and_traversal_are_refused(self):
        for path in ("/api/source/.env", "/api/source/../app.py", "/api/source/../.env",
                     "/api/source/.git/config", "/api/source/seed.py.bak",
                     "/api/source/logs/server.log"):
            with self.subTest(path=path):
                self.assertEqual(API.get(path).status_code, 404, path)

    def test_source_zip_contains_the_project_files(self):
        response = API.get("/api/source.zip")
        self.assertEqual(response.status_code, 200)
        self.assertIn("zip", response.headers["Content-Type"])
        self.assertGreater(len(response.data), 1000)


class SqlLogTests(unittest.TestCase):
    def test_log_records_begin_insert_commit_for_a_booking(self):
        API.post("/api/log/clear", json={})
        screening_id = fresh_show(1080)
        book_ok([seat_id(1, "F", 7)], screening_id)
        entries = API.get("/api/log?since=0").get_json()["entries"]
        kinds = [entry["kind"] for entry in entries]
        self.assertIn("BEGIN", kinds)
        self.assertIn("INSERT", kinds)
        self.assertTrue(kinds[-1] == "COMMIT", kinds)
        inserts = [entry for entry in entries if entry["kind"] == "INSERT"]
        self.assertTrue(any("INSERT INTO booking" in entry["statement"] for entry in inserts))
        self.assertTrue(any("INSERT INTO booked_seat" in entry["statement"] for entry in inserts))
        self.assertTrue(any("INSERT INTO ticket" in entry["statement"] for entry in inserts))
        self.assertTrue(any("INSERT INTO payment" in entry["statement"] for entry in inserts))
        self.assertTrue(all(entry["route"] for entry in entries))

    def test_log_records_a_rollback_for_a_refused_double_booking(self):
        screening_id = fresh_show(1100)
        seat = seat_id(1, "F", 8)
        book_ok([seat], screening_id)
        API.post("/api/log/clear", json={})
        response = book([seat, seat_id(1, "F", 9)], screening_id, customer_id=3)
        self.assertEqual(response.status_code, 409)
        entries = API.get("/api/log?since=0").get_json()["entries"]
        kinds = [entry["kind"] for entry in entries]
        self.assertIn("ROLLBACK", kinds)
        rollback = next(entry for entry in entries if entry["kind"] == "ROLLBACK")
        self.assertTrue(rollback["failed"])
        self.assertIn("ux_active_seat", rollback["error"] or "")
        self.assertNotIn("COMMIT", kinds)
        # and the free seat really stayed free
        self.assertEqual(seats_free(screening_id, [seat_id(1, "F", 9)]), 0)

    def test_log_captures_the_trigger_message(self):
        screening_id = fresh_show(1120)
        API.post("/api/log/clear", json={})
        self.assertEqual(book([seat_id(2, "A", 1)], screening_id).status_code, 409)
        entries = API.get("/api/log?since=0").get_json()["entries"]
        errors = [entry["error"] for entry in entries if entry["error"]]
        self.assertTrue(any("screen" in (error or "").lower() for error in errors), errors)

    def test_log_since_returns_only_new_entries(self):
        API.post("/api/log/clear", json={})
        API.get("/api/screenings?limit=1")
        first = API.get("/api/log?since=0").get_json()
        self.assertTrue(first["entries"])
        last_id = first["last_id"]
        second = API.get("/api/log?since=%d" % last_id).get_json()
        self.assertEqual(second["entries"], [])

    def test_log_clear_resets_the_buffer(self):
        API.get("/api/db/tables")
        API.post("/api/log/clear", json={})
        self.assertEqual(API.get("/api/log?since=0").get_json()["entries"], [])


# =============================================================================
#  runner
# =============================================================================
class JsonResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.records.append({"name": str(test), "status": "ok", "message": ""})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records.append({"name": str(test), "status": "failed",
                             "message": self._exc_info_to_string(err, test).splitlines()[-1][:300]})

    def addError(self, test, err):
        super().addError(test, err)
        self.records.append({"name": str(test), "status": "error",
                             "message": self._exc_info_to_string(err, test).splitlines()[-1][:300]})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.records.append({"name": str(test), "status": "skipped", "message": reason})


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=1 if as_json else 2,
                                     resultclass=JsonResult if as_json else unittest.TextTestResult,
                                     stream=sys.stderr if as_json else sys.stdout)
    result = runner.run(suite)

    total = result.testsRun
    passed = total - len(result.failures) - len(result.errors) - len(result.skipped)
    payload = {
        "ok": not result.failures and not result.errors,
        "total": total, "passed": passed, "failed": len(result.failures) + len(result.errors),
        "skipped": len(result.skipped),
        "tests": [{"name": record["name"].split(" ")[0], "status": record["status"],
                   "message": record["message"]} for record in getattr(result, "records", [])],
        "schema": SCHEMA,
        "cinema_now": FIXED_NOW,
    }
    if as_json:
        print(json.dumps(payload))
    else:
        print("=" * 74)
        print(f"  {payload['passed']} passed, {payload['failed']} failed, "
              f"{payload['skipped']} skipped  (schema {SCHEMA})")
        print("=" * 74)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
