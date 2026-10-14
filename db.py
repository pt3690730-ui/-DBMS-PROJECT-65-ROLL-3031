"""Galaxy Cinemas - database access layer.

Everything that talks to Supabase PostgreSQL goes through this module.

*   A thread-safe connection pool (psycopg2), TLS enforced (sslmode=require).
*   Transactions are EXPLICIT: BEGIN / COMMIT / ROLLBACK, never implicit.
*   Every statement the application runs passes through CinemaCursor.execute()
    and is recorded in an in-memory ring buffer (300 entries) that powers the
    "Live SQL Log" drawer: time, route, statement, params, status, duration.
*   Row Level Security is ON in the database with no policies; the backend uses
    the owner role over TLS, and the front end never sees a database credential.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections import deque
from datetime import date, datetime, time as _time
from decimal import Decimal

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv()  # also honour a .env in the current working directory

# ---------------------------------------------------------------- settings ---
SCHEMA = (os.environ.get("CINEMA_SCHEMA") or "public").strip()
CINEMA_NOW = (os.environ.get("CINEMA_NOW") or "").strip() or None
LOG_CAPACITY = 300
POOL_MIN, POOL_MAX = 1, 12

# The 17 real tables of the schema. Every endpoint that accepts a table name
# validates it against this whitelist - no table outside it can ever be read.
TABLES = [
    "cinema", "screen", "seat", "movie", "screening", "customer", "staff",
    "concession_item", "cancellation_policy", "booking", "booked_seat",
    "ticket", "concession_order", "order_item", "payment", "cancellation",
    "refund",
]
TABLE_LABELS = {
    "cinema": "Multiplexes", "screen": "Auditoria", "seat": "Seat inventory",
    "movie": "Movies", "screening": "Shows", "customer": "Customers",
    "staff": "Staff logins", "concession_item": "Food & drink menu",
    "cancellation_policy": "Refund tiers", "booking": "Bookings",
    "booked_seat": "Seats held per booking", "ticket": "Tickets",
    "concession_order": "Food orders", "order_item": "Order lines",
    "payment": "Payments", "cancellation": "Cancellations", "refund": "Refunds",
}

# The 8 indexes of the schema (7 declared + the partial unique index that stops
# double booking).
INDEXES = [
    "ux_active_seat", "idx_movie_title", "idx_screening_movie",
    "idx_screening_start", "idx_booking_customer", "idx_booking_screening",
    "idx_payment_booking", "idx_concession_order_booking",
]

# The 5 triggers of the schema.
TRIGGERS = [
    "trg_booked_seat_validate", "trg_cancellation_validate",
    "trg_cancellation_apply", "trg_refund_validate", "trg_order_item_stock",
]

# The 6 views of the schema.
VIEWS = [
    "v_show_occupancy", "v_movie_performance", "v_screen_utilization",
    "v_concession_sales", "v_cancellation_summary", "v_daily_revenue",
]

# Ticket price = screening.base_price x multiplier (documented business rule).
SEAT_MULTIPLIER = {"STANDARD": 1.0, "PREMIUM": 1.3, "RECLINER": 1.6}
PAYMENT_METHODS = ["CARD", "UPI", "CASH", "WALLET"]


class ConfigError(RuntimeError):
    """Raised when the workspace has not been pointed at a database yet."""


def database_url() -> str:
    """DATABASE_URL from the environment, hardened for a hosted database."""
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and paste the "
            "connection string of your Supabase project (Project settings -> "
            "Database -> Connection string -> URI)."
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    parts = []
    if "sslmode=" not in url:
        parts.append("sslmode=" + os.environ.get("DATABASE_SSLMODE", "require"))
    if "keepalives" not in url:
        parts.append("keepalives=1")
        parts.append("keepalives_idle=30")
    if parts:
        url += ("&" if "?" in url else "?") + "&".join(parts)
    return url


def search_path() -> str:
    entries = [SCHEMA, "public", "extensions"]
    seen, out = set(), []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return ", ".join(out)


# ------------------------------------------------------------- SQL logging ---
_state = threading.local()
_log: deque = deque(maxlen=LOG_CAPACITY)
_log_lock = threading.Lock()
_log_seq = 0


def set_route(route: str | None) -> None:
    """Attribute the statements that are about to run to an HTTP route."""
    _state.route = route


def current_route() -> str | None:
    return getattr(_state, "route", None)


def _suppressed() -> bool:
    return getattr(_state, "suppress", 0) > 0


@contextlib.contextmanager
def suppress_logging():
    """Do not record anything (used by the SQL console and the log endpoint)."""
    _state.suppress = getattr(_state, "suppress", 0) + 1
    try:
        yield
    finally:
        _state.suppress -= 1


def _clean(statement: str) -> str:
    text = " ".join(str(statement).split())
    return text if len(text) <= 4000 else text[:4000] + " ..."


def _params_hint(params) -> str | None:
    if params is None:
        return None
    if isinstance(params, dict):
        text = ", ".join(f"{k}={v!r}" for k, v in list(params.items())[:12])
    else:
        items = list(params)
        if items and isinstance(items[0], (list, tuple)):
            text = f"{len(items)} row(s) x {len(items[0])} value(s)"
        else:
            text = ", ".join(repr(v) for v in items[:12])
    return text[:300]


def log_sql(statement, params=None, status="OK", error=None, ms=None,
            kind=None, failed=False) -> int:
    """Append one entry to the ring buffer and return its id."""
    global _log_seq
    with _log_lock:
        if _suppressed():
            return 0
        _log_seq += 1
        entry_id = _log_seq
        text = _clean(statement)
        upper = text.upper()
        if kind is None:
            for keyword in ("BEGIN", "COMMIT", "ROLLBACK", "INSERT", "UPDATE",
                            "DELETE", "SELECT", "WITH", "SET", "CREATE", "DROP",
                            "EXPLAIN", "SAVEPOINT"):
                if upper.startswith(keyword):
                    kind = keyword
                    break
            else:
                kind = "SQL"
        _log.append({
            "id": entry_id,
            "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "route": current_route(),
            "kind": kind,
            "statement": text,
            "params": _params_hint(params),
            "ms": round(ms, 3) if ms is not None else None,
            "status": status,
            "failed": bool(failed) or status == "ERROR",
            "error": (str(error).strip()[:400] if error else None),
            "sql": text,
        })
        return entry_id


def mark_last(error: str | None = None) -> None:
    """Flag the newest entry as failed (used for ROLLBACK)."""
    with _log_lock:
        if not _log:
            return
        _log[-1]["failed"] = True
        if error:
            _log[-1]["error"] = (str(error).strip()[:400])
        if not _log[-1].get("status"):
            _log[-1]["status"] = "ERROR"


def get_log(since: int = 0, limit: int = LOG_CAPACITY) -> list[dict]:
    with _log_lock:
        entries = [dict(entry) for entry in _log if entry["id"] > int(since or 0)]
    return entries[-limit:]


def clear_log() -> int:
    global _log_seq
    with _log_lock:
        count = len(_log)
        _log.clear()
        _log_seq = 0
    return count


def log_stats() -> dict:
    with _log_lock:
        entries = list(_log)
        last = _log_seq
    return {
        "capacity": LOG_CAPACITY,
        "count": len(entries),
        "last_id": last,
        "errors": sum(1 for e in entries if e["failed"]),
    }


# ---------------------------------------------------------------- cursors ----
class CinemaCursor(psycopg2.extras.RealDictCursor):
    """A real-dict cursor that records every statement in the ring buffer."""

    def execute(self, query, vars=None):
        started = time.perf_counter()
        try:
            result = super().execute(query, vars)
        except BaseException as exc:
            log_sql(query, vars, status="ERROR", error=str(exc),
                    ms=(time.perf_counter() - started) * 1000)
            raise
        log_sql(query, vars, ms=(time.perf_counter() - started) * 1000)
        return result

    def executemany(self, query, vars_list):
        rows = list(vars_list)
        started = time.perf_counter()
        try:
            result = super().executemany(query, rows)
        except BaseException as exc:
            log_sql(query, rows, status="ERROR", error=str(exc),
                    ms=(time.perf_counter() - started) * 1000)
            raise
        log_sql(f"{query}  /* x{len(rows)} rows */", rows,
                ms=(time.perf_counter() - started) * 1000)
        return result


# ------------------------------------------------------------- connections ---
class CinemaConnection(psycopg2.extensions.connection):
    """Applies the session settings (search_path + optional fixed clock) once."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            with self.cursor() as cur:
                cur.execute(
                    "SELECT set_config('search_path', %s, false), "
                    "       set_config('cinema.now', %s, false)",
                    (search_path(), CINEMA_NOW or ""),
                )
            self.commit()
        except Exception:
            self.close()
            raise


