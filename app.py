"""Galaxy Cinemas - Flask JSON API.

Every endpoint is JSON in / JSON out; every error is {"error": "readable text"}.
All SQL is plain SQL written here with %s placeholders - no ORM. Reads run in an
explicit BEGIN/COMMIT, writes run inside one transaction that either commits or
rolls back completely.

Run for the viva with:      python app.py      ->  http://127.0.0.1:5000
Honours:  PORT, CINEMA_NOW="YYYY-MM-DD HH:MM" (fixed clock), DATABASE_URL.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time
import zipfile
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.errors as pgerr
from flask import Flask, jsonify, request, send_file, send_from_directory

import db

ROOT = pathlib.Path(__file__).resolve().parent
STARTED = datetime.now()

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["JSON_SORT_KEYS"] = False
app.config["PROPAGATE_EXCEPTIONS"] = False

# ============================================================================
#  Errors
# ============================================================================


class ApiError(Exception):
    """A readable, user-facing failure with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# Constraint name -> the sentence the professor will read in the red toast.
CONSTRAINT_MESSAGES = {
    "ux_active_seat": "That seat is already booked for this show. The whole "
                      "booking was rolled back, so no seat and no money was held.",
    "ex_screening_no_overlap": "This show overlaps another show on the same "
                               "screen - the 15-minute cleaning gap is included.",
    "ux_screening_slot": "That screen already has a show starting at that exact minute.",
    "ux_booking_screening": "Duplicate booking/screening pair.",
    "customer_email_key": "A customer with that e-mail already exists.",
    "staff_username_key": "That staff username is already taken.",
    "concession_item_item_name_key": "A menu item with that name already exists.",
    "payment_transaction_ref_key": "That payment reference has already been used.",
    "ticket_ticket_code_key": "Ticket code collision - please retry the booking.",
    "ticket_booked_seat_id_key": "That seat already has a ticket.",
    "cancellation_booking_id_key": "That booking has already been cancelled.",
    "ux_cinema_name_city": "That cinema already exists in that city.",
    "ux_screen_name": "That cinema already has a screen with that name.",
    "ux_seat_position": "That seat already exists on that screen.",
    "ux_movie_identity": "That movie is already in the catalogue (title + language + release date).",
    "cancellation_policy_min_hours_before_key": "A refund tier with that many hours already exists.",
    "order_item_pkey": "That item is already on this order.",
}

CHECK_MESSAGES = [
    ("stock_qty", "Not enough stock left for that item."),
    ("refund_percent", "A refund percentage must be between 0 and 100."),
    ("min_hours_before", "Refund tiers cannot use negative hours."),
    ("email", "That e-mail address is not valid."),
    ("phone", "A phone number must be 10 to 15 digits."),
    ("duration_min", "A movie must be longer than 0 and at most 400 minutes."),
    ("unit_price", "A price must be greater than 0."),
    ("base_price", "A base price must be greater than 0."),
    ("refund_amount", "A refund must be greater than 0."),
    ("quantity", "A quantity must be between 1 and 20."),
    ("end_time", "A show must end after it starts."),
    ("booking_target", "A payment must belong to a booking or to a food order."),
    ("row_label", "A row label must be a single letter A-Z."),
    ("seat_no", "A seat number must be greater than 0."),
    ("screen_type", "The screen type must be one of 2D, 3D, IMAX, 4DX."),
    ("seat_type", "The seat type must be STANDARD, PREMIUM or RECLINER."),
    ("certificate", "The certificate must be U, UA, A or S."),
    ("category", "The item category must be POPCORN, BEVERAGE, SNACK or COMBO."),
    ("role", "The role must be ADMIN or BOX_OFFICE."),
    ("method", "The payment method must be CARD, UPI, CASH or WALLET."),
    ("cancelled_by", "The cancellation source must be CUSTOMER, STAFF or SYSTEM."),
    ("status", "That status value is not allowed."),
]


def _constraint_name(exc) -> str:
    return (getattr(exc, "diag", None) and exc.diag.constraint_name) or ""


def _check_message(name: str) -> str:
    for needle, message in CHECK_MESSAGES:
        if needle in name:
            return message
    return "The database rejected that value."


def _detail(exc) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:300]


def database_error_message(exc: psycopg2.Error):
    """Turn a database error into (message, http_status)."""
    name = _constraint_name(exc)
    if isinstance(exc, pgerr.UniqueViolation):
        return CONSTRAINT_MESSAGES.get(name, f"That record already exists ({name or 'duplicate value'})."), 409
    if isinstance(exc, pgerr.ExclusionViolation):
        return CONSTRAINT_MESSAGES.get(name, "This show collides with another show on the same screen."), 409
    if isinstance(exc, pgerr.ForeignKeyViolation):
        return ("Cannot change or delete this row: other rows still reference it "
                f"({name or 'foreign key'}). Delete the dependent rows first."), 409
    if isinstance(exc, pgerr.CheckViolation):
        return _check_message(name) + f"  [database: CHECK {name}]", 409
    if isinstance(exc, pgerr.NotNullViolation):
        return f"A required value is missing ([database: {name or 'NOT NULL'}]).", 409
    if isinstance(exc, pgerr.RaiseException):          # RAISE EXCEPTION in a trigger
        return _detail(exc).replace("CONTEXT:", "").strip(), 409
    if isinstance(exc, pgerr.QueryCanceled):
        return "The query took too long and was cancelled (statement_timeout).", 504
    if isinstance(exc, pgerr.InvalidTextRepresentation):
        return f"That value has the wrong format: {_detail(exc)}", 400
    if isinstance(exc, pgerr.NumericValueOutOfRange):
        return "That number is out of the allowed range.", 400
    if isinstance(exc, pgerr.InsufficientPrivilege):
        return "The database refused that operation: " + _detail(exc), 409
    if isinstance(exc, pgerr.OperationalError):
        return "Lost the connection to the database. Is Supabase awake? " + _detail(exc), 503
    if isinstance(exc, pgerr.SyntaxError):
        return "SQL syntax error: " + _detail(exc), 400
    if isinstance(exc, pgerr.InFailedSqlTransaction):
        return "The transaction was already aborted; nothing was saved.", 500
    return "Database error: " + _detail(exc), 500


@app.errorhandler(ApiError)
def handle_api_error(exc: ApiError):
    return jsonify({"error": exc.message}), exc.status


@app.errorhandler(psycopg2.Error)
def handle_db_error(exc: psycopg2.Error):
    message, status = database_error_message(exc)
    app.logger.info("database error -> %s %s", status, message)
    return jsonify({"error": message}), status


@app.errorhandler(db.ConfigError)
def handle_config_error(exc: db.ConfigError):
    return jsonify({"error": str(exc)}), 503


