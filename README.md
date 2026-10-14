# Galaxy Cinemas — Screening & Ticket Booking System

**DBMS Project-Based Learning · Project 65** — *Design and Implementation of a Database
Management System for a Cinema Screening and Ticket Booking System.*

A working multiplex booking website (browse shows → pick seats → pay → get tickets →
cancel with a policy refund → order food) sitting on a real, fully constrained PostgreSQL
schema. Every business rule — double booking, overlapping shows, refund limits, stock —
is enforced **inside the database** by constraints, triggers and views; Flask only
validates input shapes and wraps transactions.

The whole thing is meant to be *shown*: the running app contains its own database, its own
SQL log, its own ER diagram, its own source code and its own test runner.

---

## 1. Quick start

```bash
pip install -r requirements.txt      # Flask, psycopg2-binary, python-dotenv, waitress
cp .env.example .env                 # then paste your Supabase DATABASE_URL
python setup_db.py                   # applies supabase/schema.sql, seeds, prints row counts
python app.py                        # http://127.0.0.1:5000
```

`setup_db.py` is idempotent: it drops and rebuilds everything, then prints the row count of
each of the 17 tables plus the triggers, views and indexes it verified.

Optional:

```bash
python run_prod.py                   # same app through waitress (honours PORT, THREADS)
python tests/test_cinema.py          # the full suite against a throw-away test schema
python make_er.py                    # regenerate static/er.svg
```

### Environment

| Variable | Needed | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | **yes** | Supabase PostgreSQL URI. `sslmode=require` is enforced by the backend. |
| `DATABASE_SSLMODE` | no | Defaults to `require`. |
| `PORT` | no | Defaults to `5000`. |
| `CINEMA_NOW` | no | `YYYY-MM-DD HH:MM` fixed clock, e.g. `2026-06-15 12:00`. Makes every time-dependent rule deterministic during a demo. |
| `CINEMA_SCHEMA` | no | Defaults to `public`. The test suite points the same code at `cinema_test_<pid>`. |

`.env` is git-ignored. No password, key or connection string is ever printed, logged, sent
to the browser or written into the source.

**Row Level Security is enabled on all 17 tables with no policies**, so the anon/public key
cannot read a single row. The Flask backend talks to Postgres directly over TLS as the
owner role. The anon key is never used and never shipped to the front end.

---

## 2. What is in the repository

```
supabase/schema.sql   the entire database: 17 tables, 12 CHECKs, 8 indexes, 5 triggers,
                      3 functions, 6 views, RLS — 757 lines
db.py                 connection pool, explicit BEGIN/COMMIT/ROLLBACK, 300-entry SQL log
seed.py               deterministic demo data (random.seed(65)), batch inserts
setup_db.py           one-shot setup + verification + row counts
app.py                Flask JSON API (CRUD, bookings, refunds, 17 named queries, schema)
make_er.py            draws static/er.svg by hand (no libraries at all)
run_prod.py           waitress entry point
static/               index.html + app.js + style.css + er.svg  (no build step, no CDN)
tests/test_cinema.py  93 unittest cases on a separate schema
```

### Pages inside the site

| Role | Tabs |
| --- | --- |
| **Customer** | Now Showing · My Bookings · Food & Drinks · Solution · Database · Source Code |
| **Box Office** | Shows · Bookings · Concession Counter · Customers · Solution · Database · Source Code |
| **Administrator** | Dashboard · Scheduling · Bookings · Reports · Master Data · Solution · Database · Source Code |

Plus a header **Live SQL Log** drawer and a role switcher with a customer selector.

---

## 3. The database (3NF)

17 tables. Every constraint below exists in PostgreSQL, not in Python.

### Tables