_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            dsn = database_url()
            _pool = psycopg2.pool.ThreadedConnectionPool(
                POOL_MIN, POOL_MAX, dsn,
                cursor_factory=CinemaCursor,
                connection_factory=CinemaConnection,
                application_name="galaxy-cinemas-flask",
            )
        return _pool


def get_connection():
    """Borrow a healthy connection from the pool."""
    pool = get_pool()
    try:
        conn = pool.getconn()
    except psycopg2.pool.PoolError:
        raise
    if conn.closed:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    return conn


def release(conn) -> None:
    if conn is None:
        return
    pool = _pool
    if pool is None:
        try:
            conn.close()
        finally:
            return
    try:
        pool.putconn(conn)
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            with contextlib.suppress(Exception):
                _pool.closeall()
            _pool = None


def ping() -> dict:
    """Prove the connection works and report what we are connected to."""
    with suppress_logging():
        row = query_one(
            "SELECT current_database() AS database, current_user AS username, "
            "       version() AS version, current_setting('search_path') AS search_path, "
            "       (SELECT count(*) FROM information_schema.tables "
            "         WHERE table_schema = %s AND table_type = 'BASE TABLE') AS table_count",
            (SCHEMA,),
        )
    row = dict(row or {})
    row["server"] = str(row.get("version", "")).split(" ")[0:2] and \
        " ".join(str(row.get("version", "")).split(" ")[:2])
    row["version"] = " ".join(str(row.get("version", "")).split(" ")[:2])
    return row