@app.errorhandler(404)
def handle_404(_exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": f"No such API endpoint: {request.path}"}), 404
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(405)
def handle_405(exc):
    return jsonify({"error": f"{request.method} is not allowed here."}), 405


@app.errorhandler(500)
def handle_500(exc):
    return jsonify({"error": "Unexpected server error. Check the Flask console."}), 500


# ============================================================================
#  Request plumbing
# ============================================================================


@app.before_request
def _tag_route():
    db.set_route(f"{request.method} {request.path}"
                 + (f"?{request.query_string.decode()[:60]}" if request.query_string else ""))


@app.after_request
def _no_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, private"
    return response


def body() -> dict:
    data = request.get_json(silent=True)
    if data is None:
        raise ApiError("Expected a JSON body.", 400)
    if not isinstance(data, dict):
        raise ApiError("The JSON body must be an object.", 400)
    return data


def need_int(data, key, minimum=None, maximum=None, label=None):
    label = label or key.replace("_", " ")
    value = data.get(key)
    if value is None or value == "":
        raise ApiError(f"'{label}' is required.", 400)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(f"'{label}' must be a whole number.", 400)
    if minimum is not None and number < minimum:
        raise ApiError(f"'{label}' must be at least {minimum}.", 400)
    if maximum is not None and number > maximum:
        raise ApiError(f"'{label}' must be at most {maximum}.", 400)
    return number


def need_str(data, key, min_len=1, max_len=200, label=None):
    label = label or key.replace("_", " ")
    value = data.get(key)
    if value is None:
        raise ApiError(f"'{label}' is required.", 400)
    text = str(value).strip()
    if len(text) < min_len:
        raise ApiError(f"'{label}' must be at least {min_len} character(s).", 400)
    if len(text) > max_len:
        raise ApiError(f"'{label}' must be at most {max_len} characters.", 400)
    return text


def need_choice(data, key, allowed, label=None):
    label = label or key.replace("_", " ")
    value = str(data.get(key) or "").strip().upper()
    if value not in allowed:
        raise ApiError(f"'{label}' must be one of {', '.join(allowed)}.", 400)
    return value


def need_float(data, key, minimum=None, label=None):
    label = label or key.replace("_", " ")
    value = data.get(key)
    if value is None or value == "":
        raise ApiError(f"'{label}' is required.", 400)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ApiError(f"'{label}' must be a number.", 400)
    if minimum is not None and number <= minimum:
        raise ApiError(f"'{label}' must be greater than {minimum}.", 400)
    return round(number, 2)


def need_date(data, key, label=None):
    return _parse_moment(data.get(key), key, label, date_only=True)


def need_moment(data, key, label=None):
    return _parse_moment(data.get(key), key, label, date_only=False)


def _parse_moment(value, key, label, date_only):
    label = label or key.replace("_", " ")
    if not value:
        raise ApiError(f"'{label}' is required.", 400)
    text = str(value).strip().replace("T", " ")
    formats = ["%Y-%m-%d"] if date_only else [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%d"]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ApiError(f"'{label}' must look like "
                   + ("'2026-10-14'." if date_only else "'2026-10-14 21:15'."), 400)


def smart(key, minimum=None, maximum=None, default=None):
    """Optional integer from a query-string filter (?key=7).

    Returns `default` when the filter is absent, 'all'/'any' or out of range,
    so a typo in a URL never breaks a page."""
    raw = request.args.get(key)
    if raw in (None, "", "all", "any"):
        return default
    try:
        number = int(raw)
    except ValueError:
        raise ApiError(f"Filter '{key}' must be a whole number.", 400)
    if minimum is not None and number < minimum:
        return default
    if maximum is not None and number > maximum:
        return default
    return number


def ok(payload, status=200):
    return jsonify(db.jsonable(payload)), status


def message(text, **extra):
    return ok({"message": text, **extra})


# ============================================================================
#  Small shared helpers
# ============================================================================

PRICE_CASE = ("CASE st.seat_type WHEN 'STANDARD' THEN 1.0 WHEN 'PREMIUM' THEN 1.3 "
              "ELSE 1.6 END")

_counts_cache = {"at": 0.0, "data": None}


def counts_cached(max_age: float = 4.0) -> dict:
    now = time.time()
    if _counts_cache["data"] is None or now - _counts_cache["at"] > max_age:
        _counts_cache["data"] = db.table_counts()
        _counts_cache["at"] = now
    return _counts_cache["data"]


def screening_filters(alias: str = "s"):
    """Shared WHERE clause for the show listings."""
    clauses, params = [], []
    date_arg = request.args.get("date")
    if date_arg:
        moment = _parse_moment(date_arg, "date", "date", True)
        clauses.append(f"{alias}.start_time::date = %s")
        params.append(moment.date())
    from_arg = request.args.get("from")
    if from_arg:
        moment = _parse_moment(from_arg, "from", "from", False)
        clauses.append(f"{alias}.start_time >= %s")
        params.append(moment)
    cinema_id = smart("cinema_id")
    if cinema_id:
        clauses.append("ci.cinema_id = %s")
        params.append(cinema_id)
    movie_id = smart("movie_id")
    if movie_id:
        clauses.append(f"{alias}.movie_id = %s")
        params.append(movie_id)
    screen_id = smart("screen_id")
    if screen_id:
        clauses.append(f"{alias}.screen_id = %s")
        params.append(screen_id)
    if request.args.get("upcoming") in ("1", "true", "yes"):
        clauses.append(f"{alias}.start_time > fn_now()")
    if request.args.get("bookable") in ("1", "true", "yes"):
        clauses.append(f"{alias}.start_time > fn_now() AND {alias}.status = 'SCHEDULED'")
    if request.args.get("status"):
        clauses.append(f"{alias}.status = %s")
        params.append(need_choice(request.args, "status",
                                  ["SCHEDULED", "CANCELLED", "COMPLETED"]))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


SHOW_SELECT = f"""
SELECT s.screening_id, s.movie_id, s.screen_id, s.start_time, s.end_time,
       s.base_price, s.status,
       m.title, m.genre, m.language, m.certificate, m.duration_min, m.release_date,
       sc.screen_name, sc.screen_type,
       ci.cinema_id, ci.cinema_name, ci.city,
       (SELECT count(*) FROM seat st WHERE st.screen_id = s.screen_id) AS total_seats,
       (SELECT count(*) FROM booked_seat bs
         WHERE bs.screening_id = s.screening_id AND bs.status = 'BOOKED') AS booked_seats,
       (SELECT count(*) FROM ticket t JOIN booked_seat bs
          ON bs.booked_seat_id = t.booked_seat_id
         WHERE bs.screening_id = s.screening_id AND t.status <> 'VOID') AS tickets_sold,
       (SELECT count(*) FROM seat st WHERE st.screen_id = s.screen_id)
       - (SELECT count(*) FROM booked_seat bs
           WHERE bs.screening_id = s.screening_id AND bs.status = 'BOOKED') AS seats_left,
       ROUND(100.0 * (SELECT count(*) FROM booked_seat bs
                       WHERE bs.screening_id = s.screening_id AND bs.status = 'BOOKED')
             / NULLIF((SELECT count(*) FROM seat st WHERE st.screen_id = s.screen_id), 0), 1)
                                                                          AS occupancy_pct,
       (s.status = 'SCHEDULED' AND s.start_time > fn_now()) AS bookable,
       ROUND(EXTRACT(EPOCH FROM (s.start_time - fn_now())) / 3600.0, 1) AS hours_to_show
  FROM screening s
  JOIN movie m   ON m.movie_id   = s.movie_id
  JOIN screen sc ON sc.screen_id = s.screen_id
  JOIN cinema ci ON ci.cinema_id = sc.cinema_id
"""


def fetch_show(screening_id: int, cur=None):
    sql = SHOW_SELECT + " WHERE s.screening_id = %s"
    if cur is not None:
        cur.execute(sql, (screening_id,))
        return cur.fetchone()
    return db.query_one(sql, (screening_id,))


# ============================================================================
#  Meta
# ============================================================================


@app.get("/api/health")
def api_health():
    return ok({"status": "ok", "database": db.ping(),
               "uptime_seconds": round((datetime.now() - STARTED).total_seconds(), 1)})


@app.get("/api/meta")
def api_meta():
    counts = counts_cached()
    cinemas = db.query("""
        SELECT ci.cinema_id, ci.cinema_name, ci.city, ci.address, ci.phone,
               (SELECT count(*) FROM screen sc WHERE sc.cinema_id = ci.cinema_id) AS screens
          FROM cinema ci ORDER BY ci.cinema_id""")
    screens = db.query("""
        SELECT sc.screen_id, sc.cinema_id, ci.cinema_name, sc.screen_name, sc.screen_type,
               (SELECT count(*) FROM seat st WHERE st.screen_id = sc.screen_id) AS seats
          FROM screen sc JOIN cinema ci USING (cinema_id)
         ORDER BY sc.cinema_id, sc.screen_id""")
    movies = db.query("""
        SELECT m.movie_id, m.title, m.genre, m.language, m.duration_min, m.certificate,
               m.release_date,
               (SELECT count(*) FROM screening s WHERE s.movie_id = m.movie_id) AS screenings
          FROM movie m ORDER BY m.title""")
    customers = db.query("""
        SELECT customer_id, full_name, email, phone FROM customer ORDER BY customer_id""")
    staff = db.query("""
        SELECT st.staff_id, st.full_name, st.role, st.cinema_id, ci.cinema_name
          FROM staff st JOIN cinema ci USING (cinema_id) ORDER BY st.staff_id""")
    items = db.query("""
        SELECT item_id, item_name, category, unit_price, stock_qty
          FROM concession_item ORDER BY category, item_name""")
    policies = db.query("""
        SELECT policy_id, min_hours_before, refund_percent
          FROM cancellation_policy ORDER BY min_hours_before DESC""")
    return ok({
        "app": "Galaxy Cinemas - Screening & Ticket Booking System",
        "subtitle": "Project 65 | DBMS Project-Based Learning",
        "server_time": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "cinema_now": db.scalar("SELECT fn_now() AS n"),
        "fixed_clock": bool(db.CINEMA_NOW),
        "database": {
            "engine": "Supabase PostgreSQL",
            "schema": db.SCHEMA,
            "tables": len(db.TABLES),
            "rows": sum(counts.values()),
            "counts": counts,
            "server": db.ping().get("version"),
        },
        "cinemas": cinemas, "screens": screens, "movies": movies,
        "customers": customers, "staff": staff, "items": items, "policies": policies,
        "seat_multipliers": db.SEAT_MULTIPLIER,
        "payment_methods": db.PAYMENT_METHODS,
        "categories": ["POPCORN", "BEVERAGE", "SNACK", "COMBO"],
        "certificates": ["U", "UA", "A", "S"],
        "seat_types": ["STANDARD", "PREMIUM", "RECLINER"],
        "role_tabs": {
            "CUSTOMER": ["now", "bookings", "food", "solution", "database", "source"],
            "STAFF": ["shows", "bookings", "counter", "customers", "solution", "database", "source"],
            "ADMIN": ["dashboard", "scheduling", "bookings", "reports", "master",
                      "solution", "database", "source"],
        },
    })


# ============================================================================
#  Movies, customers, concession items  (full CRUD)
# ============================================================================


def movie_or_404(cursor, movie_id):
    cursor.execute("SELECT * FROM movie WHERE movie_id = %s", (movie_id,))
    row = cursor.fetchone()
    if row is None:
        raise ApiError(f"Movie {movie_id} does not exist.", 404)
    return row


@app.get("/api/movies")
def movies_list():
    search = (request.args.get("q") or "").strip()
    params = []
    where = ""
    if search:
        where = ("WHERE (m.title ILIKE %s OR m.genre ILIKE %s OR m.language ILIKE %s "
                 "OR m.certificate ILIKE %s)")
        params = [f"%{search}%"] * 4
    rows = db.query(f"""
        SELECT m.*, (SELECT count(*) FROM screening s WHERE s.movie_id = m.movie_id) AS screenings,
               (SELECT count(*) FROM screening s WHERE s.movie_id = m.movie_id
                 AND s.start_time > fn_now()) AS upcoming
          FROM movie m {where}
         ORDER BY m.release_date DESC, m.title
         LIMIT 200""", params)
    return ok({"count": len(rows), "movies": rows})


@app.get("/api/movies/<int:movie_id>")
def movies_get(movie_id):
    with db.transaction() as cur:
        return ok({"movie": movie_or_404(cur, movie_id)})


@app.post("/api/movies")
def movies_create():
    data = body()
    payload = (
        need_str(data, "title", max_len=160),
        need_str(data, "genre", max_len=40),
        need_str(data, "language", max_len=40),
        need_int(data, "duration_min", minimum=1, maximum=400),
        need_choice(data, "certificate", ["U", "UA", "A", "S"]),
        need_date(data, "release_date").date(),
    )
    with db.transaction() as cur:
        cur.execute("""
            INSERT INTO movie (title, genre, language, duration_min, certificate, release_date)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""", payload)
        movie = cur.fetchone()
    return ok({"message": f"Movie '{movie['title']}' added to the catalogue.", "movie": movie}, 201)


@app.put("/api/movies/<int:movie_id>")
def movies_update(movie_id):
    data = body()
    with db.transaction() as cur:
        current = movie_or_404(cur, movie_id)
        merged = dict(current)
        merged.update({k: v for k, v in data.items() if k in merged})
        payload = (
            need_str(merged, "title", max_len=160),
            need_str(merged, "genre", max_len=40),
            need_str(merged, "language", max_len=40),
            need_int(merged, "duration_min", minimum=1, maximum=400),
            need_choice(merged, "certificate", ["U", "UA", "A", "S"]),
            need_date(merged, "release_date").date(),
            movie_id,
        )
        cur.execute("""
            UPDATE movie SET title=%s, genre=%s, language=%s, duration_min=%s,
                             certificate=%s, release_date=%s
             WHERE movie_id=%s RETURNING *""", payload)
        movie = cur.fetchone()
    return ok({"message": f"Movie '{movie['title']}' updated.", "movie": movie})


@app.delete("/api/movies/<int:movie_id>")
def movies_delete(movie_id):
    with db.transaction() as cur:
        movie = movie_or_404(cur, movie_id)
        cur.execute("SELECT count(*) AS n FROM screening WHERE movie_id = %s", (movie_id,))
        shows = cur.fetchone()["n"]
        if shows:
            raise ApiError(
                f"'{movie['title']}' still has {shows} show(s) on the schedule. "
                "A movie that is referenced by screenings cannot be deleted "
                "(foreign key ON DELETE RESTRICT).", 409)
        cur.execute("DELETE FROM movie WHERE movie_id = %s", (movie_id,))
    return message(f"Movie '{movie['title']}' deleted.")


CUSTOMER_SELECT = """
SELECT c.customer_id, c.full_name, c.email, c.phone, c.created_at,
       (SELECT count(*) FROM booking b WHERE b.customer_id = c.customer_id) AS bookings,
       (SELECT count(*) FROM booking b WHERE b.customer_id = c.customer_id
         AND b.status = 'CANCELLED') AS cancelled,
       (SELECT COALESCE(sum(p.amount), 0) FROM booking b
          JOIN payment p ON p.booking_id = b.booking_id AND p.status <> 'FAILED'
         WHERE b.customer_id = c.customer_id) AS spend
  FROM customer c
"""


@app.get("/api/customers")
def customers_list():
    search = (request.args.get("q") or "").strip()
    where, params = "", []
    if search:
        where = "WHERE (c.full_name ILIKE %s OR c.email ILIKE %s OR c.phone LIKE %s)"
        params = [f"%{search}%", f"%{search}%", f"%{search}%"]
    rows = db.query(CUSTOMER_SELECT + where + " ORDER BY c.customer_id LIMIT 300", params)
    return ok({"count": len(rows), "customers": rows})


@app.get("/api/customers/<int:customer_id>")
def customers_get(customer_id):
    row = db.query_one(CUSTOMER_SELECT + " WHERE c.customer_id = %s", (customer_id,))
    if row is None:
        raise ApiError(f"Customer {customer_id} does not exist.", 404)
    return ok({"customer": row})


@app.post("/api/customers")
def customers_create():
    data = body()
    payload = (
        need_str(data, "full_name", min_len=2, max_len=80),
        need_str(data, "email", max_len=120).lower(),
        need_str(data, "phone", min_len=10, max_len=15),
    )
    if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", payload[1]):
        raise ApiError("That e-mail address does not look valid.", 400)
    if not re.match(r"^[0-9]{10,15}$", payload[2]):
        raise ApiError("A phone number must be 10 to 15 digits (no spaces or +).", 400)
    with db.transaction() as cur:
        cur.execute("INSERT INTO customer (full_name, email, phone) VALUES (%s, %s, %s) "
                    "RETURNING *", payload)
        customer = cur.fetchone()
    return ok({"message": f"Customer '{customer['full_name']}' registered.", "customer": customer}, 201)


@app.put("/api/customers/<int:customer_id>")
def customers_update(customer_id):
    data = body()
    with db.transaction() as cur:
        cur.execute("SELECT * FROM customer WHERE customer_id = %s", (customer_id,))
        current = cur.fetchone()
        if current is None:
            raise ApiError(f"Customer {customer_id} does not exist.", 404)
        merged = dict(current)
        merged.update({k: v for k, v in data.items() if k in merged})
        name = need_str(merged, "full_name", min_len=2, max_len=80)
        email = need_str(merged, "email", max_len=120).lower()
        phone = str(merged.get("phone") or "").strip()
        if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email):
            raise ApiError("That e-mail address does not look valid.", 400)
        if not re.match(r"^[0-9]{10,15}$", phone):
            raise ApiError("A phone number must be 10 to 15 digits.", 400)
        cur.execute("""UPDATE customer SET full_name=%s, email=%s, phone=%s
                        WHERE customer_id=%s RETURNING *""",
                    (name, email, phone, customer_id))
        customer = cur.fetchone()
    return ok({"message": f"Customer '{customer['full_name']}' updated.", "customer": customer})


@app.delete("/api/customers/<int:customer_id>")
def customers_delete(customer_id):
    if customer_id == 1:
        raise ApiError("customer_id = 1 is the shared 'Walk-in Guest' used by every "
                       "counter sale, so it is protected and cannot be deleted.", 409)
    with db.transaction() as cur:
        cur.execute("SELECT * FROM customer WHERE customer_id = %s", (customer_id,))
        customer = cur.fetchone()
        if customer is None:
            raise ApiError(f"Customer {customer_id} does not exist.", 404)
        cur.execute("SELECT count(*) AS n FROM booking WHERE customer_id = %s", (customer_id,))
        bookings = cur.fetchone()["n"]
        if bookings:
            raise ApiError(
                f"{customer['full_name']} still has {bookings} booking(s). Delete or cancel "
                "them first - bookings protect their customer (ON DELETE RESTRICT).", 409)
        cur.execute("DELETE FROM customer WHERE customer_id = %s", (customer_id,))
    return message(f"Customer '{customer['full_name']}' deleted.")