| # | Table | Purpose | Notable constraints |
| --- | --- | --- | --- |
| 1 | `cinema` | multiplexes | `UNIQUE(cinema_name, city)` |
| 2 | `screen` | auditoria | `screen_type IN ('2D','3D','IMAX','4DX')`, `UNIQUE(cinema_id, screen_name)` |
| 3 | `seat` | seat inventory | `seat_type IN ('STANDARD','PREMIUM','RECLINER')`, `seat_no > 0`, `UNIQUE(screen_id,row_label,seat_no)`, `ON DELETE CASCADE` |
| 4 | `movie` | catalogue | `duration_min > 0`, `certificate IN ('U','UA','A','S')`, `UNIQUE(title,language,release_date)` |
| 5 | `screening` | the shows | `end_time > start_time`, `UNIQUE(screen_id,start_time)`, **`EXCLUDE USING gist (screen_id WITH =, tsrange(start_time,end_time) WITH &&) WHERE status <> 'CANCELLED'`** |
| 6 | `customer` | patrons | `UNIQUE(email)` + e-mail regex CHECK, phone `^[0-9]{10,15}$` |
| 7 | `staff` | logins | `role IN ('ADMIN','BOX_OFFICE')`, `UNIQUE(username)` |
| 8 | `concession_item` | menu | `unit_price > 0`, `stock_qty >= 0` |
| 9 | `cancellation_policy` | refund tiers | `min_hours_before >= 0`, `refund_percent BETWEEN 0 AND 100` |
| 10 | `booking` | a purchase | `status IN ('CONFIRMED','CANCELLED')`, `UNIQUE(booking_id, screening_id)` (needed by the composite FK below) |
| 11 | `booked_seat` | seats held | composite FK to `booking`, **partial `UNIQUE INDEX ux_active_seat (screening_id, seat_id) WHERE status = 'BOOKED'`** |
| 12 | `ticket` | one per seat | `UNIQUE(booked_seat_id)`, `UNIQUE(ticket_code)`, `price >= 0` |
| 13 | `concession_order` | food sale | nullable `booking_id` (counter sale), `status IN ('PLACED','CANCELLED')` |
| 14 | `order_item` | order lines | `PK(order_id,item_id)`, `quantity > 0` |
| 15 | `payment` | money received | `amount > 0`, method/status CHECKs, `UNIQUE(transaction_ref)`, `booking_id IS NOT NULL OR order_id IS NOT NULL` |
| 16 | `cancellation` | who cancelled | `UNIQUE(booking_id)`, `cancelled_by IN ('CUSTOMER','STAFF','SYSTEM')` |
| 17 | `refund` | money returned | `refund_amount > 0`, `refund_status IN ('INITIATED','COMPLETED')` |

All foreign keys are `ON DELETE RESTRICT` except `seat` (cascade) and the two child
collections of a booking.

### Functions

| Function | Contract |
| --- | --- |
| `fn_schedule_screening(movie, screen, start, price)` | inserts a show and computes `end_time = start + duration + 15 min` cleaning gap; the EXCLUDE constraint rejects overlaps |
| `fn_seat_id(screen, row, no)` | resolves a seat id from the seat map coordinates |
| `fn_refund_percent(booking_id, at_time)` | picks the policy row with the largest `min_hours_before <= hours until the show` |
| `fn_now()` | the clock: `CINEMA_NOW` when set, otherwise `now()` |

### Triggers (5)

| Trigger | Fires | Guarantees |
| --- | --- | --- |
| `fn_validate_booked_seat` | BEFORE INSERT/UPDATE `booked_seat` | the seat belongs to the screening's screen; the show is `SCHEDULED` and has not started |
| `fn_validate_cancellation` | BEFORE INSERT `cancellation` | the booking is `CONFIRMED` and the show has not started |
| `fn_apply_cancellation` | AFTER INSERT `cancellation` | booking → `CANCELLED`, its tickets → `VOID`, its seats → `RELEASED` |
| `fn_validate_refund` | BEFORE INSERT `refund` | refunds (including earlier ones) never exceed `paid × fn_refund_percent / 100` |
| `fn_order_item_stock` | BEFORE INSERT `order_item` | takes `unit_price` from the menu and reduces `stock_qty`; `CHECK (stock_qty >= 0)` blocks over-selling |

Every trigger raises a human-readable `RAISE EXCEPTION`, which the API turns into a clean
HTTP 409 with that sentence in a red toast.

### Views (6)

`v_show_occupancy`, `v_movie_performance`, `v_screen_utilization`, `v_concession_sales`,
`v_cancellation_summary`, `v_daily_revenue` (tickets − refunds + concessions).

### Indexes (8)

`ux_active_seat` (partial unique, the double-booking stopper) plus `movie(title)`,
`screening(movie_id)`, `screening(start_time)`, `booking(customer_id)`,
`booking(screening_id)`, `payment(booking_id)`, `concession_order(booking_id)`.

### Deliberate, documented denormalisation

15 of the 17 tables are in strict 3NF; two design choices are exceptions and both are
discussed on the **Solution** page:

1. **`screening.end_time` is stored**, not derived. A `GENERATED` column cannot take part
   in an `EXCLUDE` constraint, and the database — not Python — must stop overlapping shows.
2. **`booked_seat.screening_id` is repeated** from `booking` so that `ux_active_seat` can be
   a plain declarative partial index. The composite foreign key
   `(booking_id, screening_id) → booking(booking_id, screening_id)` keeps the copy honest:
   the two columns can never disagree.

Seat price = `base_price × multiplier` (STANDARD 1.0, PREMIUM 1.3, RECLINER 1.6), applied
once in SQL and reused by the API, the ticket rows and the reports.

---

## 4. API map

Errors are always `{"error": "readable sentence"}`. `UniqueViolation`, `CheckViolation`,
`ForeignKeyViolation`, `ExclusionViolation` and trigger `RaiseException` map to HTTP 409
with the sentence from the database.

```
GET    /api/meta                     cinema, screens, movies, policies, object counts, clock
CRUD   /api/movies  /api/customers  /api/items          (referenced rows → clean 409)

GET    /api/screenings               filters: date, from, cinema_id, movie_id, upcoming, bookable
POST   /api/screenings               future only; overlap → 409 (EXCLUDE constraint)
PUT    /api/screenings/<id>          reschedule, end_time recomputed
POST   /api/screenings/<id>/cancel   only when no confirmed booking
DELETE /api/screenings/<id>
GET    /api/screenings/<id>/seats    seat map: type, price, taken flag

POST   /api/bookings                 {customer_id, screening_id, seat_ids[], method, staff_id}
                                     ONE transaction: booking + booked_seat + ticket(s) + payment
GET    /api/bookings                 filters: customer_id, screening_id, status, q
GET    /api/bookings/<id>            booking + seats + tickets + payments + orders
GET    /api/bookings/<id>/refund-preview
POST   /api/bookings/<id>/cancel     {reason} → cancellation + refund + payment REFUNDED

POST   /api/orders                   {items[], method, booking_id?, staff_id?}  1-20 per item
GET    /api/orders

GET    /api/dashboard                KPIs, 14-day revenue trend, next shows, payment mix
GET    /api/schema                   tables, columns, PK/FK/unique, row counts, DDL, triggers, views
GET    /api/reports/<key>            the 17 named queries
GET    /api/queries                  the query catalogue (title, kind, description, SQL)

GET    /api/db/tables                the 17 tables with row counts
GET    /api/db/tables/<name>         whitelisted table, page/sort/search, max limit 100
GET    /api/db/objects               views, triggers (+ function SQL), functions, indexes
POST   /api/db/query                 READ-ONLY SQL console, one SELECT/WITH, 3 s timeout, 200 rows

GET    /api/source                   whitelist of the 14 published files
GET    /api/source/<path>            one whitelisted file (.env, logs, ".." → 404)
GET    /api/source.zip               the whitelisted files as a zip
POST   /api/tests/run                runs tests/test_cinema.py (one at a time, 60 s timeout)
GET    /api/log?since=<id>           the Live SQL Log
POST   /api/log/clear
```

### Read-only SQL console

Exactly one statement, must start with `SELECT` or `WITH`, executed inside a
`READ ONLY` transaction with `statement_timeout = 3000` and a 200-row cap. `INSERT`,
`UPDATE`, `DELETE`, `DROP`, `ALTER`, `GRANT`, `COPY` and multi-statement input are refused
with a clear message; keywords inside string literals are ignored, and console queries stay
out of the Live SQL Log.

---

## 5. Tests

```bash
python tests/test_cinema.py            # human readable
python tests/test_cinema.py --json     # one JSON line (what POST /api/tests/run returns)
```

93 cases, plain `unittest`, against a throw-away schema (`cinema_test_<pid>`) created from
`supabase/schema.sql` with its own small fixture — the demo data in `public` is never
touched. `CINEMA_NOW` pins the clock so every time rule is deterministic.

Coverage highlights:

* scheme: schedule a show, reject an overlap (and the 15-minute gap boundary), reject an
  identical slot, cancel a show to free the screen, reschedule recomputes `end_time`
* booking: tickets at 1.0/1.3/1.6 prices, one payment, unknown/deleted/cross-screen seat
  rejected, started and cancelled shows rejected, 1–10 seats, duplicate seats rejected
