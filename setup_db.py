"""Galaxy Cinemas - one-shot database setup.

    python setup_db.py

1. connects with DATABASE_URL (Supabase PostgreSQL),
2. resets the target schema (only OUR objects are ever dropped),
3. applies supabase/schema.sql   -> 17 tables, 5 triggers, 6 views, 8 indexes, RLS,
4. loads seed.py                 -> the deterministic demo data set,
5. verifies what actually landed in the database and prints the row count of
   every table.

Nothing secret is ever printed: the connection string is masked to
host / port / database / user.
"""

from __future__ import annotations

import os
import pathlib
import sys
from urllib.parse import parse_qsl, urlparse

import psycopg2
import psycopg2.extras

import db
import seed

# Dictionary cursors, so setup can address columns by name.
connect = lambda: psycopg2.connect(  # noqa: E731
    db.database_url(), cursor_factory=psycopg2.extras.RealDictCursor)

ROOT = pathlib.Path(__file__).resolve().parent
SCHEMA_FILE = ROOT / "supabase" / "schema.sql"

# Only these objects are removed by a re-run. They are ours, they live in the
# target schema, and nothing else in the database is ever touched.
DROP_VIEWS = ["v_daily_revenue", "v_cancellation_summary", "v_concession_sales",
              "v_screen_utilization", "v_movie_performance", "v_show_occupancy"]
DROP_FUNCTIONS = ["fn_schedule_screening", "fn_seat_id", "fn_refund_percent",
                  "fn_validate_booked_seat", "fn_validate_cancellation",
                  "fn_apply_cancellation", "fn_validate_refund",
                  "fn_order_item_stock", "fn_now"]


def masked_target() -> str:
    """host / port / database / user - never the password."""
    parts = urlparse(db.database_url())
    query = dict(parse_qsl(parts.query))
    user = (parts.username or "?").replace("%40", "@")
    return (f"{parts.hostname or '?'}:{parts.port or 5432}/{parts.path.lstrip('/')}"
            f"  user={user}  sslmode={query.get('sslmode', 'default')}")


def apply_sql(conn, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)


def reset_target(conn) -> None:
    """Bring the target schema to a known-empty state, without touching others."""
    schema = db.SCHEMA
    with conn.cursor() as cur:
        if schema == "public":
            for view in DROP_VIEWS:
                cur.execute(f"DROP VIEW IF EXISTS {view} CASCADE")
            for table in reversed(db.TABLES):
                cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            for fn in DROP_FUNCTIONS:
                cur.execute(f"DROP FUNCTION IF EXISTS {fn}() CASCADE")
                cur.execute(
                    "SELECT pg_get_function_identity_arguments(p.oid) AS args "
                    "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    " WHERE n.nspname = %s AND p.proname = %s",
                    (schema, fn))
                for row in cur.fetchall():
                    cur.execute(f"DROP FUNCTION IF EXISTS {fn}({row['args']}) CASCADE")
        else:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS btree_gist")


def verify(conn) -> dict:
    """Count what the professor asked to see, straight from the catalog."""
    checks = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              (SELECT count(*) FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE') AS tables,
              (SELECT count(*) FROM pg_trigger t
                 JOIN pg_class c ON c.oid = t.tgrelid
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND NOT t.tgisinternal) AS triggers,
              (SELECT count(*) FROM information_schema.views
                WHERE table_schema = %s) AS views,
              (SELECT count(*) FROM pg_indexes
                WHERE schemaname = %s AND indexname = ANY(%s)) AS indexes,
              (SELECT count(*) FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind = 'r' AND c.relrowsecurity) AS rls_on,
              (SELECT count(*) FROM pg_policies WHERE schemaname = %s) AS policies,
              (SELECT count(*) FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE') AS _again
        """, (db.SCHEMA, db.SCHEMA, db.SCHEMA, db.SCHEMA, db.INDEXES,
              db.SCHEMA, db.SCHEMA, db.SCHEMA))
        row = cur.fetchone()
        checks.update({k: v for k, v in row.items() if not k.startswith("_")})
        cur.execute("""
            SELECT p.proname AS name,
                   pg_get_function_identity_arguments(p.oid) AS args
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = %s AND p.proname LIKE 'fn/_%%' ESCAPE '/'
             ORDER BY p.proname
        """, (db.SCHEMA,))
        checks["functions"] = [f"{r['name']}({r['args']})" for r in cur.fetchall()]
        cur.execute("""
            SELECT c.relname AS name, t.tgname AS trigger_name
              FROM pg_trigger t
              JOIN pg_class c ON c.oid = t.tgrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = %s AND NOT t.tgisinternal
             ORDER BY t.tgname
        """, (db.SCHEMA,))
        checks["trigger_names"] = [f"{r['trigger_name']} on {r['name']}" for r in cur.fetchall()]
        cur.execute("""
            SELECT indexname FROM pg_indexes
             WHERE schemaname = %s ORDER BY indexname
        """, (db.SCHEMA,))
        checks["index_names"] = [r["indexname"] for r in cur.fetchall()]
    return checks


def main() -> int:
    print("=" * 74)
    print("  Galaxy Cinemas - Screening & Ticket Booking System : database setup")
    print("=" * 74)
    print(f"  schema        : {db.SCHEMA}")
    print(f"  fixed clock   : {db.CINEMA_NOW or '(real clock)'}")
    print(f"  target        : {masked_target()}")

    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn = connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS db, current_user AS usr")
            who = cur.fetchone()
        print(f"  connected as  : {who['usr']}@{who['db']}")
    except psycopg2.Error as exc:
        print(f"\nCannot reach the database: {exc}")
        return 2

    print(f"\n[1/4] resetting schema '{db.SCHEMA}' (only Galaxy Cinemas objects)")
    reset_target(conn)
    print("[2/4] applying supabase/schema.sql")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('search_path', %s, false)",
                    (f"{db.SCHEMA}, public, extensions",))
    apply_sql(conn, sql)
    print("      schema applied.")

    print("[3/4] seeding demo data (random.seed(65))")
    conn.close()
    counts = seed.load(verbose=False)

    print("[4/4] verifying")
    conn = connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('search_path', %s, false)",
                    (f"{db.SCHEMA}, public, extensions",))
    checks = verify(conn)
    conn.close()

    print("\n  object summary")
    print("  " + "-" * 44)
    print(f"    tables        : {checks['tables']:>3}   (expected 17)")
    print(f"    triggers      : {checks['triggers']:>3}   (expected  5)")
    print(f"    views         : {checks['views']:>3}   (expected  6)")
    print(f"    indexes       : {checks['indexes']:>3}   (expected  8 declared indexes)")
    print(f"    functions     : {len(checks['functions']):>3}   {', '.join(checks['functions'])}")
    print(f"    RLS enabled   : {checks['rls_on']:>3}/17 tables, policies = {checks['policies']}")
    print("\n    triggers: " + "; ".join(checks["trigger_names"]))
    print("    8 declared indexes: " + ", ".join(db.INDEXES))

    seed.print_counts(counts, title="Row counts of the 17 tables")

    ok = (checks["tables"] == 17 and checks["triggers"] == 5 and checks["views"] == 6
          and checks["indexes"] == 8 and checks["rls_on"] == 17
          and checks["policies"] == 0)
    print("\n  RESULT: " + ("database ready." if ok else "CHECK THE WARNINGS ABOVE."))
    print("  Next:  python app.py     ->  http://127.0.0.1:5000")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except db.ConfigError as error:
        print(f"\n{error}")
        sys.exit(2)
    except psycopg2.Error as error:
        print(f"\nDatabase error: {error}")
        sys.exit(2)