ITEM_SELECT = """
SELECT i.item_id, i.item_name, i.category, i.unit_price, i.stock_qty,
       (SELECT COALESCE(sum(oi.quantity), 0) FROM order_item oi WHERE oi.item_id = i.item_id) AS sold
  FROM concession_item i
"""


@app.get("/api/items")
def items_list():
    search = (request.args.get("q") or "").strip()
    where, params = "", []
    if search:
        where = "WHERE (i.item_name ILIKE %s OR i.category ILIKE %s)"
        params = [f"%{search}%", f"%{search}%"]
    rows = db.query(ITEM_SELECT + where + " ORDER BY i.category, i.item_name", params)
    return ok({"count": len(rows), "items": rows})


@app.post("/api/items")
def items_create():
    data = body()
    payload = (
        need_str(data, "item_name", max_len=80),
        need_choice(data, "category", ["POPCORN", "BEVERAGE", "SNACK", "COMBO"]),
        need_float(data, "unit_price", minimum=0),
        need_int(data, "stock_qty", minimum=0, maximum=100000),
    )
    with db.transaction() as cur:
        cur.execute("""INSERT INTO concession_item (item_name, category, unit_price, stock_qty)
                       VALUES (%s, %s, %s, %s) RETURNING *""", payload)
        item = cur.fetchone()
    return ok({"message": f"'{item['item_name']}' added to the menu.", "item": item}, 201)


@app.put("/api/items/<int:item_id>")
def items_update(item_id):
    data = body()
    with db.transaction() as cur:
        cur.execute("SELECT * FROM concession_item WHERE item_id = %s", (item_id,))
        current = cur.fetchone()
        if current is None:
            raise ApiError(f"Item {item_id} does not exist.", 404)
        merged = dict(current)
        merged.update({k: v for k, v in data.items() if k in merged})
        payload = (
            need_str(merged, "item_name", max_len=80),
            need_choice(merged, "category", ["POPCORN", "BEVERAGE", "SNACK", "COMBO"]),
            need_float(merged, "unit_price", minimum=0),
            need_int(merged, "stock_qty", minimum=0, maximum=100000),
            item_id,
        )
        cur.execute("""UPDATE concession_item SET item_name=%s, category=%s,
                              unit_price=%s, stock_qty=%s
                        WHERE item_id=%s RETURNING *""", payload)
        item = cur.fetchone()
    return ok({"message": f"'{item['item_name']}' updated.", "item": item})


@app.delete("/api/items/<int:item_id>")
def items_delete(item_id):
    with db.transaction() as cur:
        cur.execute("SELECT * FROM concession_item WHERE item_id = %s", (item_id,))
        item = cur.fetchone()
        if item is None:
            raise ApiError(f"Item {item_id} does not exist.", 404)
        cur.execute("SELECT count(*) AS n FROM order_item WHERE item_id = %s", (item_id,))
        used = cur.fetchone()["n"]
        if used:
            raise ApiError(f"'{item['item_name']}' appears in {used} order line(s) and cannot "
                           "be deleted (ON DELETE RESTRICT).", 409)
        cur.execute("DELETE FROM concession_item WHERE item_id = %s", (item_id,))
    return message(f"'{item['item_name']}' removed from the menu.")


# ============================================================================
#  Screenings
# ============================================================================


@app.get("/api/screenings")
def screenings_list():
    where, params = screening_filters("s")
    limit = smart("limit", minimum=1, maximum=500) or 200
    if request.args.get("upcoming") in ("1", "true", "yes") and not request.args.get("from"):
        rows = db.query(SHOW_SELECT + f" {where} ORDER BY s.start_time LIMIT %s",
                        params + [limit])
    else:
        rows = db.query(SHOW_SELECT + f" {where} ORDER BY s.start_time DESC LIMIT %s",
                        params + [limit])
    return ok({"count": len(rows), "filters": dict(request.args), "screenings": rows})


@app.get("/api/screenings/<int:screening_id>")
def screenings_get(screening_id):
    show = fetch_show(screening_id)
    if show is None:
        raise ApiError(f"Show {screening_id} does not exist.", 404)
    return ok({"screening": show})


@app.post("/api/screenings")
def screenings_create():
    data = body()
    movie_id = need_int(data, "movie_id", minimum=1)
    screen_id = need_int(data, "screen_id", minimum=1)
    start = need_moment(data, "start_time", label="start_time")
    price = need_float(data, "base_price", minimum=0)
    with db.transaction() as cur:
        cur.execute("SELECT s.* FROM fn_schedule_screening(%s, %s, %s, %s) s",
                    (movie_id, screen_id, start, price))
        created = cur.fetchone()
        show = fetch_show(created["screening_id"], cur)
    return ok({"message": f"Show scheduled: {show['title']} at "
                          f"{show['start_time']:%d %b %Y %H:%M} on {show['screen_name']}.",
               "screening": show}, 201)


@app.put("/api/screenings/<int:screening_id>")
def screenings_update(screening_id):
    data = body()
    with db.transaction() as cur:
        cur.execute("SELECT * FROM screening WHERE screening_id = %s FOR UPDATE",
                    (screening_id,))
        current = cur.fetchone()
        if current is None:
            raise ApiError(f"Show {screening_id} does not exist.", 404)
        if current["status"] != "SCHEDULED":
            raise ApiError(f"This show is {current['status']}; only a SCHEDULED show "
                           "can be rescheduled.", 409)
        cur.execute("SELECT count(*) AS n FROM booking WHERE screening_id = %s "
                    "AND status = 'CONFIRMED'", (screening_id,))
        taken = cur.fetchone()["n"]
        if taken:
            raise ApiError(f"This show already has {taken} confirmed booking(s); it cannot be "
                           "moved. Cancel the show or the bookings first.", 409)

        movie_id = need_int({**current, **data}, "movie_id", minimum=1)
        screen_id = need_int({**current, **data}, "screen_id", minimum=1)
        price = need_float({**current, **data}, "base_price", minimum=0)
        start = (need_moment(data, "start_time", label="start_time")
                 if data.get("start_time") else current["start_time"])
        cur.execute("SELECT fn_now() AS n")
        if start <= cur.fetchone()["n"]:
            raise ApiError("A show can only be moved to a future time.", 409)
        cur.execute("""
            UPDATE screening s
               SET movie_id = %s,
                   screen_id = %s,
                   start_time = %s,
                   end_time = %s + make_interval(mins => (SELECT duration_min FROM movie
                                                            WHERE movie_id = %s) + 15),
                   base_price = %s
             WHERE screening_id = %s
         RETURNING screening_id""", (movie_id, screen_id, start, start, movie_id, price,
                                     screening_id))
        show = fetch_show(screening_id, cur)
    return ok({"message": f"Show {screening_id} moved to "
                          f"{show['start_time']:%d %b %Y %H:%M}.", "screening": show})


@app.post("/api/screenings/<int:screening_id>/cancel")
def screenings_cancel(screening_id):
    with db.transaction() as cur:
        cur.execute("SELECT * FROM screening WHERE screening_id = %s FOR UPDATE",
                    (screening_id,))
        current = cur.fetchone()
        if current is None:
            raise ApiError(f"Show {screening_id} does not exist.", 404)
        if current["status"] == "CANCELLED":
            raise ApiError("This show is already cancelled.", 409)
        cur.execute("SELECT count(*) AS n FROM booking WHERE screening_id = %s "
                    "AND status = 'CONFIRMED'", (screening_id,))
        taken = cur.fetchone()["n"]
        if taken:
            raise ApiError(f"Show {screening_id} has {taken} confirmed booking(s). A show can "
                           "only be cancelled while nothing is sold for it.", 409)
        cur.execute("UPDATE screening SET status = 'CANCELLED' WHERE screening_id = %s",
                    (screening_id,))
        show = fetch_show(screening_id, cur)
    return ok({"message": "Show cancelled - the slot is free again for the EXCLUDE constraint.",
               "screening": show})


@app.delete("/api/screenings/<int:screening_id>")
def screenings_delete(screening_id):
    with db.transaction() as cur:
        cur.execute("SELECT * FROM screening WHERE screening_id = %s FOR UPDATE",
                    (screening_id,))
        current = cur.fetchone()
        if current is None:
            raise ApiError(f"Show {screening_id} does not exist.", 404)
        cur.execute("SELECT count(*) AS n FROM booking WHERE screening_id = %s",
                    (screening_id,))
        booked = cur.fetchone()["n"]
        if booked:
            raise ApiError(f"Show {screening_id} already has {booked} booking(s) and cannot be "
                           "deleted. Cancel the show instead so the history is kept.", 409)
        cur.execute("DELETE FROM screening WHERE screening_id = %s", (screening_id,))
    return message(f"Show {screening_id} deleted from the schedule.")


@app.get("/api/screenings/<int:screening_id>/seats")
def screenings_seats(screening_id):
    show = fetch_show(screening_id)
    if show is None:
        raise ApiError(f"Show {screening_id} does not exist.", 404)
    seats = db.query(f"""
        SELECT st.seat_id, st.row_label, st.seat_no, st.seat_type,
               ROUND(s.base_price * {PRICE_CASE}, 2) AS price,
               (bs.booked_seat_id IS NOT NULL) AS taken,
               bs.booked_seat_id
          FROM screening s
          JOIN seat st ON st.screen_id = s.screen_id
          LEFT JOIN booked_seat bs
                 ON bs.screening_id = s.screening_id
                AND bs.seat_id = st.seat_id
                AND bs.status = 'BOOKED'
         WHERE s.screening_id = %s
         ORDER BY st.row_label, st.seat_no""", (screening_id,))

    rows, order = [], []
    for seat in seats:
        if seat["row_label"] not in order:
            order.append(seat["row_label"])
            rows.append({"row_label": seat["row_label"], "seats": []})
        index = order.index(seat["row_label"])
        rows[index]["seats"].append({
            "seat_id": seat["seat_id"], "seat_no": seat["seat_no"],
            "seat_type": seat["seat_type"], "price": seat["price"],
            "taken": bool(seat["taken"]), "label": f"{seat['row_label']}{seat['seat_no']}",
        })
    taken = sum(1 for seat in seats if seat["taken"])
    return ok({
        "screening": show,
        "screen": {"screen_id": show["screen_id"], "screen_name": show["screen_name"],
                   "screen_type": show["screen_type"], "cinema_name": show["cinema_name"]},
        "rows": rows,
        "total_seats": len(seats),
        "taken_seats": taken,
        "seats_left": len(seats) - taken,
        "max_seats_per_booking": 10,
        "legend": db.SEAT_MULTIPLIER,
    })


# ============================================================================
#  Bookings
# ============================================================================