* the money shot: a refused double booking **rolls the whole request back** — the free seat
  of the same request is still free afterwards
* refunds: 24 h → 100 %, 2 h → 50 %, under 2 h → 0 % at every boundary; cancellation voids
  tickets, releases seats and rebooking issues a new ticket code; double cancel rejected;
  a refund above the limit is refused by trigger T4
* concessions: price taken from the menu, stock reduced, over-selling rolled back,
  `stock_qty >= 0` enforced by the CHECK
* integrity: duplicate e-mail, e-mail format, phone format, FK protection on delete
* every named query runs; both integrity audits must return 0 rows
* API surface: 17 tables listed, non-whitelisted table refused, SQL console refuses
  `INSERT`/`DROP`/multiple statements, `/api/source/.env` and `/api/source/../app.py`
  are 404, the SQL log shows `BEGIN/INSERT/COMMIT` for a booking and a `ROLLBACK` for a
  failed one

---

## 6. Front-end notes

Plain HTML + CSS + vanilla JS, one page, hash routes, no build step and no CDN scripts —
the only external resource is the Google Fonts stylesheet. Colours come exclusively from
`:root` CSS variables (`--bg #050A14`, `--glow #123B66`, `--panel #0B1B30`, `--cyan #6FD3F2`,
…). Oswald for headings, DM Sans for body, JetBrains Mono for code, ticket codes and SQL.
All user text is escaped before it reaches the DOM.

`static/er.svg` is generated by `make_er.py`, which lays the 17 entities out itself,
routes the crow's-foot connectors and *checks its own output*: no connector may cross an
entity box and no two relationship labels may overlap.

---

## 7. Viva demo script (7 steps)

1. **Book a seat.** Customer → *Now Showing* → pick a show → the seat map opens with the
   glowing cyan SCREEN bar; click two seats (watch the sticky summary price and the
   STANDARD/PREMIUM/RECLINER multiplier), choose a payment method → **Pay & confirm** → the
   ticket modal shows one ticket per seat with its `TKT-…` code.
2. **See the new rows.** Inside the site: *Database* → table **booking** (then `booked_seat`,
   `ticket`, `payment`) and use the search box for the new id. Outside: Supabase →
   **Table Editor** → `booking`, `booked_seat`, `ticket`, `payment` — the rows are there.
3. **Break it on purpose.** Book the *same* seat on the *same* show from a second booking
   (the seat now shows as dim/booked, so use the API/SQL console or a second browser tab):
   the database refuses it and the site shows a red toast with the database's own message
   (the `ux_active_seat` partial unique index).
4. **Show the rollback.** Open the header **Live SQL Log**: `BEGIN` → the `INSERT INTO
   booking` → the failing `INSERT INTO booked_seat` → `ROLLBACK` in red with the index name,
   and no `COMMIT` anywhere in that request.
5. **Cancel with a refund.** *My Bookings* → **Cancel** → the modal first calls
   *refund-preview*, which reads the policy table live: 24 h+ → 100 %, 2–24 h → 50 %,
   under 2 h → 0 %. Confirm: the refund row is created, the payment flips to `REFUNDED`,
   the tickets become `VOID` and the seats are freed and instantly bookable again.
6. **Overlap a show.** Administrator → *Scheduling* → try to schedule a show on a screen one
   hour into an existing show: it is rejected with the EXCLUDE constraint message
   (`ex_screening_no_overlap`). Move it into a free slot and it is accepted. Then try
   cancelling a show that already has confirmed bookings — also refused.
7. **Show the source.** *Source Code* → `supabase/schema.sql` → the five trigger functions
   with their `RAISE EXCEPTION` messages, then `app.py` → `bookings_create` (the single
   transaction) — and finally the **Tests** tab → *Run tests* → `93 passed, 0 failed`.

---

## 8. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `DATABASE_URL is not set` | `cp .env.example .env` and paste the Supabase URI. |
| `relation "booking" does not exist` | `python setup_db.py`. |
| `extension "btree_gist" is not available` | Supabase has it; on a bare server run `CREATE EXTENSION btree_gist;` as a superuser. |
| Pooler connection fails | Use the direct 5432 URI, or add `?sslmode=require` to the 6543 URI. |
| Nothing in Supabase Table Editor | Check the schema in the Table Editor selector and refresh; confirm `setup_db.py` printed 17 tables. |
| `port already in use` | `PORT=5050 python app.py`. |