# -------------------------------------------------------------- query API ----
def _run(sql, params, fetch, source=None, retries=1):
    route = source or current_route()
    attempt = 0
    while True:
        set_route(route)
        conn = get_connection()
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("BEGIN")
            cur.execute(sql, params)
            out = fetch(cur)
            cur.execute("COMMIT")
            return out
        except psycopg2.OperationalError as exc:
            with contextlib.suppress(Exception):
                cur.execute("ROLLBACK")
            release(conn)
            if attempt >= retries or "closed the connection" not in str(exc):
                raise
            attempt += 1
            continue
        except BaseException:
            with contextlib.suppress(Exception):
                if cur is not None:
                    cur.execute("ROLLBACK")
            mark_last()
            raise
        finally:
            with contextlib.suppress(Exception):
                if cur is not None:
                    cur.close()
            release(conn)


def query(sql, params=None, source=None):
    """Run a SELECT in an explicit READ WRITE transaction, return row dicts."""
    def fetch(cur):
        return [dict(row) for row in cur.fetchall()] if cur.description else []
    return _run(sql, params, fetch, source)


def query_one(sql, params=None, source=None):
    def fetch(cur):
        if not cur.description:
            return None
        row = cur.fetchone()
        return dict(row) if row is not None else None
    return _run(sql, params, fetch, source)


def scalar(sql, params=None, source=None):
    row = query_one(sql, params, source)
    if not row:
        return None
    return next(iter(row.values()))


def execute(sql, params=None, source=None):
    """Run one DML statement in its own explicit transaction."""
    def fetch(cur):
        return cur.rowcount if cur.rowcount is not None else 0
    return _run(sql, params, fetch, source)


def fetch_all(sql, params=None, source=None, limit=None):
    rows = query(sql, params, source)
    return rows[:limit] if limit else rows


@contextlib.contextmanager
def transaction(source=None, readonly=False):
    """Explicit BEGIN / COMMIT / ROLLBACK with every statement logged."""
    route = source or current_route()
    set_route(route)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN READ ONLY" if readonly else "BEGIN")
        yield cur
        cur.execute("COMMIT")
    except BaseException as exc:
        with contextlib.suppress(Exception):
            cur.execute("ROLLBACK")
            mark_last(str(exc))
        raise
    finally:
        with contextlib.suppress(Exception):
            cur.close()
        release(conn)


# ----------------------------------------------------------- json helpers ----
def jsonable(value):
    """Make a psycopg2 value safe for jsonify()."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        # seconds are enough for the UI: "2026-10-14 21:15:00", not microseconds
        return value.replace(microsecond=0).isoformat(sep=" ")
    if isinstance(value, (date, _time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return str(value)


def jsonable_rows(rows):
    """Accept both dict rows and plain tuple rows (named reports use tuples)."""
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append(jsonable(row))
        else:
            try:
                out.append(jsonable(dict(row)))
            except (TypeError, ValueError):
                out.append(jsonable(list(row)))
    return out


def table_counts() -> dict:
    """Exact row count of each of the 17 tables (one cheap COUNT per table)."""
    counts = {}
    for table in TABLES:
        counts[table] = scalar(f"SELECT count(*) AS n FROM {table}") or 0
    return counts


def reset_sequence(table: str, pk: str) -> None:
    """Keep the identity sequence ahead of the rows we inserted explicitly."""
    execute(
        f"SELECT setval(pg_get_serial_sequence(%s, %s), "
        f"       COALESCE((SELECT max({pk}) FROM {table}), 1), true)",
        (table, pk),
    )