def booking_detail(cur, booking_id):
    cur.execute("""
        SELECT b.booking_id, b.customer_id, b.screening_id, b.staff_id, b.booking_time,
               b.status, c.full_name AS customer_name, c.email AS customer_email,
               c.phone AS customer_phone,
               s.start_time, s.end_time, s.base_price, sc.screen_name, sc.screen_type,
               ci.cinema_name, ci.city, m.title AS movie_title, m.language, m.certificate,
               st.full_name AS staff_name,
               (SELECT COALESCE(sum(t.price), 0) FROM ticket t
                  JOIN booked_seat bs ON bs.booked_seat_id = t.booked_seat_id
                 WHERE bs.booking_id = b.booking_id AND t.status <> 'VOID') AS ticket_total,
               (SELECT count(*) FROM booked_seat bs WHERE bs.booking_id = b.booking_id
                 AND bs.status = 'BOOKED') AS seats_held
          FROM booking b
          JOIN customer c ON c.customer_id = b.customer_id
          JOIN screening s ON s.screening_id = b.screening_id
          JOIN movie m ON m.movie_id = s.movie_id
          JOIN screen sc ON sc.screen_id = s.screen_id
          JOIN cinema ci ON ci.cinema_id = sc.cinema_id
          LEFT JOIN staff st ON st.staff_id = b.staff_id
         WHERE b.booking_id = %s""", (booking_id,))
    booking = cur.fetchone()
    if booking is None:
        raise ApiError(f"Booking {booking_id} does not exist.", 404)

    cur.execute("""
        SELECT bs.booked_seat_id, bs.seat_id, bs.status AS seat_status,
               st.row_label, st.seat_no, st.seat_type,
               (st.row_label || st.seat_no) AS seat_label,
               t.ticket_id, t.ticket_code, t.price, t.status AS ticket_status, t.issued_at
          FROM booked_seat bs
          JOIN seat st ON st.seat_id = bs.seat_id
          LEFT JOIN ticket t ON t.booked_seat_id = bs.booked_seat_id
         WHERE bs.booking_id = %s
         ORDER BY st.row_label, st.seat_no""", (booking_id,))
    seats = cur.fetchall()

    cur.execute("""SELECT payment_id, amount, method, status, paid_at, transaction_ref
                     FROM payment WHERE booking_id = %s ORDER BY payment_id""",
                (booking_id,))
    payments = cur.fetchall()

    cur.execute("""
        SELECT o.order_id, o.order_time, o.status,
               (SELECT COALESCE(sum(oi.quantity * oi.unit_price), 0) FROM order_item oi
                 WHERE oi.order_id = o.order_id) AS total,
               (SELECT count(*) FROM order_item oi WHERE oi.order_id = o.order_id) AS lines
          FROM concession_order o WHERE o.booking_id = %s ORDER BY o.order_id""", (booking_id,))
    orders = cur.fetchall()

    cur.execute("""
        SELECT c.cancellation_id, c.cancelled_at, c.cancelled_by, c.reason,
               (SELECT COALESCE(sum(r.refund_amount), 0) FROM refund r
                 WHERE r.cancellation_id = c.cancellation_id) AS refund_amount,
               fn_refund_percent(b.booking_id, c.cancelled_at) AS refund_percent
          FROM cancellation c JOIN booking b ON b.booking_id = c.booking_id
         WHERE c.booking_id = %s""", (booking_id,))
    cancellation = cur.fetchone()

    booking["seats"] = seats
    booking["payments"] = payments
    booking["orders"] = orders
    booking["cancellation"] = cancellation
    booking["tickets"] = [dict(seat) for seat in seats if seat.get("ticket_code")]
    booking["paid"] = sum(float(p["amount"]) for p in payments if p["status"] != "FAILED")
    booking["refunded"] = float(cancellation["refund_amount"]) if cancellation else 0.0
    return booking


@app.post("/api/bookings")
def bookings_create():
    data = body()
    customer_id = need_int(data, "customer_id", minimum=1)
    screening_id = need_int(data, "screening_id", minimum=1)
    method = need_choice(data, "method", db.PAYMENT_METHODS)
    staff_id = None
    if data.get("staff_id") not in (None, ""):
        try:
            staff_id = int(data["staff_id"])
        except (TypeError, ValueError):
            raise ApiError("'staff_id' must be a whole number.", 400)

    raw_seats = data.get("seat_ids")
    if not isinstance(raw_seats, list) or not raw_seats:
        raise ApiError("'seat_ids' must be a non-empty list of seat numbers.", 400)
    try:
        seat_ids = [int(value) for value in raw_seats]
    except (TypeError, ValueError):
        raise ApiError("Every entry of 'seat_ids' must be a whole number.", 400)
    if len(seat_ids) > 10:
        raise ApiError(f"You selected {len(seat_ids)} seats, but at most 10 seats are allowed "
                       "in one booking.", 400)
    if len(set(seat_ids)) != len(seat_ids):
        raise ApiError("The same seat was selected twice.", 400)

    with db.transaction() as cur:
        cur.execute("""SELECT screening_id, screen_id, movie_id, start_time, end_time,
                              base_price, status
                         FROM screening WHERE screening_id = %s FOR UPDATE""", (screening_id,))
        show = cur.fetchone()
        if show is None:
            raise ApiError(f"Show {screening_id} does not exist.", 404)
        if show["status"] != "SCHEDULED":
            raise ApiError(f"This show is {show['status']}, so seats cannot be booked for it.", 409)
        cur.execute("SELECT fn_now() AS n")
        now = cur.fetchone()["n"]
        if show["start_time"] <= now:
            raise ApiError(f"This show started at {show['start_time']:%d %b %Y %H:%M}; booking is "
                           "closed (trigger fn_validate_booked_seat).", 409)

        cur.execute("SELECT customer_id, full_name FROM customer WHERE customer_id = %s",
                    (customer_id,))
        customer = cur.fetchone()
        if customer is None:
            raise ApiError(f"Customer {customer_id} does not exist.", 400)
        if staff_id is not None:
            cur.execute("SELECT staff_id FROM staff WHERE staff_id = %s", (staff_id,))
            if cur.fetchone() is None:
                raise ApiError(f"Staff {staff_id} does not exist.", 400)

        cur.execute("""SELECT seat_id, row_label, seat_no, seat_type, screen_id
                         FROM seat WHERE seat_id = ANY(%s)""", (seat_ids,))
        seats = {row["seat_id"]: row for row in cur.fetchall()}
        for seat_id in seat_ids:
            if seat_id not in seats:
                raise ApiError(f"Seat {seat_id} does not exist.", 400)
        strangers = [f"{row['row_label']}{row['seat_no']}" for row in seats.values()
                     if row["screen_id"] != show["screen_id"]]
        if strangers:
            raise ApiError(f"Seat(s) {', '.join(sorted(strangers))} are not in this screen "
                           f"({show['screen_id']}); trigger fn_validate_booked_seat rejects "
                           "a seat from another screen.", 409)

        cur.execute("""INSERT INTO booking (customer_id, screening_id, staff_id)
                       VALUES (%s, %s, %s) RETURNING booking_id, booking_time""",
                    (customer_id, screening_id, staff_id))
        booking = cur.fetchone()
        booking_id = booking["booking_id"]

        tickets_to_write = []
        for seat_id in seat_ids:
            cur.execute("""INSERT INTO booked_seat (booking_id, screening_id, seat_id)
                           VALUES (%s, %s, %s) RETURNING booked_seat_id""",
                        (booking_id, screening_id, seat_id))
            booked_seat_id = cur.fetchone()["booked_seat_id"]
            tickets_to_write.append((booked_seat_id, f"TKT-{booked_seat_id:06d}",
                                     show["base_price"], seats[seat_id]["seat_type"]))

        # The price rule lives in SQL: ticket price = base_price x multiplier
        # (STANDARD 1.0, PREMIUM 1.3, RECLINER 1.6).
        for booked_seat_id, code, base_price, seat_type in tickets_to_write:
            cur.execute("""INSERT INTO ticket (booked_seat_id, ticket_code, price)
                           VALUES (%s, %s,
                                   ROUND(%s * CASE %s WHEN 'STANDARD' THEN 1.0
                                                      WHEN 'PREMIUM'  THEN 1.3
                                                      ELSE 1.6 END, 2))""",
                        (booked_seat_id, code, base_price, seat_type))

        cur.execute("SELECT COALESCE(sum(price), 0) AS total FROM ticket t "
                    "JOIN booked_seat bs ON bs.booked_seat_id = t.booked_seat_id "
                    "WHERE bs.booking_id = %s AND t.status <> 'VOID'", (booking_id,))
        amount = cur.fetchone()["total"]
        reference = (f"TXN-{now:%Y%m%d%H%M%S}-"
                     f"{secrets.token_hex(2).upper()}")
        cur.execute("""INSERT INTO payment (booking_id, amount, method, transaction_ref, paid_at)
                       VALUES (%s, %s, %s, %s, %s) RETURNING payment_id""",
                    (booking_id, amount, method, reference, now))

        detail = booking_detail(cur, booking_id)

    return ok({"message": f"Booked {len(seat_ids)} seat(s) for {detail['movie_title']} - "
                          f"{len(seat_ids)} ticket(s) issued, INR {detail['paid']:.2f} paid by "
                          f"{method}.",
               "booking": detail}, 201)


@app.get("/api/bookings")
def bookings_list():
    clauses, params = ["1 = 1"], []
    customer_id = smart("customer_id")
    if customer_id:
        clauses.append("b.customer_id = %s")
        params.append(customer_id)
    screening_id = smart("screening_id")
    if screening_id:
        clauses.append("b.screening_id = %s")
        params.append(screening_id)
    if request.args.get("status"):
        clauses.append("b.status = %s")
        params.append(need_choice(request.args, "status", ["CONFIRMED", "CANCELLED"]))
    if request.args.get("date"):
        clause = _parse_moment(request.args["date"], "date", "date", True)
        clauses.append("s.start_time::date = %s")
        params.append(clause.date())
    search = (request.args.get("q") or "").strip()
    if search:
        clauses.append("(c.full_name ILIKE %s OR c.email ILIKE %s OR m.title ILIKE %s "
                       "OR b.booking_id::text = %s)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%", search]
    limit = smart("limit", minimum=1, maximum=200) or 60
    rows = db.query(f"""
        SELECT b.booking_id, b.customer_id, b.screening_id, b.status, b.booking_time,
               c.full_name AS customer_name, c.email, m.title AS movie_title,
               s.start_time, sc.screen_name, ci.cinema_name, b.staff_id,
               (SELECT count(*) FROM booked_seat bs WHERE bs.booking_id = b.booking_id
                 AND bs.status = 'BOOKED') AS seats,
               (SELECT COALESCE(sum(t.price), 0) FROM ticket t
                  JOIN booked_seat bs ON bs.booked_seat_id = t.booked_seat_id
                 WHERE bs.booking_id = b.booking_id AND t.status <> 'VOID') AS total,
               (SELECT COALESCE(sum(r.refund_amount), 0) FROM refund r
                  JOIN cancellation cn ON cn.cancellation_id = r.cancellation_id
                 WHERE cn.booking_id = b.booking_id) AS refunded,
               (SELECT count(*) FROM concession_order o WHERE o.booking_id = b.booking_id) AS orders,
               (b.status = 'CONFIRMED' AND s.start_time > fn_now()) AS cancellable,
               (SELECT string_agg(st.row_label || st.seat_no, ', ' ORDER BY st.row_label,
                                  st.seat_no)
                  FROM booked_seat bs JOIN seat st ON st.seat_id = bs.seat_id
                 WHERE bs.booking_id = b.booking_id AND bs.status = 'BOOKED') AS seat_labels
          FROM booking b
          JOIN customer c ON c.customer_id = b.customer_id
          JOIN screening s ON s.screening_id = b.screening_id
          JOIN movie m ON m.movie_id = s.movie_id
          JOIN screen sc ON sc.screen_id = s.screen_id
          JOIN cinema ci ON ci.cinema_id = sc.cinema_id
         WHERE {' AND '.join(clauses)}
         ORDER BY b.booking_id DESC
         LIMIT %s""", params + [limit])
    return ok({"count": len(rows), "bookings": rows})


@app.get("/api/bookings/<int:booking_id>")
def bookings_get(booking_id):
    with db.transaction() as cur:
        return ok({"booking": booking_detail(cur, booking_id)})


@app.get("/api/bookings/<int:booking_id>/refund-preview")
def bookings_refund_preview(booking_id):
    row = db.query_one("""
        SELECT b.booking_id, b.status, s.start_time,
               ROUND(EXTRACT(EPOCH FROM (s.start_time - fn_now())) / 3600.0, 1) AS hours_to_show,
               fn_refund_percent(b.booking_id, fn_now()) AS percent,
               COALESCE((SELECT sum(p.amount) FROM payment p
                          WHERE p.booking_id = b.booking_id AND p.status <> 'FAILED'), 0) AS paid,
               fn_now() AS now
          FROM booking b JOIN screening s ON s.screening_id = b.screening_id
         WHERE b.booking_id = %s""", (booking_id,))
    if row is None:
        raise ApiError(f"Booking {booking_id} does not exist.", 404)
    policies = db.query("""
        SELECT min_hours_before, refund_percent FROM cancellation_policy
         ORDER BY min_hours_before DESC""")
    paid = float(row["paid"])
    refund = round(paid * int(row["percent"]) / 100.0, 2)
    can_cancel = row["status"] == "CONFIRMED" and row["start_time"] > row["now"]
    return ok({
        "booking_id": booking_id,
        "status": row["status"],
        "start_time": row["start_time"],
        "hours_to_show": float(row["hours_to_show"]) if row["hours_to_show"] is not None else None,
        "percent": int(row["percent"]),
        "paid": paid,
        "refund_amount": refund if can_cancel else 0.0,
        "can_cancel": can_cancel,
        "policies": policies,
        "explain": ("Refund tier picked by fn_refund_percent(): the policy row with the largest "
                    "min_hours_before that is still <= the hours until the show."),
    })


