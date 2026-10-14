#!/usr/bin/env python3
"""Galaxy Cinemas - production entry point.

    python run_prod.py                 # waitress on 0.0.0.0:5000
    PORT=8080 python run_prod.py       # a different port
    THREADS=8 python run_prod.py       # a bigger thread pool

`python app.py` is the development server (Flask + reloader). This module serves
the exact same WSGI application through waitress, which is production safe and
still a single process, so the in-memory SQL log and the connection pool the
Database page inspects stay consistent.

DATABASE_URL is read from the environment or .env - never from the command line,
so the password never lands in the shell history.
"""

from __future__ import annotations

import os
import sys

import app as application
import db

DEFAULT_PORT = 5000
DEFAULT_THREADS = 4


def main() -> int:
    port = int(os.environ.get("PORT") or DEFAULT_PORT)
    host = os.environ.get("HOST") or "0.0.0.0"
    threads = int(os.environ.get("THREADS") or DEFAULT_THREADS)

    try:
        import waitress
    except ImportError:
        print("waitress is not installed - run: pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    # Fail fast and loudly if the database is unreachable or the schema is missing.
    try:
        tables = db.scalar(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_schema = %s", (db.SCHEMA,))
        rows = sum(db.table_counts().values())
    except Exception as error:                                # pragma: no cover
        print(f"cannot reach PostgreSQL: {error}", file=sys.stderr)
        print("check DATABASE_URL in .env, then run: python setup_db.py", file=sys.stderr)
        return 1

    print("=" * 72)
    print("  Galaxy Cinemas - Screening & Ticket Booking System")
    print(f"  database : Supabase PostgreSQL, schema '{db.SCHEMA}'")
    print(f"  tables   : {tables}   rows: {rows:,}")
    print(f"  clock    : {db.scalar('SELECT fn_now() AS n')}"
          f"{'  (CINEMA_NOW fixed)' if db.CINEMA_NOW else '  (real clock)'}")
    print(f"  serving  : http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    print("=" * 72)

    waitress.serve(application.app, host=host, port=port, threads=threads,
                   ident="Galaxy Cinemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
