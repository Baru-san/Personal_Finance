# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Ledger" — a retro terminal-styled personal finance tracker: scheduled budget
categories, an auto-save-on-unspent rule, and savings pots, backed by SQLite.
Single-user, self-hosted, gated by one shared password (see Authentication
below) — no per-user accounts.

## Commands

```
pip install -r requirements.txt
python app.py          # runs dev server on 0.0.0.0:5000, and calls init_db()
                        # to create ledger.db on first run
```

There is no test suite, linter, or build step in this repo.

Relevant environment variables (all optional — see Authentication below for
what happens when they're unset):

- `LEDGER_PASSWORD_HASH` — a `werkzeug.security.generate_password_hash(...)`
  output, for automated/secrets-manager deploys. Leave unset for the normal
  flow: the app prompts for a password itself on first run (see below).
- `SECRET_KEY` — signs the session cookie; auto-generated and persisted to
  `.secret_key` if unset.
- `CLOSE_DAY_TOKEN` — shared secret an external cron sends to `/close-day`.
- `LEDGER_ENV` — set to `production` to mark the session cookie
  `Secure` (requires HTTPS).
- `LEDGER_DISABLE_AUTH=1` — turns the login gate off entirely (`require_login`
  returns immediately, `/login` and `/setup` redirect to the dashboard, and the
  Logout button is hidden via the `auth_disabled` Jinja global). For local
  development only — with it set, every route including the write endpoints is
  open to anyone who can reach the port.
- `LEDGER_DEBUG=1` — enables the Werkzeug debugger/reloader; off by default
  because that console is remote code execution on a host bound to `0.0.0.0`.
- `DB_PATH` — overrides where `ledger.db` lives; defaults to
  `BASE_DIR/ledger.db`. Only needs setting when the app directory itself
  isn't persistent storage (see Deployment notes below).

## Architecture

Everything lives in `app.py` (single-file Flask app) — no blueprints, no
models/views split. It's organized top-to-bottom as: DB helpers → domain
logic → routes.

- **`get_db()`/`close_db()`**: per-request SQLite connection stashed on
  Flask's `g`, closed in `teardown_appcontext`. `row_factory = sqlite3.Row`
  so query results are accessed by column name (`row["col"]`). **Invariant:**
  every value that comes from a request goes through a `?`/named placeholder,
  never string-built into SQL. The one exception is the `client_id` migration
  loop in `init_db()`, which interpolates table names — that's safe only
  because those names come from the hardcoded `MIGRATED_TABLES` constant,
  never from runtime input; don't extend it with anything else.
- **`init_db()`**: creates tables and seeds the three `pots` if empty. Runs on
  `python app.py` directly, and on *every* import under gunicorn — not only
  when `ledger.db` is missing, since it is also where schema changes are
  applied and a deployed database on a Fly volume would otherwise never get
  a newly added table. It is idempotent (`CREATE TABLE IF NOT EXISTS`, and
  the pots seed only fires on an empty table). `categories` and `income_sources` intentionally
  start empty — the user adds their own via the UI, nothing is pre-seeded.
- **Schema** (SQLite, `ledger.db`): `categories` (budget_amount, optional
  `due_day` 1–28 for recurring bills), `transactions` (`kind` is `'spend'` or
  `'auto_save'`, `amount` always stored positive, `created_at` is an ISO
  string used for month-range filtering, FK to `categories` with
  `ON DELETE CASCADE`), `pots` (named savings buckets), `pot_deposits`
  (manual "set aside" deposits into a pot — pot_id/amount/optional note/
  `created_at`; the audit trail behind `pots.balance` for money moved by hand
  rather than swept), `income_sources`
  (recurring income templates — name/amount/optional `due_day`, counted every
  month regardless of `due_day`; `due_day` is informational only, mirroring
  how `categories.due_day` works), `custom_incomes` (one-off income entries —
  label/amount/`created_at`, counted only for the month they were logged in).
  There is also a legacy unused `settings` table left over on databases
  created before the income-sources model existed — harmless, no code reads
  or writes it anymore.
- **Money values** are plain integers (Rupiah, no decimals) — no float
  arithmetic for currency anywhere. Formatted for display via the
  `rp` Jinja filter (`format_rp`), registered as `app.jinja_env.filters["rp"]`.
- **Month logic**: `month_bounds()` gives the current calendar month's
  `[start, end)` ISO date range, used to filter transactions by
  `created_at`. `available_months()` / `get_ledger_for_month()` support the
  `/ledger` history view, keyed by `'YYYY-MM'` strings.
- **Auto-save-on-unspent rule**: `/close-day` (POST) is the core domain
  action — for every category whose `due_day` matches today, any leftover
  budget (`budget_amount - spent`) becomes an `auto_save` transaction and is
  added to the "Auto-saved (Unspent)" pot. It also prunes transactions older
  than 12 months (`prune_old_transactions`). This is meant to run once daily
  unattended (e.g. a cron hitting the endpoint) — see README for the
  Fly.io/cron setup — not just via the dashboard's "Run Daily Close" button.
- **Manual pot deposits**: the Savings tab's "Set aside current money" form
  (`POST /savings/deposit`) banks cash you already have into any pot — it
  inserts a `pot_deposits` row *and* adds to that pot's stored `balance`, the
  same two-step bookkeeping `sweep_unspent` does, since `pots.balance` is a
  running total rather than derived. The balance is only credited when the
  insert actually happened (`cur.rowcount`), so an offline replay that hits the
  `client_id` conflict can't move the money twice.
  `POST /savings/deposit/<id>/delete` undoes one, debiting the pot again.
  `pot_deposit_for_range()` sums the current month's deposits and is subtracted
  from `available` on both the dashboard and the Savings tab, so money set aside
  stops being counted as unallocated.
- **Income**: `income = total_scheduled_income(db)` (sum of all `income_sources`,
  always counted) `+ custom_income_for_range(db, start, end)` (this month's
  `custom_incomes` only). `available = income - total_scheduled` (sum of
  category budgets) `- pot_deposit_for_range(...)` (money already set aside by
  hand this month), computed in `build_dashboard_data()`.
- **Unified entry feed**: `ENTRY_UNION_SQL` (parameterized with optional date
  filters) is the shared building block behind `ALL_ENTRIES_SQL` (recent
  activity, all-time) and `LEDGER_ENTRIES_SQL` (a single month) — both `UNION
  ALL` `transactions` (spend/auto_save) with `custom_incomes` (kind forced to
  `'income'`) so the dashboard/ledger views render one merged, date-sorted
  list. Each row carries a `source` column (`'transaction'` or
  `'custom_income'`) since the two tables' primary keys collide — the journal
  edit page uses `source` to only render an edit form for `'transaction'`
  rows (`edit_journal_entry` only knows how to update the `transactions`
  table).
- **Editing a transaction** (`/journal/entry/<id>/edit`) changes its `amount`
  directly; since spend totals are always computed live via `SUM(...)` over
  `transactions`, this naturally acts as a refund (lower amount) or extra
  charge (higher amount) with no separate ledger entry. The one exception is
  `auto_save` transactions, where the delta must also be applied by hand to
  the "Auto-saved (Unspent)" pot's stored `balance`, since that figure is a
  running total rather than derived.
- **Routes** are thin: they pull data via the domain-logic functions above,
  write through `db.execute(...)` + `db.commit()` directly (no ORM), and
  `redirect(url_for(...))` back to the relevant page after POSTs.
- **Dashboard aggregation** (`build_dashboard_data()`) computes per-category
  spend/percent/status (`OVER BUDGET`, `COMPLETE`, `DUE TODAY`, `UNTOUCHED`,
  `ON TRACK`) and overall `available = income - total_scheduled`.

## Authentication

Single shared password, no per-user accounts. `require_login()`
(`@app.before_request`) gates every route except `login`, `setup`, `static`,
and `service_worker`; a valid session (`session["auth"]` matching a
fingerprint derived from the current password hash) or a matching
`X-Close-Day-Token` header on `/close-day` lets a request through. An
unauthenticated **POST** gets a bare `401`, not a redirect —
`static/offline.js`'s write queue treats a redirect-to-login as a false
"success" and deletes the queued entry, so this distinction is load-bearing,
not stylistic.

The password itself is never stored, only its one-way hash
(`werkzeug.security.generate_password_hash`), sourced from either
`LEDGER_PASSWORD_HASH` (env var, for automated deploys) or a `.password_hash`
file (mode 0600) written by the first-run setup wizard — `_load_password_hash()`
checks the env var first, then that file. **If neither exists, the app is not
open — it's locked to everything except `/setup`.** `GET /setup` requires a
`?token=` matching `SETUP_TOKEN`, a random value generated once at startup
and printed only to the process's own console; this closes the "first person
to hit the freshly-deployed public URL sets the password" race, since a
remote attacker never sees that token. Once setup completes,
`LEDGER_PASSWORD_HASH`/`AUTH_FINGERPRINT` are rebound in-process (no restart
needed) and `SETUP_TOKEN` is cleared, so `/setup` redirects to `/login`
permanently from then on — token or not.

Logging out (`POST /logout`) clears the session and redirects to
`/login?logged_out=1`; `templates/login.html` reads that flag and purges the
`ledger-*` Cache Storage entries client-side (not the IndexedDB write queue —
losing unsynced writes on logout would be worse than a stale cache).

## Templates & static

- `templates/dashboard.html`, `templates/ledger.html`, `templates/journal.html`,
  and `templates/income.html` are server-rendered Jinja2, styled by the single
  `static/style.css` (retro terminal/CRT theme: monospace, scanline/blink
  effects, `.window`/`.pot`/`.log-entry` components). All four pages share a
  `.topnav` (Dashboard/Ledger/Income) in the top bar. Minimal inline
  `<script>` blocks only (e.g. month dropdown, progress bar width) — no JS
  framework or build pipeline.
- Templates read row data as `dict`-style (`t['field']`) or attribute-style
  (`c.field`) interchangeably — both work because of `sqlite3.Row`.

## Hosting on PythonAnywhere

An alternative to Fly for a card-free, persistent-disk host. Two files exist
only for it and are inert everywhere else:

- `wsgi_pythonanywhere.py` — template for the WSGI file PythonAnywhere owns.
  Sets `SECRET_KEY`/`LEDGER_PASSWORD_HASH`/`LEDGER_ENV`/`DB_PATH` in
  `os.environ` **before** `from app import app as application`, because
  `app.py` reads all of its configuration at import time. The repo copy holds
  placeholders only — real values are filled in on their side, outside version
  control.
- `close_day_task.py` — the daily close for their Tasks tab. Free accounts
  can't make arbitrary outbound requests, so rather than curling `/close-day`
  it sets a random `CLOSE_DAY_TOKEN` in its own environment, imports the app,
  and POSTs the route through Flask's test client with that token — reusing
  the real route instead of duplicating sweep logic that could drift.
  `require_login()` checks the close-day token before the password branch, so
  this works whether or not a password is configured.

## Deployment notes (from README)

Intended for Fly.io, and the repo carries the deploy artifacts: `Dockerfile`
(python:3.12-slim, non-root, `gunicorn -b 0.0.0.0:8080 --workers 1
--threads 4 app:app` — one worker because every write hits a single SQLite
file), `.dockerignore` (keeps `ledger.db`/`.secret_key` out of the image),
and `fly.toml` (volume mounted at `/data`, `LEDGER_ENV`/`DB_PATH` in
`[env]`). A volume attaches to one machine only — never scale past 1. Fly's filesystem is ephemeral by
default, which matters for two unrelated things: `SECRET_KEY` and
`LEDGER_PASSWORD_HASH` should be set via `fly secrets` (not left to their
on-disk `.secret_key`/`.password_hash` fallbacks — see Authentication above),
while `ledger.db` itself needs a real Fly Volume, mounted somewhere other
than the app's own directory (e.g. `/data`) with `DB_PATH` (env var,
`app.py:15`) pointed at it — `DB_PATH` has no default other than
`BASE_DIR/ledger.db`, so it must be set explicitly whenever the volume isn't
mounted at the app directory. `/close-day` must be triggered by an external
scheduler (Fly Machines scheduled run or cron) in production; there is no
in-app scheduler.