@app.post("/api/bookings/<int:booking_id>/cancel")
def bookings_cancel(booking_id):
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "Cancelled by customer").strip()[:200]
    cancelled_by = need_choice({**{"cancelled_by": data.get("cancelled_by") or "CUSTOMER"}},
                               "cancelled_by", ["CUSTOMER", "STAFF", "SYSTEM"])
    with db.transaction() as cur:
        cur.execute("""SELECT b.booking_id, b.status, b.screening_id, s.start_time
                         FROM booking b JOIN screening s ON s.screening_id = b.screening_id
                        WHERE b.booking_id = %s FOR UPDATE OF b""", (booking_id,))
        booking = cur.fetchone()
        if booking is None:
            raise ApiError(f"Booking {booking_id} does not exist.", 404)
        if booking["status"] != "CONFIRMED":
            raise ApiError(f"Booking {booking_id} is already {booking['status']}; a booking can "
                           "only be cancelled once (UNIQUE cancellation.booking_id).", 409)

        cur.execute("""SELECT fn_now() AS now, fn_refund_percent(%s, fn_now()) AS percent""",
                    (booking_id,))
        clock = cur.fetchone()
        cur.execute("""SELECT COALESCE(sum(amount), 0) AS paid,
                              (array_agg(payment_id ORDER BY payment_id))[1] AS payment_id
                         FROM payment WHERE booking_id = %s AND status <> 'FAILED'""",
                    (booking_id,))
        money = cur.fetchone()
        if money["payment_id"] is None:
            raise ApiError(f"Booking {booking_id} has no successful payment to refund.", 409)

        cur.execute("""INSERT INTO cancellation (booking_id, cancelled_at, cancelled_by, reason)
                       VALUES (%s, %s, %s, %s) RETURNING cancellation_id""",
                    (booking_id, clock["now"], cancelled_by, reason))
        cancellation_id = cur.fetchone()["cancellation_id"]

        percent = int(clock["percent"])
        refund_amount = round(float(money["paid"]) * percent / 100.0, 2)
        refund_id = None
        if refund_amount > 0:
            cur.execute("""INSERT INTO refund (cancellation_id, payment_id, refund_amount,
                                               refund_status, refunded_at)
                           VALUES (%s, %s, %s, 'COMPLETED', %s) RETURNING refund_id""",
                        (cancellation_id, money["payment_id"], refund_amount, clock["now"]))
            refund_id = cur.fetchone()["refund_id"]
            cur.execute("""UPDATE payment SET status = 'REFUNDED'
                            WHERE booking_id = %s AND status = 'SUCCESS'""", (booking_id,))
        detail = booking_detail(cur, booking_id)

    if refund_amount > 0:
        note = (f"Booking {booking_id} cancelled. Refund INR {refund_amount:.2f} "
                f"({percent}% of INR {float(money['paid']):.2f}) issued - seats released, "
                "tickets voided.")
    else:
        note = (f"Booking {booking_id} cancelled. The show was under 2 hours away, so the "
                "policy allows a 0% refund - seats released and tickets voided anyway.")
    return ok({"message": note, "refund_amount": refund_amount, "percent": percent,
               "cancellation_id": cancellation_id, "refund_id": refund_id,
               "booking": detail})


# ============================================================================
#  Food & drink orders
# ============================================================================


def order_detail(cur, order_id):
    cur.execute("""
        SELECT o.order_id, o.booking_id, o.staff_id, o.order_time, o.status,
               st.full_name AS staff_name,
               (SELECT c.full_name FROM booking b JOIN customer c
                 ON c.customer_id = b.customer_id WHERE b.booking_id = o.booking_id) AS customer_name,
               (SELECT COALESCE(sum(oi.quantity * oi.unit_price), 0) FROM order_item oi
                 WHERE oi.order_id = o.order_id) AS total
          FROM concession_order o LEFT JOIN staff st ON st.staff_id = o.staff_id
         WHERE o.order_id = %s""", (order_id,))
    order = cur.fetchone()
    if order is None:
        raise ApiError(f"Food order {order_id} does not exist.", 404)
    cur.execute("""
        SELECT oi.item_id, i.item_name, i.category, oi.quantity, oi.unit_price,
               (oi.quantity * oi.unit_price) AS line_total
          FROM order_item oi JOIN concession_item i ON i.item_id = oi.item_id
         WHERE oi.order_id = %s ORDER BY i.item_name""", (order_id,))
    order["items"] = cur.fetchall()
    cur.execute("""SELECT payment_id, amount, method, status, paid_at, transaction_ref
                     FROM payment WHERE order_id = %s ORDER BY payment_id""", (order_id,))
    order["payments"] = cur.fetchall()
    return order


@app.post("/api/orders")
def orders_create():
    data = body()
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ApiError("'items' must be a non-empty list of {item_id, quantity}.", 400)
    if len(raw_items) > 20:
        raise ApiError("A single order can hold at most 20 different items.", 400)
    method = need_choice(data, "method", db.PAYMENT_METHODS)
    booking_id = None
    if data.get("booking_id") not in (None, ""):
        try:
            booking_id = int(data["booking_id"])
        except (TypeError, ValueError):
            raise ApiError("'booking_id' must be a whole number.", 400)
    staff_id = None
    if data.get("staff_id") not in (None, ""):
        try:
            staff_id = int(data["staff_id"])
        except (TypeError, ValueError):
            raise ApiError("'staff_id' must be a whole number.", 400)

    lines, seen = [], set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ApiError("Every order line must be an object {item_id, quantity}.", 400)
        item_id = need_int(entry, "item_id", minimum=1)
        quantity = need_int(entry, "quantity", minimum=1, maximum=20,
                            label="quantity (1-20 per item)")
        if item_id in seen:
            raise ApiError(f"Item {item_id} appears twice in the order.", 400)
        seen.add(item_id)
        lines.append((item_id, quantity))

    with db.transaction() as cur:
        cur.execute("""SELECT item_id, item_name, unit_price, stock_qty
                         FROM concession_item WHERE item_id = ANY(%s)""",
                    ([item_id for item_id, _ in lines],))
        catalogue = {row["item_id"]: row for row in cur.fetchall()}
        for item_id, quantity in lines:
            if item_id not in catalogue:
                raise ApiError(f"Menu item {item_id} does not exist.", 400)
            available = catalogue[item_id]["stock_qty"]
            if quantity > available:
                raise ApiError(f"Only {available} left of '{catalogue[item_id]['item_name']}' "
                               f"in stock - you asked for {quantity}.", 409)

        if booking_id is not None:
            cur.execute("SELECT booking_id FROM booking WHERE booking_id = %s", (booking_id,))
            if cur.fetchone() is None:
                raise ApiError(f"Booking {booking_id} does not exist.", 400)
        if staff_id is not None:
            cur.execute("SELECT staff_id FROM staff WHERE staff_id = %s", (staff_id,))
            if cur.fetchone() is None:
                raise ApiError(f"Staff {staff_id} does not exist.", 400)

        cur.execute("""INSERT INTO concession_order (booking_id, staff_id)
                       VALUES (%s, %s) RETURNING order_id""", (booking_id, staff_id))
        order_id = cur.fetchone()["order_id"]

        from psycopg2.extras import execute_values
        # unit_price is left out on purpose: trigger T5 copies it from the item
        # master and decrements the stock in the same statement.
        execute_values(cur,
                       "INSERT INTO order_item (order_id, item_id, quantity) VALUES %s",
                       [(order_id, item_id, quantity) for item_id, quantity in lines])

        cur.execute("SELECT COALESCE(sum(quantity * unit_price), 0) AS total "
                    "FROM order_item WHERE order_id = %s", (order_id,))
        amount = cur.fetchone()["total"]
        reference = f"TXN-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(2).upper()}"
        cur.execute("""INSERT INTO payment (order_id, amount, method, transaction_ref)
                       VALUES (%s, %s, %s, %s)""",
                    (order_id, amount, method, reference))
        detail = order_detail(cur, order_id)

    return ok({"message": f"Order #{order_id} placed - {len(lines)} item line(s), "
                          f"INR {float(amount):.2f} paid by {method}. Stock was reduced by "
                          "trigger trg_order_item_stock.",
               "order": detail}, 201)


@app.get("/api/orders")
def orders_list():
    clauses, params = ["1 = 1"], []
    booking_id = smart("booking_id")
    if booking_id:
        clauses.append("o.booking_id = %s")
        params.append(booking_id)
    if request.args.get("q"):
        clauses.append("""EXISTS (SELECT 1 FROM order_item soi JOIN concession_item si
                                     ON si.item_id = soi.item_id
                                    WHERE soi.order_id = o.order_id
                                      AND (si.item_name ILIKE %s OR si.category ILIKE %s))""")
        params += [f"%{request.args['q']}%"] * 2
    limit = smart("limit", minimum=1, maximum=200) or 25
    rows = db.query(f"""
        SELECT o.order_id, o.booking_id, o.order_time, o.status, o.staff_id,
               st.full_name AS staff_name,
               (SELECT c.full_name FROM booking b JOIN customer c
                 ON c.customer_id = b.customer_id WHERE b.booking_id = o.booking_id) AS customer_name,
               (SELECT COALESCE(sum(oi.quantity * oi.unit_price), 0) FROM order_item oi
                 WHERE oi.order_id = o.order_id) AS total,
               (SELECT string_agg(i.item_name || ' x' || oi.quantity, ', ' ORDER BY i.item_name)
                  FROM order_item oi JOIN concession_item i ON i.item_id = oi.item_id
                 WHERE oi.order_id = o.order_id) AS summary,
               (SELECT count(*) FROM order_item oi WHERE oi.order_id = o.order_id) AS lines
          FROM concession_order o
          LEFT JOIN staff st ON st.staff_id = o.staff_id
         WHERE {' AND '.join(clauses)}
         ORDER BY o.order_id DESC
         LIMIT %s""", params + [limit])
    return ok({"count": len(rows), "orders": rows})


# ============================================================================
#  Named SQL queries  (the only SQL the reports page can run)
# ============================================================================

QUERIES = OrderedDict()

QUERIES["show_occupancy"] = dict(
    title="Show occupancy", kind="VIEW",
    description="v_show_occupancy - every show with seats sold, seats left and occupancy %.",
    sql="""SELECT screening_id, movie, language, cinema_name, screen_name, screen_type,
       start_time, status, base_price, total_seats, booked_seats, seats_left, occupancy_pct
  FROM v_show_occupancy
 ORDER BY start_time DESC
 LIMIT 40""")

QUERIES["movie_performance"] = dict(
    title="Movie performance", kind="VIEW",
    description="v_movie_performance - screenings, tickets and ticket revenue per movie.",
    sql="""SELECT movie_id, title, language, certificate, screenings, tickets_sold,
       ticket_revenue, avg_occupancy_pct
  FROM v_movie_performance
 ORDER BY ticket_revenue DESC""")

QUERIES["screen_utilization"] = dict(
    title="Screen utilization", kind="VIEW",
    description="v_screen_utilization - how full each auditorium is across all of its shows.",
    sql="""SELECT cinema_name, screen_name, screen_type, seats, screenings, seats_sold,
       utilization_pct
  FROM v_screen_utilization
 ORDER BY utilization_pct DESC NULLS LAST""")

QUERIES["concession_sales"] = dict(
    title="Concession sales", kind="VIEW",
    description="v_concession_sales - units sold, revenue and orders per menu item.",
    sql="""SELECT item_name, category, unit_price, stock_qty, units_sold, revenue, orders
  FROM v_concession_sales
 ORDER BY revenue DESC""")

QUERIES["cancellation_summary"] = dict(
    title="Cancellations & refunds", kind="VIEW",
    description="v_cancellation_summary - each cancellation with its refund tier and amount.",
    sql="""SELECT cancellation_id, booking_id, customer, movie, show_time, cancelled_at,
       cancelled_by, reason, amount_paid, refund_amount, refund_pct, refund_status
  FROM v_cancellation_summary
 ORDER BY cancelled_at DESC
 LIMIT 40""")

QUERIES["daily_revenue"] = dict(
    title="Daily revenue", kind="VIEW",
    description="v_daily_revenue - tickets minus refunds plus concessions, per day.",
    sql="""SELECT day, ticket_revenue, concession_revenue, refunds, net_revenue
  FROM v_daily_revenue
 ORDER BY day DESC
 LIMIT 30""")

QUERIES["bookings_detail"] = dict(
    title="Booking detail (INNER JOIN)", kind="INNER JOIN",
    description="Six tables joined: who booked which seat of which show in which cinema.",
    sql="""SELECT b.booking_id, cu.full_name AS customer, m.title AS movie,
       ci.cinema_name, sc.screen_name, s.start_time, b.status,
       count(t.ticket_id) AS tickets, sum(t.price) AS amount
  FROM booking b
  JOIN customer cu ON cu.customer_id = b.customer_id
  JOIN screening s ON s.screening_id = b.screening_id
  JOIN movie    m  ON m.movie_id      = s.movie_id
  JOIN screen   sc ON sc.screen_id    = s.screen_id
  JOIN cinema   ci ON ci.cinema_id    = sc.cinema_id
  JOIN booked_seat bs ON bs.booking_id  = b.booking_id
  JOIN ticket      t  ON t.booked_seat_id = bs.booked_seat_id
 GROUP BY b.booking_id, cu.full_name, m.title, ci.cinema_name, sc.screen_name,
          s.start_time, b.status
 ORDER BY b.booking_id DESC
 LIMIT 25""")

QUERIES["movies_no_shows"] = dict(
    title="Not showing next 7 days (LEFT JOIN anti-join)", kind="LEFT JOIN",
    description="Movies with no upcoming show in the next week - LEFT JOIN ... IS NULL.",
    sql="""SELECT m.movie_id, m.title, m.language, m.certificate, m.duration_min
  FROM movie m
  LEFT JOIN screening s
         ON s.movie_id = m.movie_id
        AND s.status <> 'CANCELLED'
        AND s.start_time BETWEEN fn_now() AND fn_now() + interval '7 days'
 WHERE s.screening_id IS NULL
 ORDER BY m.title""")

QUERIES["top_customers"] = dict(
    title="Top customers (nested subqueries)", kind="NESTED SUBQUERY",
    description="Customers who spent more than the average customer - derived table inside WHERE.",
    sql="""SELECT full_name,
       (SELECT count(*) FROM booking b WHERE b.customer_id = s.customer_id) AS bookings,
       spend
  FROM (SELECT cu.customer_id, cu.full_name, sum(p.amount) AS spend
          FROM customer cu
          JOIN booking b ON b.customer_id = cu.customer_id
          JOIN payment p ON p.booking_id = b.booking_id AND p.status <> 'FAILED'
         GROUP BY cu.customer_id, cu.full_name) s
 WHERE spend > (SELECT avg(total)
                  FROM (SELECT sum(p.amount) AS total
                          FROM booking b
                          JOIN payment p ON p.booking_id = b.booking_id
                           AND p.status <> 'FAILED'
                         GROUP BY b.customer_id) totals)
 ORDER BY spend DESC
 LIMIT 15""")

QUERIES["busiest_screens"] = dict(
    title="Busiest screens (derived tables)", kind="NESTED SUBQUERY",
    description="Occupancy per screen computed from two derived tables joined to the master data.",
    sql="""SELECT ci.cinema_name, sc.screen_name, sc.screen_type, agg.shows, agg.sold,
       cap.seats,
       ROUND(100.0 * agg.sold / NULLIF(agg.shows * cap.seats, 0), 1) AS occupancy_pct
  FROM screen sc
  JOIN cinema ci ON ci.cinema_id = sc.cinema_id
  JOIN (SELECT s.screen_id,
               count(DISTINCT s.screening_id) AS shows,
               count(bs.booked_seat_id)       AS sold
          FROM screening s
          LEFT JOIN booked_seat bs ON bs.screening_id = s.screening_id
                                  AND bs.status = 'BOOKED'
         GROUP BY s.screen_id) agg ON agg.screen_id = sc.screen_id
  JOIN (SELECT screen_id, count(*) AS seats FROM seat GROUP BY screen_id) cap
       ON cap.screen_id = sc.screen_id
 ORDER BY occupancy_pct DESC""")

QUERIES["seat_type_revenue"] = dict(
    title="Revenue by seat type", kind="GROUP BY",
    description="STANDARD 1.0x, PREMIUM 1.3x, RECLINER 1.6x - what each class earns.",
    sql="""SELECT st.seat_type,
       count(t.ticket_id) AS tickets_sold,
       ROUND(avg(t.price), 2) AS average_price,
       sum(t.price) AS revenue,
       ROUND(100.0 * sum(t.price)
             / (SELECT sum(price) FROM ticket WHERE status <> 'VOID'), 1) AS share_of_tickets
  FROM ticket t
  JOIN booked_seat bs ON bs.booked_seat_id = t.booked_seat_id
  JOIN seat st ON st.seat_id = bs.seat_id
 WHERE t.status <> 'VOID'
 GROUP BY st.seat_type
 ORDER BY revenue DESC""")

QUERIES["customers_above_average"] = dict(
    title="Customers above average spend (correlated subquery)", kind="CORRELATED",
    description="The subquery in WHERE is evaluated once per customer row.",
    sql="""SELECT cu.customer_id, cu.full_name, cu.email,
       (SELECT count(*) FROM booking b WHERE b.customer_id = cu.customer_id) AS bookings,
       (SELECT COALESCE(sum(p.amount), 0)
          FROM booking b
          JOIN payment p ON p.booking_id = b.booking_id AND p.status <> 'FAILED'
         WHERE b.customer_id = cu.customer_id) AS spend
  FROM customer cu
 WHERE (SELECT COALESCE(sum(p.amount), 0)
          FROM booking b
          JOIN payment p ON p.booking_id = b.booking_id AND p.status <> 'FAILED'
         WHERE b.customer_id = cu.customer_id)
       > (SELECT avg(total)
            FROM (SELECT COALESCE(sum(p.amount), 0) AS total
                    FROM booking b
                    JOIN payment p ON p.booking_id = b.booking_id
                     AND p.status <> 'FAILED'
                   GROUP BY b.customer_id) t)
 ORDER BY spend DESC""")

QUERIES["payment_mix"] = dict(
    title="Payment mix", kind="GROUP BY",
    description="How customers pay, split between tickets and food.",
    sql="""SELECT method,
       count(*) AS payments,
       sum(amount) AS amount,
       ROUND(100.0 * count(*) / (SELECT count(*) FROM payment), 1) AS share_pct,
       sum(CASE WHEN booking_id IS NOT NULL THEN amount ELSE 0 END) AS for_tickets,
       sum(CASE WHEN order_id   IS NOT NULL THEN amount ELSE 0 END) AS for_food
  FROM payment
 GROUP BY method
 ORDER BY amount DESC""")

QUERIES["shows_per_day"] = dict(
    title="Shows per day (GROUP BY ... HAVING)", kind="GROUP BY / HAVING",
    description="Only days with 20 or more shows - HAVING filters the groups, not the rows.",
    sql="""SELECT start_time::date AS day,
       count(*) AS shows,
       count(DISTINCT screen_id) AS screens_used,
       count(*) FILTER (WHERE status = 'COMPLETED') AS completed,
       count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled,
       count(*) FILTER (WHERE status = 'SCHEDULED') AS upcoming
  FROM screening
 GROUP BY start_time::date
HAVING count(*) >= 20
 ORDER BY day DESC""")

QUERIES["occupancy_by_format"] = dict(
    title="Format comparison", kind="GROUP BY",
    description="IMAX / 4DX / 3D / 2D: shows, seats sold, occupancy and revenue per format.",
    sql="""SELECT sc.screen_type,
       count(*) AS shows,
       sum(x.capacity) AS seat_slots,
       sum(x.sold) AS seats_sold,
       ROUND(100.0 * sum(x.sold) / NULLIF(sum(x.capacity), 0), 1) AS occupancy_pct,
       sum(x.revenue) AS ticket_revenue
  FROM (SELECT s.screening_id, s.screen_id,
               (SELECT count(*) FROM seat st WHERE st.screen_id = s.screen_id) AS capacity,
               (SELECT count(*) FROM booked_seat bs
                 WHERE bs.screening_id = s.screening_id AND bs.status = 'BOOKED') AS sold,
               (SELECT COALESCE(sum(t.price), 0) FROM ticket t
                  JOIN booked_seat bs ON bs.booked_seat_id = t.booked_seat_id
                 WHERE bs.screening_id = s.screening_id AND t.status <> 'VOID') AS revenue
          FROM screening s) x
  JOIN screen sc ON sc.screen_id = x.screen_id
 GROUP BY sc.screen_type
 ORDER BY ticket_revenue DESC""")

QUERIES["audit_overlapping_shows"] = dict(
    title="Audit: overlapping shows", kind="INTEGRITY AUDIT",
    description="Cross-joins screening with itself. Must return 0 rows - the EXCLUDE constraint.",
    sql="""SELECT a.screening_id AS show_a, b.screening_id AS show_b, a.screen_id,
       a.start_time AS a_from, a.end_time AS a_to,
       b.start_time AS b_from, b.end_time AS b_to
  FROM screening a
  JOIN screening b
    ON a.screen_id = b.screen_id
   AND a.screening_id < b.screening_id
   AND tsrange(a.start_time, a.end_time) && tsrange(b.start_time, b.end_time)
 WHERE a.status <> 'CANCELLED'
   AND b.status <> 'CANCELLED'""")

QUERIES["audit_double_booked_seat"] = dict(
    title="Audit: one seat held twice", kind="INTEGRITY AUDIT",
    description="Must return 0 rows - the partial unique index ux_active_seat.",
    sql="""SELECT bs.screening_id, bs.seat_id, count(*) AS active_holds,
       string_agg(bs.booked_seat_id::text, ', ' ORDER BY bs.booked_seat_id) AS booked_seat_ids
  FROM booked_seat bs
 WHERE bs.status = 'BOOKED'
 GROUP BY bs.screening_id, bs.seat_id
HAVING count(*) > 1
 ORDER BY active_holds DESC""")


def run_named_query(key: str) -> dict:
    entry = QUERIES.get(key)
    if entry is None:
        raise ApiError(f"'{key}' is not one of the {len(QUERIES)} named queries. "
                       f"Try one of: {', '.join(QUERIES)}", 404)
    started = time.perf_counter()
    with db.transaction(source=f"report:{key}", readonly=True) as cur:
        cur.execute("SET LOCAL statement_timeout = 8000")
        cur.execute(entry["sql"])
        columns = [d.name for d in (cur.description or [])]
        rows = [list(row) for row in cur.fetchall()]
    return {
        "key": key, "title": entry["title"], "kind": entry["kind"],
        "description": entry["description"], "sql": entry["sql"].strip(),
        "columns": columns, "rows": db.jsonable_rows(rows), "count": len(rows),
        "ms": round((time.perf_counter() - started) * 1000, 1),
    }


@app.get("/api/queries")
def queries_list():
    return ok({"count": len(QUERIES), "queries": [
        {"key": key, "title": value["title"], "kind": value["kind"],
         "description": value["description"], "sql": value["sql"].strip()}
        for key, value in QUERIES.items()]})


@app.get("/api/reports")
def reports_list():
    return queries_list()


@app.get("/api/reports/<key>")
def reports_run(key):
    return ok(run_named_query(key))


# ============================================================================
#  Dashboard
# ============================================================================


@app.get("/api/dashboard")
def api_dashboard():
    money = db.query_one("""
        SELECT COALESCE(sum(ticket_revenue), 0)     AS ticket_revenue,
               COALESCE(sum(concession_revenue), 0) AS concession_revenue,
               COALESCE(sum(refunds), 0)            AS refunds,
               COALESCE(sum(net_revenue), 0)        AS net_revenue
          FROM v_daily_revenue
         WHERE day >= fn_now()::date - 30""")
    health = db.query_one("""
        SELECT (SELECT count(*) FROM customer) AS customers,
               (SELECT count(*) FROM ticket WHERE status IN ('ISSUED','USED')) AS tickets,
               (SELECT count(*) FROM booking WHERE status = 'CONFIRMED') AS active_bookings,
               (SELECT count(*) FROM booking WHERE status = 'CANCELLED') AS cancelled_bookings,
               (SELECT count(*) FROM screening WHERE status = 'SCHEDULED'
                 AND start_time > fn_now()) AS upcoming_shows,
               (SELECT count(*) FROM screening) AS total_shows,
               (SELECT count(*) FROM movie) AS movies,
               (SELECT count(*) FROM screen) AS screens,
               (SELECT count(*) FROM seat) AS seats,
               (SELECT count(*) FROM concession_order WHERE status = 'PLACED') AS food_orders,
               (SELECT count(*) FROM staff) AS staff,
               (SELECT COALESCE(sum(oi.quantity), 0) FROM order_item oi
                 JOIN concession_order o ON o.order_id = oi.order_id
                WHERE o.status = 'PLACED') AS items_sold,
               (SELECT count(*) FROM concession_item WHERE stock_qty <= 20) AS low_stock,
               (SELECT COALESCE(sum(amount), 0) FROM payment WHERE status = 'SUCCESS') AS collected""")
    occupancy = db.query_one("""
        SELECT ROUND(avg(occupancy_pct), 1) AS avg_occupancy,
               ROUND(avg(occupancy_pct) FILTER (WHERE start_time > fn_now()), 1) AS avg_future,
               count(*) AS shows
          FROM v_show_occupancy
         WHERE start_time::date >= fn_now()::date - 30""")
    trend = db.query("""
        SELECT day, ticket_revenue, concession_revenue, refunds, net_revenue
          FROM v_daily_revenue
         WHERE day <= fn_now()::date
         ORDER BY day DESC
         LIMIT 14""")
    next_shows = db.query("""
        SELECT screening_id, movie, cinema_name, screen_name, screen_type, start_time,
               seats_left, occupancy_pct, base_price
          FROM v_show_occupancy
         WHERE start_time > fn_now() AND status = 'SCHEDULED'
         ORDER BY start_time
         LIMIT 6""")
    top_movies = db.query("""
        SELECT title, language, tickets_sold, ticket_revenue FROM v_movie_performance
         ORDER BY ticket_revenue DESC LIMIT 5""")
    payment_mix = db.query("""
        SELECT method, count(*) AS payments, sum(amount) AS amount,
               ROUND(100.0 * count(*) / (SELECT count(*) FROM payment), 1) AS share_pct
          FROM payment GROUP BY method ORDER BY amount DESC""")
    formats = db.query("""
        SELECT sc.screen_type, count(*) AS shows,
               ROUND(avg(o.occupancy_pct), 1) AS occupancy_pct
          FROM screening s JOIN screen sc ON sc.screen_id = s.screen_id
          JOIN v_show_occupancy o ON o.screening_id = s.screening_id
         WHERE s.start_time > fn_now() - interval '7 days'
         GROUP BY sc.screen_type ORDER BY occupancy_pct DESC""")
    return ok({
        "kpis": {
            "net_revenue": money["net_revenue"], "ticket_revenue": money["ticket_revenue"],
            "concession_revenue": money["concession_revenue"], "refunds": money["refunds"],
            "avg_occupancy_pct": occupancy["avg_occupancy"],
            "avg_occupancy_upcoming": occupancy["avg_future"],
            "shows_last_30_days": occupancy["shows"],
            "cancellations": health["cancelled_bookings"], **health,
        },
        "trend": trend, "next_shows": next_shows, "top_movies": top_movies,
        "payment_mix": payment_mix, "formats": formats,
        "rows": sum(counts_cached().values()),
        "tables": len(db.TABLES),
        "cinema_now": db.scalar("SELECT fn_now() AS n"),
    })


# ============================================================================
#  Schema introspection
# ============================================================================


COLUMNS_SQL = """
SELECT a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default_value,
       col_description(a.attrelid, a.attnum) AS comment,
       a.attnum AS position
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum
"""

CONSTRAINTS_SQL = """
SELECT con.conname AS name, con.contype AS kind,
       pg_get_constraintdef(con.oid) AS definition,
       (SELECT array_agg(att.attname ORDER BY u.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = u.attnum)
                                                              AS columns,
       CASE WHEN con.contype = 'f' THEN con.confrelid::regclass::text END AS ref_table,
       CASE WHEN con.contype = 'f' THEN
         (SELECT array_agg(att.attname ORDER BY u.ord)
            FROM unnest(con.confkey) WITH ORDINALITY AS u(attnum, ord)
            JOIN pg_attribute att ON att.attrelid = con.confrelid AND att.attnum = u.attnum)
       END AS ref_columns,
       CASE con.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
            WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END
                                                              AS on_delete
  FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relname = %s
 ORDER BY con.contype, con.conname
"""


def table_meta(table: str) -> dict:
    columns = db.query(COLUMNS_SQL, (db.SCHEMA, table))
    constraints = db.query(CONSTRAINTS_SQL, (db.SCHEMA, table))
    indexes = db.query("""
        SELECT indexname AS name, indexdef AS definition,
               (indexdef LIKE 'CREATE UNIQUE%%') AS is_unique
          FROM pg_indexes WHERE schemaname = %s AND tablename = %s ORDER BY indexname""",
                       (db.SCHEMA, table))
    comment = db.scalar("""
        SELECT obj_description(c.oid) FROM pg_class c JOIN pg_namespace n
          ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s""",
                        (db.SCHEMA, table))

    primary_key = next((row["columns"] for row in constraints if row["kind"] == "p"), [])
    uniques = [{"name": row["name"], "columns": row["columns"]}
               for row in constraints if row["kind"] == "u"]
    checks = [{"name": row["name"], "definition": row["definition"]}
              for row in constraints if row["kind"] == "c"]
    fks = [{"name": row["name"], "columns": row["columns"], "ref_table": row["ref_table"],
            "ref_columns": row["ref_columns"], "on_delete": row["on_delete"],
            "definition": row["definition"]}
           for row in constraints if row["kind"] == "f"]
    excludes = [{"name": row["name"], "definition": row["definition"]}
                for row in constraints if row["kind"] == "x"]

    fk_columns = {}
    for fk in fks:
        for index, column in enumerate(fk["columns"] or []):
            fk_columns[column] = {
                "table": (fk["ref_table"] or "").split(".")[-1],
                "column": (fk["ref_columns"] or [None])[index],
                "constraint": fk["name"],
            }
    unique_columns = {column for unique in uniques for column in (unique["columns"] or [])}
    for column in columns:
        column["is_pk"] = column["name"] in primary_key
        column["is_unique"] = column["name"] in unique_columns or column["is_pk"]
        column["is_fk"] = column["name"] in fk_columns
        link = fk_columns.get(column["name"])
        column["references"] = f"{link['table']}.{link['column']}" if link else None
        column["check"] = next(
            (check["definition"] for check in checks
             if re.search(rf"\b{re.escape(column['name'])}\b", check["definition"])), None)

    lines = [f"CREATE TABLE {table} ("]
    body = []
    for column in columns:
        piece = f'    {column["name"]:<18} {column["data_type"]}'
        if column["default_value"]:
            piece += f' DEFAULT {column["default_value"]}'
        if column["not_null"]:
            piece += " NOT NULL"
        body.append(piece)
    if primary_key:
        body.append(f'    CONSTRAINT {table}_pkey PRIMARY KEY ({", ".join(primary_key)})')
    for unique in uniques:
        body.append(f'    CONSTRAINT {unique["name"]} UNIQUE ({", ".join(unique["columns"])})')
    for check in checks:
        body.append(f'    CONSTRAINT {check["name"]} {check["definition"]}')
    for exclude in excludes:
        body.append(f'    CONSTRAINT {exclude["name"]} {exclude["definition"]}')
    for fk in fks:
        body.append(f'    CONSTRAINT {fk["name"]} {fk["definition"]}')
    ddl = "\n".join(lines + [",\n".join(body)]) + "\n);"
    for index in indexes:
        if index["name"].endswith("_pkey"):
            continue
        ddl += "\n" + index["definition"].rstrip(";") + ";"

    return {"name": table, "comment": comment, "columns": columns,
            "primary_key": primary_key, "uniques": uniques, "checks": checks,
            "foreign_keys": fks, "excludes": excludes, "indexes": indexes,
            "fk_columns": fk_columns, "ddl": ddl,
            "row_count": db.scalar(f"SELECT count(*) AS n FROM {table}") or 0}


def build_schema() -> dict:
    """Everything the Database page and the Solution page need, live from pg_catalog."""
    tables = [table_meta(table) for table in db.TABLES]
    triggers = db.query("""
        SELECT t.tgname AS name, c.relname AS table_name,
               CASE WHEN (t.tgtype & 2) <> 0 THEN 'BEFORE' ELSE 'AFTER' END AS timing,
               trim(both ' ' from concat_ws(' OR ',
                   CASE WHEN (t.tgtype & 4) <> 0 THEN 'INSERT' END,
                   CASE WHEN (t.tgtype & 8) <> 0 THEN 'DELETE' END,
                   CASE WHEN (t.tgtype & 16) <> 0 THEN 'UPDATE' END)) AS event,
               p.proname AS function_name,
               pg_get_triggerdef(t.oid) AS definition
          FROM pg_trigger t
          JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_proc p ON p.oid = t.tgfoid
         WHERE n.nspname = %s AND NOT t.tgisinternal
         ORDER BY t.tgname""", (db.SCHEMA,))
    for trigger in triggers:
        body = db.scalar("SELECT prosrc FROM pg_proc WHERE proname = %s LIMIT 1",
                         (trigger["function_name"],))
        trigger["function_sql"] = db.scalar(
            "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = %s LIMIT 1",
            (trigger["function_name"],))
        trigger["body"] = body
    views = []
    for view in db.VIEWS:
        definition = db.scalar(
            "SELECT pg_get_viewdef(c.oid, true) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s", (db.SCHEMA, view))
        if definition is None:
            continue
        views.append({"name": view, "sql": f"CREATE OR REPLACE VIEW {view} AS\n"
                                          f"{definition.strip()}",
                      "columns": [row["name"] for row in db.query(
                          "SELECT column_name AS name FROM information_schema.columns "
                          "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                          (db.SCHEMA, view))],
                      "row_count": db.scalar(f"SELECT count(*) AS n FROM {view}")})
    functions = db.query("""
        SELECT p.proname AS name,
               pg_get_function_identity_arguments(p.oid) AS arguments,
               pg_get_function_result(p.oid) AS returns,
               l.lanname AS language,
               d.description AS comment,
               p.prosrc AS body
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_language l ON l.oid = p.prolang
          LEFT JOIN pg_description d ON d.objoid = p.oid
         WHERE n.nspname = %s AND p.proname LIKE 'fn/_%%' ESCAPE '/'
         ORDER BY p.proname""", (db.SCHEMA,))
    for function in functions:
        function["sql"] = db.scalar(
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
            "WHERE p.proname = %s AND pg_get_function_identity_arguments(p.oid) = %s",
            (function["name"], function["arguments"]))
    indexes = db.query("""
        SELECT i.indexname AS name, i.tablename AS table_name, i.indexdef AS definition,
               (i.indexdef LIKE 'CREATE UNIQUE%%') AS is_unique,
               (i.indexdef LIKE '%%WHERE%%') AS is_partial
          FROM pg_indexes i
         WHERE i.schemaname = %s AND i.indexname = ANY(%s)
         ORDER BY i.tablename, i.indexname""", (db.SCHEMA, db.INDEXES))
    counts = counts_cached()
    return {
        "schema": db.SCHEMA, "server": db.ping().get("version"),
        "tables": tables, "triggers": triggers, "views": views,
        "functions": functions, "indexes": indexes,
        "summary": {
            "tables": len(tables), "triggers": len(triggers), "views": len(views),
            "functions": len(functions), "indexes": len(indexes),
            "rows": sum(counts.values()), "counts": counts,
        },
    }


@app.get("/api/schema")
def api_schema():
    return ok(build_schema())


# ============================================================================
#  Database page: tables browser + read-only SQL console
# ============================================================================

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|GRANT|REVOKE|COPY|CREATE|TRUNCATE|MERGE|"
    r"VACUUM|REINDEX|CLUSTER|REFRESH|CALL|DO|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|SET|"
    r"RESET|LOCK|NOTIFY|LISTEN|IMPORT|EXECUTE|PREPARE)\b", re.IGNORECASE)


def strip_sql_noise(sql: str) -> str:
    """Remove comments and string literals so keyword checks cannot be fooled."""
    out, index, size = [], 0, len(sql)
    while index < size:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < size else ""
        if char == "-" and nxt == "-":
            index = sql.find("\n", index)
            if index == -1:
                break
            continue
        if char == "/" and nxt == "*":
            end = sql.find("*/", index + 2)
            index = size if end == -1 else end + 2
            continue
        if char == "'":
            end = index + 1
            while end < size:
                if sql[end] == "'" and (end + 1 >= size or sql[end + 1] != "'"):
                    break
                end += 1
            out.append("''")
            index = end + 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def validate_console_sql(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise ApiError("Type a SELECT or WITH query first.", 400)
    clean = strip_sql_noise(text).strip()
    statements = [part for part in clean.split(";") if part.strip()]
    if len(statements) > 1:
        raise ApiError(f"Only ONE statement is allowed in the console; you sent "
                       f"{len(statements)}. Run them one at a time.", 400)
    if clean.endswith(";"):
        clean = clean[:-1].strip()
    first = (statements[0].strip().split() or [""])[0].upper()
    if first not in ("SELECT", "WITH"):
        raise ApiError(f"The SQL console is READ-ONLY: a statement must start with SELECT or "
                       f"WITH, not {first or 'nothing'}.", 400)
    forbidden = FORBIDDEN.search(clean)
    if forbidden:
        raise ApiError(f"'{forbidden.group(0).upper()}' is not allowed here. The console only "
                       "runs one read-only SELECT/WITH statement.", 400)
    if not clean:
        raise ApiError("The statement is empty after removing the trailing semicolon.", 400)
    return statements[0].strip().rstrip(";")


@app.get("/api/db/tables")
def db_tables():
    counts = counts_cached(0.0)
    tables = [{"name": name, "label": db.TABLE_LABELS.get(name, name),
               "row_count": counts.get(name, 0),
               "column_count": db.scalar(
                   "SELECT count(*) AS n FROM information_schema.columns "
                   "WHERE table_schema = %s AND table_name = %s", (db.SCHEMA, name))}
              for name in db.TABLES]
    return ok({"count": len(tables), "total_rows": sum(counts.values()), "tables": tables})


@app.get("/api/db/objects")
def db_objects():
    payload = build_schema()
    return ok({"views": payload["views"], "triggers": payload["triggers"],
               "functions": payload["functions"], "indexes": payload["indexes"],
               "summary": payload["summary"]})


@app.get("/api/db/tables/<name>")
def db_table_rows(name):
    if name not in db.TABLES:
        raise ApiError(f"'{name}' is not one of the 17 tables of this schema.", 404)
    meta = table_meta(name)
    limit = smart("limit", minimum=1, maximum=100) or 25
    offset = smart("offset", minimum=0) or 0
    search = (request.args.get("q") or "").strip()
    default_sort = meta["primary_key"][0] if meta["primary_key"] else meta["columns"][0]["name"]
    sort = request.args.get("sort") or default_sort
    if sort not in [column["name"] for column in meta["columns"]]:
        raise ApiError(f"'{sort}' is not a column of {name}.", 400)
    direction = "DESC" if (request.args.get("dir") or "").lower() == "desc" else "ASC"

    where, params = "", []
    if search:
        where = " WHERE " + " OR ".join(f"{column['name']}::text ILIKE %s"
                                        for column in meta["columns"])
        params = [f"%{search}%" for _ in meta["columns"]]
    total = db.scalar(f"SELECT count(*) AS n FROM {name}") or 0
    filtered = db.scalar(f"SELECT count(*) AS n FROM {name}{where}", params) or 0
    order = f'ORDER BY "{sort}" {direction} NULLS LAST'
    if meta["primary_key"] and sort not in meta["primary_key"]:
        order += f', {", ".join(meta["primary_key"])}'
    rows = db.query(f'SELECT * FROM {name}{where} {order} LIMIT %s OFFSET %s',
                    params + [limit, offset])
    return ok({
        "table": name, "label": db.TABLE_LABELS.get(name, name),
        "columns": meta["columns"], "primary_key": meta["primary_key"],
        "uniques": meta["uniques"], "foreign_keys": meta["foreign_keys"],
        "fk_columns": meta["fk_columns"], "comment": meta["comment"], "ddl": meta["ddl"],
        "indexes": meta["indexes"],
        "rows": [list(row.values()) for row in rows],
        "row_count": filtered, "total_count": total, "limit": limit, "offset": offset,
        "sort": sort, "dir": direction.lower(), "q": search,
    })


@app.post("/api/db/query")
def db_console():
    data = body()
    statement = validate_console_sql(str(data.get("sql") or ""))
    started = time.perf_counter()
    with db.suppress_logging():
        with db.transaction(source="sql-console", readonly=True) as cur:
            cur.execute("SET LOCAL statement_timeout = 3000")
            cur.execute(statement)
            columns = [d.name for d in (cur.description or [])]
            rows = cur.fetchall() if cur.description else []
    truncated = len(rows) > 200
    rows = rows[:200]
    return ok({
        "statement": statement,
        "columns": columns,
        "rows": [list(row.values()) for row in rows],
        "count": len(rows),
        "truncated": truncated,
        "ms": round((time.perf_counter() - started) * 1000, 1),
        "note": ("Read-only transaction, statement_timeout 3000 ms, maximum 200 rows."
                 if not truncated else
                 "Read-only transaction, 200 of " + str(len(rows)) + "+ rows shown."),
    })


# ============================================================================
#  Source code page
# ============================================================================

SOURCE_FILES = [
    ("supabase/schema.sql", "Database", "sql",
     "The whole database: 17 tables, 12 CHECK rules, 8 indexes, 5 triggers, 6 views, RLS."),
    ("seed.py", "Database", "python",
     "Deterministic demo data (random.seed(65)) loaded with execute_values()."),
    ("setup_db.py", "Database", "python",
     "One-shot setup: applies schema.sql, runs seed.py, verifies objects and prints row counts."),
    ("make_er.py", "Database", "python",
     "Draws static/er.svg by hand: 17 entities, crow's-foot cardinalities, no crossing lines."),
    ("app.py", "Backend", "python",
     "Flask JSON API: CRUD, screenings, bookings, orders, 17 named queries, dashboard, schema."),
    ("db.py", "Backend", "python",
     "Connection pool, explicit BEGIN/COMMIT/ROLLBACK and the 300-entry Live SQL Log."),
    ("run_prod.py", "Backend", "python",
     "Production entry point: waitress with threads, honours PORT."),
    ("requirements.txt", "Backend", "text",
     "Flask, psycopg2-binary, python-dotenv, waitress - nothing else."),
    ("static/index.html", "Front end", "html",
     "Single page shell: header, role switcher, tabs, modals, SQL log drawer."),
    ("static/app.js", "Front end", "javascript",
     "Hash routing, seat map, booking, cancellation preview, dashboard, db browser, SQL console."),
    ("static/style.css", "Front end", "css",
     "Dark navy + cyan design system built only from the :root CSS variables."),
    (".env.example", "Docs", "text",
     "The only environment variable the backend needs: DATABASE_URL."),
    ("README.md", "Docs", "markdown",
     "How to run everything, the schema tour and the viva demo script."),
    ("tests/test_cinema.py", "Tests", "python",
     "unittest suite against a throw-away test schema: rules, rollbacks, refunds, APIs."),
]
SOURCE_MAP = {path: (group, language, description)
              for path, group, language, description in SOURCE_FILES}
SOURCE_PATHS = [path for path, *_ in SOURCE_FILES]


def source_entry(path: str) -> dict:
    group, language, description = SOURCE_MAP[path]
    full = ROOT / path
    text = full.read_text(encoding="utf-8")
    return {"path": path, "group": group, "language": language,
            "description": description, "size": full.stat().st_size,
            "lines": text.count("\n") + 1}


@app.get("/api/source")
def source_list():
    groups = OrderedDict()
    for path in SOURCE_PATHS:
        entry = source_entry(path)
        groups.setdefault(entry["group"], []).append(entry)
    total_lines = sum(entry["lines"] for group in groups.values() for entry in group)
    return ok({"count": len(SOURCE_PATHS), "total_lines": total_lines,
               "groups": [{"group": name, "files": files} for name, files in groups.items()]})


@app.get("/api/source.zip")
def source_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in SOURCE_PATHS:
            archive.write(ROOT / path, arcname=f"galaxy-cinemas/{path}")
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name="galaxy-cinemas-source.zip")


@app.get("/api/source/<path:name>")
def source_file(name):
    candidate = (name or "").replace("\\", "/").lstrip("/")
    if candidate not in SOURCE_MAP or ".." in candidate or candidate.startswith("."):
        raise ApiError(f"'{name}' is not a published source file. Only the "
                       f"{len(SOURCE_PATHS)} project files are downloadable.", 404)
    text = (ROOT / candidate).read_text(encoding="utf-8")
    body_text = text
    functions = []
    if candidate.endswith(".py"):
        for number, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^(?:def|class)\s+(\w+)", line)
            if match:
                functions.append({"name": match.group(1), "line": number})
    elif candidate.endswith(".js"):
        for number, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^(?:async\s+)?function\s+(\w+)", line) or \
                re.match(r"^(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?\(", line)
            if match:
                functions.append({"name": match.group(1), "line": number})
    elif candidate.endswith(".sql"):
        for number, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^CREATE (?:OR REPLACE )?(?:FUNCTION|VIEW|TRIGGER)\s+(\w+)", line)
            if match:
                functions.append({"name": match.group(1), "line": number})
    entry = source_entry(candidate)
    entry.update({"content": body_text, "functions": functions})
    return ok(entry)


# ============================================================================
#  Live SQL log
# ============================================================================


@app.get("/api/log")
def api_log():
    since = request.args.get("since", "0")
    try:
        since = int(since)
    except ValueError:
        since = 0
    entries = db.get_log(since)
    return ok({"entries": db.jsonable_rows(entries), "stats": db.log_stats(),
               "last_id": entries[-1]["id"] if entries else db.log_stats()["last_id"]})


@app.post("/api/log/clear")
def api_log_clear():
    return ok({"message": f"Cleared {db.clear_log()} log entries.", "stats": db.log_stats()})


# ============================================================================
#  Tests tab
# ============================================================================

_test_lock = threading.Lock()
_test_state = {"running": False, "started": None, "last": None}


@app.get("/api/tests")
def tests_state():
    return ok({"running": _test_state["running"], "last": _test_state["last"],
               "command": "python tests/test_cinema.py  (separate test schema)"})


@app.post("/api/tests/run")
def tests_run():
    if not _test_lock.acquire(blocking=False):
        raise ApiError("The test suite is already running - wait for it to finish.", 409)
    try:
        _test_state["running"] = True
        _test_state["started"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        environment = os.environ.copy()
        environment["CINEMA_TEST_SCHEMA"] = environment.get(
            "CINEMA_TEST_SCHEMA", f"cinema_test_api_{os.getpid()}")
        environment.setdefault("CINEMA_SCHEMA", db.SCHEMA)
        if db.CINEMA_NOW:
            environment["CINEMA_NOW"] = db.CINEMA_NOW
        started = time.perf_counter()
        try:
            process = subprocess.run(
                [sys.executable, str(ROOT / "tests" / "test_cinema.py"), "--json"],
                cwd=str(ROOT), env=environment, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            _test_state["last"] = {"ok": False, "error": "timeout"}
            raise ApiError("The test suite did not finish within 60 seconds and was killed.",
                           504)
        duration = round(time.perf_counter() - started, 2)
        payload = None
        for line in reversed((process.stdout or "").strip().splitlines()):
            if line.strip().startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if payload is None:
            _test_state["last"] = {"ok": False, "output": (process.stdout or "")[-4000:]}
            raise ApiError("Could not read the test result. Raw output: "
                           + (process.stdout or process.stderr or "")[-800:], 500)
        payload["duration"] = duration
        payload["exit_code"] = process.returncode
        payload["schema"] = environment["CINEMA_TEST_SCHEMA"]
        payload["command"] = (f"{pathlib.Path(sys.executable).name} tests/test_cinema.py "
                              f"--schema {environment['CINEMA_TEST_SCHEMA']}")
        _test_state["last"] = payload
        return ok(payload)
    finally:
        _test_state["running"] = False
        _test_lock.release()


# ============================================================================
#  Static single page app
# ============================================================================


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/favicon.ico")
def favicon():
    """A tiny inline icon, so the browser never logs a 404 for it."""
    icon = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="7" fill="#050A14"/>'
            '<circle cx="16" cy="16" r="9" fill="none" stroke="#6FD3F2" '
            'stroke-width="2.5"/><circle cx="16" cy="16" r="3" fill="#A5E8FA"/>'
            '<ellipse cx="16" cy="16" rx="14" ry="5" fill="none" stroke="#3D8BFF" '
            'stroke-width="1.6" transform="rotate(-24 16 16)"/></svg>')
    return app.response_class(icon, mimetype="image/svg+xml")


@app.get("/<path:page>")
def spa(page):
    candidate = ROOT / "static" / page
    if candidate.is_file():
        return send_from_directory(app.static_folder, page)
    return send_from_directory(app.static_folder, "index.html")


def main():
    # PORT wins when it is a real port number; an empty/zero PORT (some shells and
    # IDEs export PORT=0 for "any free port") falls back to 5000 so that the plain
    # `python app.py` command always lands on the documented address.
    try:
        port = int(os.environ.get("PORT") or 0) or 5000
    except ValueError:
        port = 5000
    debug = (os.environ.get("FLASK_DEBUG") or "").lower() in ("1", "true", "yes")
    if os.environ.get("CINEMA_NOW"):
        print(f"Fixed clock active: CINEMA_NOW={os.environ['CINEMA_NOW']}")
    print("=" * 74)
    print("  Galaxy Cinemas - Screening & Ticket Booking System")
    print(f"  Flask API + single page app   ->   http://127.0.0.1:{port}")
    print("=" * 74)
    try:
        info = db.ping()
        print(f"  database : {info.get('database')} as {info.get('username')} "
              f"({info.get('server')}), schema '{db.SCHEMA}', "
              f"{info.get('table_count')} tables")
    except Exception as exc:                      # pragma: no cover - startup help
        print(f"  database : NOT REACHABLE - {exc}")
        print("  Run 'python setup_db.py' first (it applies the schema and seeds the data).")
    try:
        app.run(host="127.0.0.1", port=port, debug=debug, threaded=True)
    except OSError as exc:                        # port already taken - pick a free one
        print(f"  port {port} is busy ({exc}); serving on a free port instead")
        app.run(host="127.0.0.1", port=0, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
