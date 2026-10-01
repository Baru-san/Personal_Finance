import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, g, send_from_directory, session, abort, flash
from flask_compress import Compress
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Override for deployments where the app directory itself isn't persistent
# (e.g. Fly.io without a volume mounted there) — point this at a mounted
# volume instead, such as /data/ledger.db.
DB_PATH = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "ledger.db")

app = Flask(__name__)
Compress(app)  # gzip/br responses — matters most on the slow/metered connections this app targets

# Cache static assets for a year; safe because every url_for('static', ...) call
# gets a ?v=<mtime> query string appended below, so a changed file gets a new URL
# instead of serving a stale cached one.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

# ---------- auth config ----------
# Single-user app: one password, one long-lived session cookie, no accounts.
#
# SECRET_KEY signs that cookie. If it's not set in the environment, generate
# one once and persist it to disk — without persistence every restart or
# deploy would silently invalidate every session.
SECRET_KEY_PATH = os.path.join(BASE_DIR, ".secret_key")
LEDGER_ENV = os.environ.get("LEDGER_ENV", "development")

_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH) as f:
            _secret_key = f.read().strip()
    if not _secret_key:
        _secret_key = secrets.token_hex(32)
        with open(SECRET_KEY_PATH, "w") as f:
            f.write(_secret_key)
        os.chmod(SECRET_KEY_PATH, 0o600)
app.secret_key = _secret_key

# The password itself is never stored — only its one-way hash, either
# supplied out-of-band (LEDGER_PASSWORD_HASH, for automated/secrets-manager
# deploys) or chosen through the /setup wizard on first run and persisted to
# a local file readable only by the OS user running this process. Neither
# source of truth is ever readable over HTTP, logged, or reversible.
PASSWORD_HASH_PATH = os.path.join(BASE_DIR, ".password_hash")
CLOSE_DAY_TOKEN = os.environ.get("CLOSE_DAY_TOKEN") or None

# Local-development escape hatch: LEDGER_DISABLE_AUTH=1 turns the login gate
# off entirely, so the app is usable without setting a password first. It is
# opt-in and defaults to off precisely because flipping it on a host bound to
# 0.0.0.0 (or on Fly) leaves every route, including the write endpoints, open
# to anyone who finds the URL — never set it in a deployment.
AUTH_DISABLED = os.environ.get("LEDGER_DISABLE_AUTH") == "1"


def _load_password_hash():
    env_hash = os.environ.get("LEDGER_PASSWORD_HASH")
    if env_hash:
        return env_hash
    if os.path.exists(PASSWORD_HASH_PATH):
        with open(PASSWORD_HASH_PATH) as f:
            return f.read().strip() or None
    return None


LEDGER_PASSWORD_HASH = _load_password_hash()

# A short digest of the password hash, stored in the session instead of a
# plain "logged in" flag — rotating the password this way invalidates every
# existing session instead of leaving a stolen cookie valid for a year.
AUTH_FINGERPRINT = (
    hashlib.sha256(LEDGER_PASSWORD_HASH.encode()).hexdigest() if LEDGER_PASSWORD_HASH else None
)

# First-run only: a high-entropy token that must be presented to /setup
# before it will accept a new password. Without this, whoever's fastest to
# hit the freshly-deployed public URL — attacker or owner — gets to set the
# password; this token is only ever printed to the process's own console
# (i.e. only visible to whoever controls the deploy), so a remote attacker
# racing to /setup can't win. It's cleared the moment setup completes, so
# the route is permanently closed after first use regardless of the token.
SETUP_TOKEN = None
if AUTH_DISABLED:
    print("=" * 72)
    print("AUTH DISABLED (LEDGER_DISABLE_AUTH=1): every route is open, no login.")
    print("Use this for local development only — never on a public host.")
    print("=" * 72)
elif not LEDGER_PASSWORD_HASH:
    SETUP_TOKEN = secrets.token_urlsafe(24)
    print("=" * 72)
    print("FIRST-TIME SETUP: no password configured yet.")
    print(f"Open http://<this-host>:<port>/setup?token={SETUP_TOKEN}")
    print("to choose a password before exposing this app to any network you don't trust.")
    print("=" * 72)

PUBLIC_ENDPOINTS = {"login", "setup", "static", "service_worker"}

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",  # also the CSRF mitigation for this single-user app
    SESSION_COOKIE_SECURE=(LEDGER_ENV == "production"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),  # installed PWA must not expire mid-use
)

# Escalating brute-force lockout on /login. Deliberately a single global
# counter rather than per-IP: this is a single-user app behind Fly's proxy,
# where trusting X-Forwarded-For for per-IP keying would be meaningless.
LOGIN_ATTEMPTS = {"count": 0, "locked_until": 0.0}
LOCKOUT_THRESHOLD = 5
LOCKOUT_BASE_SECONDS = 60
LOCKOUT_CAP_SECONDS = 15 * 60


def login_locked_out():
    return time.time() < LOGIN_ATTEMPTS["locked_until"]


def login_lockout_seconds_remaining():
    return max(0, int(LOGIN_ATTEMPTS["locked_until"] - time.time()))


def register_failed_login():
    LOGIN_ATTEMPTS["count"] += 1
    if LOGIN_ATTEMPTS["count"] >= LOCKOUT_THRESHOLD:
        tier = LOGIN_ATTEMPTS["count"] - LOCKOUT_THRESHOLD + 1
        delay = min(LOCKOUT_BASE_SECONDS * (2 ** (tier - 1)), LOCKOUT_CAP_SECONDS)
        LOGIN_ATTEMPTS["locked_until"] = time.time() + delay


def register_successful_login():
    LOGIN_ATTEMPTS["count"] = 0
    LOGIN_ATTEMPTS["locked_until"] = 0.0


def safe_next_path(raw):
    """Only ever redirect to a same-site relative path — an open redirect
    here (e.g. accepting '//evil.com') would be a phishing vector."""
    if raw and raw.startswith("/") and not raw.startswith("//") and not raw.startswith("/\\"):
        return raw
    return url_for("dashboard")


@app.before_request
def require_login():
    if AUTH_DISABLED:
        return None

    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    if request.endpoint == "close_day" and CLOSE_DAY_TOKEN:
        token = request.headers.get("X-Close-Day-Token", "")
        if hmac.compare_digest(token, CLOSE_DAY_TOKEN):
            return None

    if not LEDGER_PASSWORD_HASH:
        # No password configured yet — block everything except /setup
        # (which itself requires the console-printed token) rather than
        # leaving the app open to whoever finds it first.
        if request.method == "POST":
            return ("Setup required", 401)
        return redirect(url_for("setup"))

    if session.get("auth") == AUTH_FINGERPRINT:
        return None

    if request.method == "POST":
        # Not a redirect: static/offline.js's write queue treats a non-2xx,
        # non-redirect response as "failed, keep it queued" — a 302 to the
        # login page would instead look like success (a followed redirect
        # resolves to 200) and the queued write would be deleted unsent.
        return ("Unauthorized", 401)

    return redirect(url_for("login", next=request.path))


@app.url_defaults
def add_static_file_version(endpoint, values):
    if endpoint == "static" and "filename" in values:
        filepath = os.path.join(app.static_folder, values["filename"])
        try:
            values["v"] = int(os.path.getmtime(filepath))
        except OSError:
            pass


# ---------- DB helpers ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# The one place in this app that builds SQL via f-string interpolation, used
# below in the client_id migration loop — safe only because every name here
# is a hardcoded literal, never runtime input. Must never be extended with a
# table name that comes from a request.
MIGRATED_TABLES = ("categories", "transactions", "income_sources", "custom_incomes", "custom_expenses", "pot_deposits", "pot_movements")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            budget_amount INTEGER NOT NULL DEFAULT 0,
            due_day INTEGER,                     -- day of month this is due (1-28), NULL = flexible or weekly
            sort_order INTEGER DEFAULT 0,
            period TEXT NOT NULL DEFAULT 'monthly', -- 'weekly' or 'monthly'
            client_id TEXT,                      -- see offline write-queue note below
            created_at TEXT                      -- when the category was added; bounds catch-up sweeps
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL,             -- always positive
            kind TEXT NOT NULL,                  -- 'spend' or 'auto_save'
            note TEXT,
            created_at TEXT NOT NULL,
            client_id TEXT,
            sweep_key TEXT                       -- auto_save only: the swept window's start date
        );

        CREATE TABLE IF NOT EXISTS pots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            balance INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS income_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount INTEGER NOT NULL,             -- recurring amount, counted every month
            due_day INTEGER,                     -- day of month this arrives (1-28), NULL = flexible
            sort_order INTEGER DEFAULT 0,
            client_id TEXT
        );

        CREATE TABLE IF NOT EXISTS custom_incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            amount INTEGER NOT NULL,             -- always positive, one-off, counted for its own month only
            created_at TEXT NOT NULL,
            client_id TEXT
        );

        CREATE TABLE IF NOT EXISTS pot_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pot_id INTEGER NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL,             -- always positive, money moved by hand into a pot
            note TEXT,
            created_at TEXT NOT NULL,
            client_id TEXT
        );

        CREATE TABLE IF NOT EXISTS custom_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            amount INTEGER NOT NULL,             -- always positive, one-off spend not tied to any category
            created_at TEXT NOT NULL,
            client_id TEXT
        );

        CREATE TABLE IF NOT EXISTS pot_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_pot_id INTEGER NOT NULL REFERENCES pots(id) ON DELETE CASCADE,
            to_pot_id INTEGER REFERENCES pots(id) ON DELETE CASCADE, -- NULL = available/remaining balance
            amount INTEGER NOT NULL,             -- always positive, money moved by hand out of a pot
            note TEXT,
            created_at TEXT NOT NULL,
            client_id TEXT
        );
        """
    )

    # Categories and income sources start empty — the user adds their own via the UI.

    # Migrate pre-existing databases that predate the weekly/monthly period column.
    existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(categories)")}
    if "period" not in existing_columns:
        db.execute("ALTER TABLE categories ADD COLUMN period TEXT NOT NULL DEFAULT 'monthly'")
    # Categories created before this column existed stay NULL and are treated as
    # legacy: /close-day only ever sweeps their most recent completed window
    # (the old behavior), never retroactively.
    if "created_at" not in existing_columns:
        db.execute("ALTER TABLE categories ADD COLUMN created_at TEXT")

    # Offline write queue: static/offline.js tags each queued POST with a
    # client-generated UUID so a replayed submission (page reload before the
    # first attempt's response arrived, a retried Background Sync, etc.)
    # can't insert the same row twice. A plain form submit (JS disabled, or
    # any pre-existing row) sends no client_id, leaving it NULL — the partial
    # index below only enforces uniqueness where one was actually supplied,
    # so those inserts are never affected.
    for table in MIGRATED_TABLES:
        assert table.isidentifier(), table  # never accept a runtime-derived table name here
        cols = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if "client_id" not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN client_id TEXT")
        db.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_client_id "
            f"ON {table}(client_id) WHERE client_id IS NOT NULL"
        )

    # A sweep is identified by (category, window start), so /close-day can be
    # called repeatedly (cron + the dashboard button, retries, or two requests
    # racing) without banking the same window twice: whichever INSERT wins the
    # partial unique index credits the pot, the rest are ON CONFLICT no-ops.
    tx_cols = {row["name"] for row in db.execute("PRAGMA table_info(transactions)")}
    if "sweep_key" not in tx_cols:
        db.execute("ALTER TABLE transactions ADD COLUMN sweep_key TEXT")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_sweep "
        "ON transactions(category_id, sweep_key) WHERE sweep_key IS NOT NULL"
    )

    cur = db.execute("SELECT COUNT(*) AS c FROM pots")
    if cur.fetchone()["c"] == 0:
        db.executemany(
            "INSERT INTO pots (name, balance) VALUES (?, ?)",
            [("Emergency Fund", 0), ("Sinking Fund", 0), ("Auto-saved (Unspent)", 0)],
        )

    db.commit()
    db.close()


# ---------- domain logic ----------

def month_bounds(today=None):
    today = today or date.today()
    start = today.replace(day=1)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    return start.isoformat(), next_start.isoformat()


def week_bounds(today=None):
    """Monday-Sunday window containing `today`. May straddle a month boundary."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(days=7)
    return monday.isoformat(), next_monday.isoformat()


def prev_week_bounds(today=None):
    """The Monday-Sunday window that ended most recently before/on `today`."""
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    prev_monday = this_monday - timedelta(days=7)
    return prev_monday.isoformat(), this_monday.isoformat()


def prev_month_bounds(today=None):
    """The calendar month that ended most recently before/on `today`."""
    today = today or date.today()
    this_month_start, _ = month_bounds(today)
    this_start = date.fromisoformat(this_month_start)
    if this_start.month == 1:
        prev_start = this_start.replace(year=this_start.year - 1, month=12)
    else:
        prev_start = this_start.replace(month=this_start.month - 1)
    return prev_start.isoformat(), this_month_start


def next_monday(today=None):
    """The next upcoming weekly-sweep date — today itself if today is a Monday."""
    today = today or date.today()
    days_ahead = (7 - today.weekday()) % 7
    return today if days_ahead == 0 else today + timedelta(days=days_ahead)


def next_month_first(today=None):
    """The next upcoming monthly-sweep date — today itself if today is the 1st."""
    today = today or date.today()
    if today.day == 1:
        return today
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


def period_bounds(period, today=None):
    return week_bounds(today) if period == "weekly" else month_bounds(today)


def mondays_in_month(year, month):
    """Count of Mondays in a calendar month — the number of weekly allowances issued."""
    ndays = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - date(year, month, 1)
    return sum(1 for d in range(ndays.days) if date(year, month, 1 + d).weekday() == 0)


def monthly_equivalent(budget, period, year, month):
    """What a category's budget counts as against the month's scheduled total."""
    if period == "weekly":
        return budget * mondays_in_month(year, month)
    return budget


def total_scheduled_income(db):
    row = db.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM income_sources").fetchone()
    return row["total"]


def custom_income_for_range(db, start, end):
    row = db.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM custom_incomes
           WHERE created_at >= ? AND created_at < ?""",
        (start, end),
    ).fetchone()
    return row["total"]


def custom_expense_for_range(db, start, end):
    row = db.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM custom_expenses
           WHERE created_at >= ? AND created_at < ?""",
        (start, end),
    ).fetchone()
    return row["total"]


def pot_deposit_for_range(db, start, end):
    """Money the user moved into a pot by hand this period. Unlike pots.balance
    (a running total that also absorbs sweeps and manual edits), this is the
    figure that has to come back off 'available' — it is money already set
    aside out of this month's unallocated cash."""
    row = db.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM pot_deposits
           WHERE created_at >= ? AND created_at < ?""",
        (start, end),
    ).fetchone()
    return row["total"]


def pot_withdrawn_for_range(db, start, end):
    """Money pulled back out of a pot into available cash this period (pot ->
    available movements). Unlike pot_deposit_for_range, this is added to
    'available' — it is money returning to this month's unallocated cash."""
    row = db.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM pot_movements
           WHERE to_pot_id IS NULL AND created_at >= ? AND created_at < ?""",
        (start, end),
    ).fetchone()
    return row["total"]


def auto_save_for_range(db, start, end):
    row = db.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
           WHERE kind = 'auto_save' AND created_at >= ? AND created_at < ?""",
        (start, end),
    ).fetchone()
    return row["total"]


def spent_for_category(db, category_id, start, end):
    row = db.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
           WHERE category_id = ? AND kind = 'spend'
             AND created_at >= ? AND created_at < ?""",
        (category_id, start, end),
    ).fetchone()
    return row["total"]


def _nice_ceil(x):
    """Round up to a clean axis-scale number (1 significant digit)."""
    if x <= 0:
        return 1
    magnitude = 10 ** (len(str(int(x))) - 1)
    import math
    return math.ceil(x / magnitude) * magnitude


CHART_W, CHART_H = 640, 200
CHART_PAD = {"l": 50, "r": 16, "t": 16, "b": 26}


def line_chart_geometry(points, value_key="cumulative", y_max=None, x_span=None, x_key="day"):
    """Project a list of point-dicts onto SVG plot coordinates (px/py) and build a
    polyline + filled area path. Mutates and returns `points`; each dict needs at
    least `value_key`. Shared by every line/area chart (spend trend, savings trajectory).

    By default points are spaced evenly by index — correct when every slot is
    present (the 12-month trajectory). Pass `x_span` to pin each point by a
    numeric `x_key` instead, so the series occupies its true position within a
    larger range: the day-by-day trend uses this to lay a month's elapsed days
    across the whole calendar month rather than stretching them edge to edge."""
    plot_w = CHART_W - CHART_PAD["l"] - CHART_PAD["r"]
    plot_h = CHART_H - CHART_PAD["t"] - CHART_PAD["b"]
    plot_bottom = CHART_PAD["t"] + plot_h

    if y_max is None:
        y_max = _nice_ceil(max((p[value_key] for p in points), default=0))

    n = len(points)
    for i, p in enumerate(points):
        if x_span and x_span > 1:
            p["px"] = round(CHART_PAD["l"] + plot_w * (p[x_key] - 1) / (x_span - 1), 2)
        else:
            p["px"] = round(CHART_PAD["l"] + (plot_w * i / (n - 1) if n > 1 else plot_w / 2), 2)
        p["py"] = round(plot_bottom - (plot_h * p[value_key] / y_max), 2)

    polyline = " ".join(f"{p['px']},{p['py']}" for p in points)
    if points:
        path_segments = " L ".join(f"{p['px']},{p['py']}" for p in points)
        area_path = f"M {points[0]['px']},{plot_bottom} L {path_segments} L {points[-1]['px']},{plot_bottom} Z"
    else:
        area_path = ""

    return {
        "points": points,
        "points_json": json.dumps(points),
        "polyline": polyline,
        "area_path": area_path,
        "w": CHART_W,
        "h": CHART_H,
        "plot_top": CHART_PAD["t"],
        "plot_bottom": plot_bottom,
        "plot_left": CHART_PAD["l"],
        "plot_right": CHART_W - CHART_PAD["r"],
        "y_max": y_max,
        "y_mid": y_max / 2,
    }


def spending_trend_chart(db, today, start, end):
    """Day-by-day cumulative spend for the current month, laid out as SVG geometry."""
    daily_rows = db.execute(
        """SELECT substr(created_at, 1, 10) AS d, SUM(amount) AS amt FROM (
               SELECT created_at, amount FROM transactions WHERE kind = 'spend'
                 AND created_at >= ? AND created_at < ?
               UNION ALL
               SELECT created_at, amount FROM custom_expenses
                 WHERE created_at >= ? AND created_at < ?
           ) GROUP BY d""",
        (start, end, start, end),
    ).fetchall()
    daily_totals = {r["d"]: r["amt"] for r in daily_rows}

    running = 0
    points = []
    for day_num in range(1, today.day + 1):
        d = date(today.year, today.month, day_num)
        running += daily_totals.get(d.isoformat(), 0)
        points.append({"day": day_num, "label": d.strftime("%d %b"), "amount": daily_totals.get(d.isoformat(), 0), "cumulative": running})

    # Anchor the x-axis to the whole current calendar month, not just the days
    # elapsed — otherwise the line always stretches edge to edge and (early in
    # the month) reads as if the month were already over.
    month_start = date(today.year, today.month, 1)
    next_month_start = (
        date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
    )
    days_in_month = (next_month_start - month_start).days

    geometry = line_chart_geometry(points, value_key="cumulative", x_span=days_in_month, x_key="day")
    geometry["x_last_label"] = date(today.year, today.month, days_in_month).strftime("%d %b")
    return geometry


def cash_flow_chart(db):
    """Income vs. spend per month, as grouped-column SVG geometry.

    Covers TREND_START_MONTH through the current month (see trend_months()), so
    the chart grows rather than rolling a month off the left edge each month.

    `scheduled_income` (income_sources) has no history — it's a live snapshot, so every
    month is credited with today's scheduled rate. Only custom_incomes varies
    per month. auto_save is not subtracted from spend: it's an internal transfer of
    unspent budget, not an outflow.
    """
    months = trend_months()
    scheduled = total_scheduled_income(db)

    rows = []
    for m in months:
        income = scheduled + custom_income_for_range(db, m["start"], m["end"])
        spend_row = db.execute(
            """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
               WHERE kind = 'spend' AND created_at >= ? AND created_at < ?""",
            (m["start"], m["end"]),
        ).fetchone()
        spend = spend_row["total"] + custom_expense_for_range(db, m["start"], m["end"])
        rows.append({
            "label": m["label"],
            "full_label": m["full_label"],
            "income": income,
            "spend": spend,
            "net": income - spend,
        })

    plot_w = CHART_W - CHART_PAD["l"] - CHART_PAD["r"]
    plot_h = CHART_H - CHART_PAD["t"] - CHART_PAD["b"]
    plot_bottom = CHART_PAD["t"] + plot_h

    y_max = _nice_ceil(max((max(r["income"], r["spend"]) for r in rows), default=0))
    n = len(rows)
    group_w = plot_w / n if n else plot_w
    bar_w = min(20, group_w * 0.32)
    gap = 3

    for i, r in enumerate(rows):
        group_left = CHART_PAD["l"] + group_w * i
        pair_w = bar_w * 2 + gap
        pair_left = group_left + (group_w - pair_w) / 2
        income_h = round(plot_h * r["income"] / y_max, 2) if y_max else 0
        spend_h = round(plot_h * r["spend"] / y_max, 2) if y_max else 0
        r["income_x"] = round(pair_left, 2)
        r["income_y"] = round(plot_bottom - income_h, 2)
        r["income_h"] = income_h
        r["spend_x"] = round(pair_left + bar_w + gap, 2)
        r["spend_y"] = round(plot_bottom - spend_h, 2)
        r["spend_h"] = spend_h
        r["group_cx"] = round(group_left + group_w / 2, 2)

    income_total = sum(r["income"] for r in rows)
    spend_total = sum(r["spend"] for r in rows)

    return {
        "rows": rows,
        "rows_json": json.dumps(rows),
        "bar_w": round(bar_w, 2),
        "w": CHART_W,
        "h": CHART_H,
        "plot_top": CHART_PAD["t"],
        "plot_bottom": plot_bottom,
        "plot_left": CHART_PAD["l"],
        "plot_right": CHART_W - CHART_PAD["r"],
        "y_max": y_max,
        "y_mid": y_max / 2,
        "income_total": income_total,
        "spend_total": spend_total,
        "net_total": income_total - spend_total,
    }


def savings_trajectory_chart(db):
    """Cumulative auto-saved total since TREND_START_MONTH, as line-chart geometry.

    Anchored to the same window as the cash-flow chart (see trend_months()), so
    the running total starts accumulating from that month. Tracks only the
    'Auto-saved (Unspent)' pot's source transactions — not pots.balance, which
    manual edits can also change.
    """
    running = 0
    points = []
    for m in trend_months():
        month_total = auto_save_for_range(db, m["start"], m["end"])
        running += month_total
        points.append({"label": m["full_label"], "month_total": month_total, "cumulative": running})
    return line_chart_geometry(points, value_key="cumulative")


def prune_old_transactions(retain_months=12):
    """Delete transactions older than `retain_months` months to cap storage growth."""
    db = get_db()

    today = date.today()
    month_index = today.month - retain_months
    year = today.year
    while month_index <= 0:
        month_index += 12
        year -= 1
    cutoff = date(year, month_index, today.day if today.day <= 28 else 28).isoformat()

    cur = db.execute("DELETE FROM transactions WHERE created_at < ?", (cutoff,))
    db.commit()
    return cur.rowcount, cutoff

def available_months(retain_months=12):
    """List of the last `retain_months` months as (key, label) pairs, newest first."""
    today = date.today()
    result = []
    for i in range(retain_months):
        month_index = today.month - i
        year = today.year
        while month_index <= 0:
            month_index += 12
            year -= 1
        key = f"{year:04d}-{month_index:02d}"
        label = date(year, month_index, 1).strftime("%B %Y").upper()
        result.append((key, label))
    return result


MONTH_KEY_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def month_key_bounds(month_key):
    """[start, end) ISO date range for a 'YYYY-MM' key. Both current callers
    pre-validate against available_months(), so this can't be reached with a
    malformed key today — the regex check is here so a future caller that
    forgets to validate gets the current month back instead of a 500."""
    if not MONTH_KEY_RE.match(month_key):
        return month_bounds()
    year, month = map(int, month_key.split("-"))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()


AMOUNT_MIN, AMOUNT_MAX = 1, 1_000_000_000_000  # Rp sanity bound; keeps chart geometry and formatting sane


def clamp_amount(value):
    """Reject an amount outside a sane range instead of letting a huge value
    reach chart geometry (line_chart_geometry) or format_rp."""
    if value is None or not (AMOUNT_MIN <= value <= AMOUNT_MAX):
        return None
    return value


def trailing_months(retain_months=12):
    """The last `retain_months` months, oldest first, each with its date-range bounds."""
    result = []
    for key, label in reversed(available_months(retain_months)):
        start, end = month_key_bounds(key)
        year, month = map(int, key.split("-"))
        result.append({
            "key": key,
            "label": date(year, month, 1).strftime("%b'%y"),
            "full_label": label.title(),
            "start": start,
            "end": end,
        })
    return result


# The Trends page charts are anchored here rather than to a rolling 12-month
# window: every month from this one through the current month is charted, so
# the charts grow one bar/point per month instead of shifting an old month off
# the left edge. The Savings page's "Saved by Month" chart still uses
# trailing_months(12), and ledger retention is unaffected.
TREND_START_MONTH = "2026-09"


def months_between(start_key, end_key=None):
    """Month descriptors (same shape as trailing_months), oldest first, from
    `start_key` through `end_key` (default: the current month). Falls back to
    the current month alone if `start_key` is after `end_key`, so a start month
    in the future never yields a blank chart."""
    today = date.today()
    end_key = end_key or f"{today.year:04d}-{today.month:02d}"
    if not MONTH_KEY_RE.match(start_key) or not MONTH_KEY_RE.match(end_key):
        return trailing_months(1)
    sy, sm = map(int, start_key.split("-"))
    ey, em = map(int, end_key.split("-"))
    if (sy, sm) > (ey, em):
        sy, sm = ey, em

    result = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        key = f"{y:04d}-{m:02d}"
        start, end = month_key_bounds(key)
        result.append({
            "key": key,
            "label": date(y, m, 1).strftime("%b'%y"),
            "full_label": date(y, m, 1).strftime("%B %Y").title(),
            "start": start,
            "end": end,
        })
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


def trend_months():
    """Months shown on the Trends page: TREND_START_MONTH through the current month."""
    return months_between(TREND_START_MONTH)


ENTRY_UNION_SQL = """
    SELECT t.id AS id, t.created_at AS created_at, t.amount AS amount, t.kind AS kind,
           t.note AS note, c.name AS category_name, 'transaction' AS source
    FROM transactions t
    JOIN categories c ON c.id = t.category_id
    {transactions_filter}
    UNION ALL
    SELECT ci.id AS id, ci.created_at AS created_at, ci.amount AS amount, 'income' AS kind,
           ci.label AS note, 'Custom Income' AS category_name, 'custom_income' AS source
    FROM custom_incomes ci
    {custom_incomes_filter}
    UNION ALL
    SELECT ce.id AS id, ce.created_at AS created_at, ce.amount AS amount, 'spend' AS kind,
           ce.label AS note, 'Custom Expense' AS category_name, 'custom_expense' AS source
    FROM custom_expenses ce
    {custom_expenses_filter}
"""

ALL_ENTRIES_SQL = (
    "SELECT id, created_at, amount, kind, note, category_name, source FROM ("
    + ENTRY_UNION_SQL.format(transactions_filter="", custom_incomes_filter="", custom_expenses_filter="")
    + ") ORDER BY created_at DESC"
)

LEDGER_ENTRIES_SQL = (
    "SELECT id, created_at, amount, kind, note, category_name, source FROM ("
    + ENTRY_UNION_SQL.format(
        transactions_filter="WHERE t.created_at >= :start AND t.created_at < :end",
        custom_incomes_filter="WHERE ci.created_at >= :start AND ci.created_at < :end",
        custom_expenses_filter="WHERE ce.created_at >= :start AND ce.created_at < :end",
    )
    + ") ORDER BY created_at DESC"
)

# Hand-made pot movements for the Savings page's merged audit log. Deposits
# (pot_deposits) are available -> pot; pot_movements are pot -> pot (transfer)
# or pot -> available (withdraw, to_pot_id NULL). `source` picks the matching
# UNDO route; `kind` drives the label and direction.
POT_MOVEMENT_LOG_SQL = """
    SELECT d.id AS id, d.created_at AS created_at, d.amount AS amount, d.note AS note,
           'deposit' AS kind, 'deposit' AS source,
           p.name AS pot_name, NULL AS counterpart_name
    FROM pot_deposits d
    JOIN pots p ON p.id = d.pot_id
    UNION ALL
    SELECT m.id AS id, m.created_at AS created_at, m.amount AS amount, m.note AS note,
           CASE WHEN m.to_pot_id IS NULL THEN 'withdraw' ELSE 'transfer' END AS kind,
           'movement' AS source,
           fp.name AS pot_name, tp.name AS counterpart_name
    FROM pot_movements m
    JOIN pots fp ON fp.id = m.from_pot_id
    LEFT JOIN pots tp ON tp.id = m.to_pot_id
    ORDER BY created_at DESC, id DESC
    LIMIT :limit
"""


def get_pot_movements(db, limit=20):
    return db.execute(POT_MOVEMENT_LOG_SQL, {"limit": limit}).fetchall()


def get_ledger_for_month(month_key):
    """Return all entries (spend + auto-save + custom income) for a single month (key = 'YYYY-MM')."""
    db = get_db()
    start, end = month_key_bounds(month_key)

    rows = db.execute(LEDGER_ENTRIES_SQL, {"start": start, "end": end}).fetchall()

    spend_total = sum(r["amount"] for r in rows if r["kind"] == "spend")
    save_total = sum(r["amount"] for r in rows if r["kind"] == "auto_save")
    income_total = sum(r["amount"] for r in rows if r["kind"] == "income")

    return {
        "entries": rows,
        "spend_total": spend_total,
        "save_total": save_total,
        "income_total": income_total,
        "count": len(rows),
    }

def build_dashboard_data():
    db = get_db()
    today = date.today()
    start, end = month_bounds(today)

    categories = db.execute(
        "SELECT * FROM categories ORDER BY sort_order, id"
    ).fetchall()

    cat_rows = []
    total_scheduled = 0
    for c in categories:
        period = c["period"]
        win_start, win_end = period_bounds(period, today)
        spent = spent_for_category(db, c["id"], win_start, win_end)
        budget = c["budget_amount"]
        total_scheduled += monthly_equivalent(budget, period, today.year, today.month)
        pct = min(100, round((spent / budget) * 100)) if budget else 0
        if spent > budget:
            status, css = "OVER BUDGET", "over"
        elif spent >= budget:
            status, css = "COMPLETE", "ok"
        elif c["due_day"] and c["due_day"] == today.day:
            status, css = "DUE TODAY", "warn"
        elif spent == 0:
            status, css = "UNTOUCHED", "ok"
        else:
            status, css = "ON TRACK", "ok"
        if period == "weekly":
            window_end = date.fromisoformat(win_end) - timedelta(days=1)
            window_label = f"{date.fromisoformat(win_start).strftime('%d')}–{window_end.strftime('%d %b')}"
        else:
            window_label = today.strftime("%B")
        cat_rows.append(
            {
                "id": c["id"],
                "name": c["name"],
                "spent": spent,
                "budget": budget,
                "pct": pct,
                "status": status,
                "css": css,
                "due_day": c["due_day"],
                "period": period,
                "window_label": window_label,
            }
        )

    pots = db.execute("SELECT * FROM pots ORDER BY id").fetchall()

    scheduled_income = total_scheduled_income(db)
    custom_income = custom_income_for_range(db, start, end)
    income = scheduled_income + custom_income
    manual_saved = pot_deposit_for_range(db, start, end)
    withdrawn = pot_withdrawn_for_range(db, start, end)
    custom_expense = custom_expense_for_range(db, start, end)
    available = income - total_scheduled - manual_saved + withdrawn - custom_expense

    recent = db.execute(ALL_ENTRIES_SQL + " LIMIT 8").fetchall()

    funded_count = sum(1 for r in cat_rows if r["spent"] >= r["budget"])

    total_spent = db.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
           WHERE kind = 'spend' AND created_at >= ? AND created_at < ?""",
        (start, end),
    ).fetchone()["total"] + custom_expense

    # What's actually left in hand, as opposed to `available` above: that one
    # reserves the full scheduled budget whether or not it has been spent yet,
    # while this subtracts only money that has really left the wallet — real
    # category spending plus anything banked into a pot by hand this month
    # (custom_expense is already counted in both, since it was never part of
    # a category budget to reserve). Money withdrawn from a pot back to
    # available (withdrawn) is added back to both figures.
    cash_left = income - total_spent - manual_saved + withdrawn

    trend = spending_trend_chart(db, today, start, end)

    return {
        "income": income,
        "scheduled_income": scheduled_income,
        "custom_income": custom_income,
        "total_scheduled": total_scheduled,
        "manual_saved": manual_saved,
        "withdrawn": withdrawn,
        "available": available,
        "categories": cat_rows,
        "pots": pots,
        "recent": recent,
        "funded_count": funded_count,
        "total_spent": total_spent,
        "cash_left": cash_left,
        "trend": trend,
        "today": date.today().strftime("%d %b %Y").upper(),
    }


def format_rp(n):
    return "Rp {:,}".format(int(n)).replace(",", ".")


app.jinja_env.filters["rp"] = format_rp
# Templates hide the Logout button when there's no login to log out of.
app.jinja_env.globals["auth_disabled"] = AUTH_DISABLED


# ---------- routes ----------

@app.route("/sw.js")
def service_worker():
    """Served from the site root (not /static/) so its default scope is '/' —
    a worker registered at /static/sw.js could only ever control /static/*,
    never the actual app pages."""
    response = send_from_directory(app.static_folder, "sw.js")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/setup", methods=["GET", "POST"])
def setup():
    global LEDGER_PASSWORD_HASH, AUTH_FINGERPRINT, SETUP_TOKEN

    if AUTH_DISABLED:
        return redirect(url_for("dashboard"))

    if LEDGER_PASSWORD_HASH:
        # Already configured (via env var or a completed setup) — permanently
        # closed from here on, regardless of any token presented.
        return redirect(url_for("login"))

    token = request.values.get("token", "")
    if SETUP_TOKEN is None or not hmac.compare_digest(token, SETUP_TOKEN):
        abort(403)

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            new_hash = generate_password_hash(password)
            with open(PASSWORD_HASH_PATH, "w") as f:
                f.write(new_hash)
            os.chmod(PASSWORD_HASH_PATH, 0o600)
            LEDGER_PASSWORD_HASH = new_hash
            AUTH_FINGERPRINT = hashlib.sha256(new_hash.encode()).hexdigest()
            SETUP_TOKEN = None  # closes this route for good, token or not
            session.permanent = True
            session["auth"] = AUTH_FINGERPRINT
            return redirect(url_for("dashboard"))

    return render_template("setup.html", error=error, token=token)


@app.route("/login", methods=["GET", "POST"])
def login():
    if AUTH_DISABLED:
        return redirect(url_for("dashboard"))

    error = None
    next_path = safe_next_path(request.args.get("next") or request.form.get("next"))

    if request.method == "POST":
        if not LEDGER_PASSWORD_HASH:
            error = "No password configured yet — complete setup first."
        elif login_locked_out():
            error = f"Too many attempts. Try again in {login_lockout_seconds_remaining()}s."
        elif check_password_hash(LEDGER_PASSWORD_HASH, request.form.get("password", "")):
            register_successful_login()
            session.permanent = True
            session["auth"] = AUTH_FINGERPRINT
            return redirect(next_path)
        else:
            register_failed_login()
            error = "Incorrect password."

    return render_template("login.html", error=error, next=next_path)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    # ?logged_out=1 tells the login page to purge the cached app shell (see
    # templates/login.html) — an installed PWA otherwise keeps rendering
    # cached balances to whoever holds the device after logout.
    return redirect(url_for("login", logged_out=1))


@app.route("/")
def dashboard():
    data = build_dashboard_data()
    return render_template("dashboard.html", **data)

@app.route("/ledger")
def ledger():
    months = available_months(retain_months=12)
    valid_keys = {k for k, _ in months}

    requested = request.args.get("month")
    selected = requested if requested in valid_keys else months[0][0]  # default: current month

    data = get_ledger_for_month(selected)
    return render_template(
        "ledger.html", **data,
        months=months,
        selected_month=selected,
        today=date.today().strftime("%d %b %Y").upper(),
    )

@app.route("/journal/<month_key>")
def journal(month_key):
    months = available_months(retain_months=12)
    valid_keys = {k for k, _ in months}
    if month_key not in valid_keys:
        return redirect(url_for("ledger"))

    data = get_ledger_for_month(month_key)
    return render_template(
        "journal.html", **data,
        months=months,
        selected_month=month_key,
        today=date.today().strftime("%d %b %Y").upper(),
    )


@app.route("/journal/entry/<int:entry_id>/edit", methods=["POST"])
def edit_journal_entry(entry_id):
    db = get_db()
    month_key = request.form.get("month", "")
    amount = clamp_amount(request.form.get("amount", type=int))
    note = request.form.get("note", "").strip()

    row = db.execute("SELECT * FROM transactions WHERE id = ?", (entry_id,)).fetchone()
    if row and amount and amount > 0:
        delta = amount - row["amount"]  # positive = charged more, negative = refunded
        db.execute(
            "UPDATE transactions SET amount = ?, note = ? WHERE id = ?",
            (amount, note, entry_id),
        )
        if row["kind"] == "auto_save" and delta != 0:
            db.execute(
                "UPDATE pots SET balance = balance + ? WHERE name = 'Auto-saved (Unspent)'",
                (delta,),
            )
        db.commit()

    return redirect(url_for("journal", month_key=month_key))


@app.route("/expense/add", methods=["POST"])
def add_expense():
    db = get_db()
    category_id = request.form.get("category_id", type=int)
    amount = clamp_amount(request.form.get("amount", type=int))
    note = request.form.get("note", "").strip()
    client_id = request.form.get("client_id") or None

    if category_id and amount and amount > 0:
        db.execute(
            """INSERT INTO transactions (category_id, amount, kind, note, created_at, client_id)
               VALUES (?, ?, 'spend', ?, ?, ?)
               ON CONFLICT(client_id) WHERE client_id IS NOT NULL DO NOTHING""",
            (category_id, amount, note, datetime.now().isoformat(timespec="seconds"), client_id),
        )
        db.commit()

    return redirect(url_for("dashboard"))


@app.route("/expense/custom/add", methods=["POST"])
def add_custom_expense():
    db = get_db()
    label = request.form.get("label", "").strip()
    amount = clamp_amount(request.form.get("amount", type=int))
    client_id = request.form.get("client_id") or None

    if amount and amount > 0:
        db.execute(
            """INSERT INTO custom_expenses (label, amount, created_at, client_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(client_id) WHERE client_id IS NOT NULL DO NOTHING""",
            (label or "Custom Expense", amount, datetime.now().isoformat(timespec="seconds"), client_id),
        )
        db.commit()

    return redirect(url_for("dashboard"))

def normalize_category_period(period, due_day):
    if period not in ("weekly", "monthly"):
        period = "monthly"
    if period == "weekly":
        due_day = None  # weekly categories reset every Monday, not on a day-of-month
    elif due_day is not None and not (1 <= due_day <= 28):
        due_day = None  # ignore invalid day-of-month instead of erroring
    return period, due_day


@app.route("/category/add", methods=["POST"])
def add_category():
    db = get_db()
    name = request.form.get("name", "").strip()
    budget_amount = clamp_amount(request.form.get("budget_amount", type=int))
    due_day = request.form.get("due_day", type=int)  # optional, None if blank
    period = request.form.get("period", "monthly")
    client_id = request.form.get("client_id") or None

    if name and budget_amount and budget_amount > 0:
        period, due_day = normalize_category_period(period, due_day)

        max_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM categories"
        ).fetchone()["m"]

        db.execute(
            """INSERT INTO categories (name, budget_amount, due_day, sort_order, period, client_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(client_id) WHERE client_id IS NOT NULL DO NOTHING""",
            (name, budget_amount, due_day, max_order + 1, period, client_id,
             datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()

    return redirect(url_for("dashboard"))


@app.route("/category/<int:category_id>/edit", methods=["POST"])
def edit_category(category_id):
    db = get_db()
    name = request.form.get("name", "").strip()
    budget_amount = clamp_amount(request.form.get("budget_amount", type=int))
    due_day = request.form.get("due_day", type=int)
    period = request.form.get("period", "monthly")

    if name and budget_amount and budget_amount > 0:
        period, due_day = normalize_category_period(period, due_day)
        db.execute(
            "UPDATE categories SET name = ?, budget_amount = ?, due_day = ?, period = ? WHERE id = ?",
            (name, budget_amount, due_day, period, category_id),
        )
        db.commit()

    return redirect(url_for("dashboard"))


def sweep_unspent(db, category, window_start, window_end, strict_key=False):
    """Move a category's unspent budget for [window_start, window_end) into the
    Auto-saved pot, unless that window was already swept (keeps /close-day safe
    to call more than once, e.g. from an external cron or the dashboard button).

    `strict_key=True` checks for an exact `sweep_key = window_start` row, which
    is what let a single /close-day catch up several missed windows without
    re-banking one. `strict_key=False` (the legacy path) instead treats any
    auto_save row at/after the window start as "already swept" — loose, but it
    recognizes rows written by older versions, which have no `sweep_key`.

    Either way the pre-check is only a fast path: two requests can both pass it
    before either writes. The INSERT carries `sweep_key` = the window's start
    date and relies on the partial unique index on (category_id, sweep_key) so
    only one can win, and only the winner credits the pot."""
    if strict_key:
        already_swept = db.execute(
            """SELECT 1 FROM transactions
               WHERE category_id = ? AND kind = 'auto_save' AND sweep_key = ?""",
            (category["id"], window_start),
        ).fetchone()
    else:
        already_swept = db.execute(
            """SELECT 1 FROM transactions
               WHERE category_id = ? AND kind = 'auto_save' AND created_at >= ?""",
            (category["id"], window_start),
        ).fetchone()
    if already_swept:
        return 0

    spent = spent_for_category(db, category["id"], window_start, window_end)
    leftover = category["budget_amount"] - spent
    if leftover <= 0:
        return 0

    cur = db.execute(
        """INSERT INTO transactions (category_id, amount, kind, note, created_at, sweep_key)
           VALUES (?, ?, 'auto_save', ?, ?, ?)
           ON CONFLICT(category_id, sweep_key) WHERE sweep_key IS NOT NULL DO NOTHING""",
        (category["id"], leftover, "Unspent scheduled amount", datetime.now().isoformat(timespec="seconds"), window_start),
    )
    if not cur.rowcount:
        return 0  # another request banked this window first
    db.execute(
        "UPDATE pots SET balance = balance + ? WHERE name = 'Auto-saved (Unspent)'",
        (leftover,),
    )
    return leftover


def apply_pot_movement(db, from_pot_id, to_pot_id, amount, note, client_id):
    """Move money out of one pot, either into another pot (to_pot_id set) or back
    to available/remaining balance (to_pot_id None). Both balances are updated in
    the same transaction, and only when the movement row was actually inserted —
    a replayed offline submission hits the client_id conflict and must not move
    the money a second time.

    Returns a reason string the route turns into user-facing feedback:
    'ok', 'invalid', 'unknown_pot', 'same_pot', 'insufficient', or 'duplicate'
    (an idempotent replay, not an error)."""
    if not amount or amount <= 0:
        return "invalid"

    source = db.execute(
        "SELECT id, balance FROM pots WHERE id = ?", (from_pot_id,)
    ).fetchone()
    if not source:
        return "unknown_pot"

    if to_pot_id is not None:
        # 0/negative is a malformed id, not "back to available" (which is None).
        if to_pot_id <= 0:
            return "invalid"
        if to_pot_id == source["id"]:
            return "same_pot"
        dest = db.execute("SELECT id FROM pots WHERE id = ?", (to_pot_id,)).fetchone()
        if not dest:
            return "unknown_pot"

    if source["balance"] < amount:
        return "insufficient"

    cur = db.execute(
        """INSERT INTO pot_movements (from_pot_id, to_pot_id, amount, note, created_at, client_id)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(client_id) WHERE client_id IS NOT NULL DO NOTHING""",
        (source["id"], to_pot_id, amount, note, datetime.now().isoformat(timespec="seconds"), client_id),
    )
    if not cur.rowcount:
        return "duplicate"

    # Guarded debit: the INSERT above is the idempotency gate, but two requests
    # can still pass the balance check concurrently (gunicorn runs multiple
    # threads against one SQLite file). Re-check inside the UPDATE and abandon
    # without committing if the pot was drained in between, so it can't overdraw.
    debit = db.execute(
        "UPDATE pots SET balance = balance - ? WHERE id = ? AND balance >= ?",
        (amount, source["id"], amount),
    )
    if not debit.rowcount:
        db.rollback()  # drop the movement row too; nothing has moved
        return "insufficient"

    if to_pot_id is not None:
        db.execute("UPDATE pots SET balance = balance + ? WHERE id = ?", (amount, to_pot_id))
    db.commit()
    return "ok"


def _retention_floor(today, retain_months=12):
    """First day of the oldest month retention still keeps — catch-up never
    looks further back, since `prune_old_transactions` has deleted older spends
    and a sweep there would blindly bank the full budget."""
    month_index = today.month - retain_months
    year = today.year
    while month_index <= 0:
        month_index += 12
        year -= 1
    return date(year, month_index, 1)


def _category_created_date(category):
    """A category's created_at as a date, or None for legacy rows created before
    the column existed (those keep the old single-window behavior)."""
    try:
        raw = category["created_at"]
    except (IndexError, KeyError):
        raw = None
    return date.fromisoformat(raw[:10]) if raw else None


def _week_windows(start_day, last_start):
    """Every completed Monday-Sunday window from the week containing `start_day`
    through the one beginning at `last_start`, oldest first."""
    d = start_day - timedelta(days=start_day.weekday())
    last = date.fromisoformat(last_start)
    while d <= last:
        yield d.isoformat(), (d + timedelta(days=7)).isoformat()
        d += timedelta(days=7)


def _month_windows(start_day, last_start):
    """Every completed calendar month from `start_day`'s month through the one
    beginning at `last_start`, oldest first."""
    y, m = start_day.year, start_day.month
    last = date.fromisoformat(last_start)
    while (y, m) <= (last.year, last.month):
        ws = date(y, m, 1)
        we = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        yield ws.isoformat(), we.isoformat()
        m += 1
        if m > 12:
            m = 1
            y += 1


def run_close_day(db, today):
    """Bank every completed, not-yet-swept weekly/monthly window.

    Catch-up safe: on any day it sweeps the most recent completed window *and*
    any earlier ones that were missed, so a forgotten 1st (or Monday) doesn't
    cost a period. Windows are bounded below by the category's `created_at`
    (never bank a period before the category existed) and by the retention
    floor. Legacy categories with no `created_at` keep the old single-window
    behavior, so deploying this can't retroactively re-bank their history.

    Returns the total moved into the Auto-saved pot; the caller commits."""
    moved_total = 0
    floor = _retention_floor(today).isoformat()
    week_last_start = prev_week_bounds(today)[0]
    month_last_start = prev_month_bounds(today)[0]

    weekly = db.execute("SELECT * FROM categories WHERE period = 'weekly'").fetchall()
    for c in weekly:
        created = _category_created_date(c)
        if created is None:
            ws, we = prev_week_bounds(today)
            moved_total += sweep_unspent(db, c, ws, we, strict_key=False)
        else:
            start = max(created, _retention_floor(today))
            for ws, we in _week_windows(start, week_last_start):
                moved_total += sweep_unspent(db, c, ws, we, strict_key=True)

    monthly = db.execute("SELECT * FROM categories WHERE period = 'monthly'").fetchall()
    for c in monthly:
        created = _category_created_date(c)
        if created is None:
            ws, we = prev_month_bounds(today)
            moved_total += sweep_unspent(db, c, ws, we, strict_key=False)
        else:
            start = max(created, _retention_floor(today))
            for ws, we in _month_windows(start, month_last_start):
                moved_total += sweep_unspent(db, c, ws, we, strict_key=True)

    return moved_total


@app.route("/close-day", methods=["POST"])
def close_day():
    """Manual trigger for the 'unspent scheduled amount moves to savings' rule,
    plus a data-retention sweep that deletes transactions older than 1 year.
    In production this is meant to run once daily via a scheduled job (e.g. Fly.io cron).

    Weekly categories bank the week that just ended, monthly categories the
    month that just ended — the same schedule for every category of that
    period, regardless of its own `due_day` (which is only a bill-due reminder
    and the DUE TODAY badge). It is catch-up safe (see `run_close_day`), so it
    does not have to be called on a Monday or the 1st."""
    db = get_db()
    prune_old_transactions(retain_months=12)
    today = date.today()

    moved_total = run_close_day(db, today)
    db.commit()

    if moved_total:
        flash(f"Swept {format_rp(moved_total)} of unspent budget into Auto-saved (Unspent).", "success")
    else:
        flash("Nothing to sweep — every completed window is already banked.", "info")

    return redirect(url_for("dashboard"))


@app.route("/savings")
def savings():
    db = get_db()
    today = date.today()

    pots = db.execute("SELECT * FROM pots ORDER BY id").fetchall()

    def preview_for(period, window_start, window_end):
        rows = db.execute(
            "SELECT * FROM categories WHERE period = ? ORDER BY sort_order, id", (period,)
        ).fetchall()
        preview = []
        for c in rows:
            spent = spent_for_category(db, c["id"], window_start, window_end)
            leftover = max(0, c["budget_amount"] - spent)
            preview.append({
                "name": c["name"],
                "budget": c["budget_amount"],
                "spent": spent,
                "leftover": leftover,
            })
        return preview

    week_start, week_end = week_bounds(today)
    weekly_preview = preview_for("weekly", week_start, week_end)

    month_start, month_end = month_bounds(today)
    monthly_preview = preview_for("monthly", month_start, month_end)

    # Chart: saved by expense — all-time auto-save total per category, ranked.
    category_rows = db.execute(
        """SELECT c.name AS name, SUM(t.amount) AS total
           FROM transactions t
           JOIN categories c ON c.id = t.category_id
           WHERE t.kind = 'auto_save'
           GROUP BY t.category_id
           ORDER BY total DESC"""
    ).fetchall()
    by_category = [{"name": r["name"], "total": r["total"]} for r in category_rows]
    CATEGORY_CHART_CAP = 10
    if len(by_category) > CATEGORY_CHART_CAP:
        other_total = sum(c["total"] for c in by_category[CATEGORY_CHART_CAP:])
        by_category = by_category[:CATEGORY_CHART_CAP]
        by_category.append({"name": "Other", "total": other_total})
    category_max = max((c["total"] for c in by_category), default=0)
    for c in by_category:
        c["pct"] = round(c["total"] / category_max * 100) if category_max else 0

    # Chart: saved by month — trailing 12 months, oldest to newest.
    by_month = []
    for m in trailing_months(retain_months=12):
        by_month.append({
            "label": m["label"],
            "full_label": m["full_label"],
            "total": auto_save_for_range(db, m["start"], m["end"]),
        })
    month_max = max((m["total"] for m in by_month), default=0)
    for m in by_month:
        m["pct"] = round(m["total"] / month_max * 100) if month_max else 0

    # "Available to set aside right now" — the same figure the dashboard hero
    # shows, so the prefilled deposit amount can never exceed what is actually
    # unallocated this month.
    month_start, month_end_excl = month_bounds(today)
    income = total_scheduled_income(db) + custom_income_for_range(db, month_start, month_end_excl)
    scheduled_total = sum(
        monthly_equivalent(c["budget_amount"], c["period"], today.year, today.month)
        for c in db.execute("SELECT budget_amount, period FROM categories")
    )
    manual_saved = pot_deposit_for_range(db, month_start, month_end_excl)
    withdrawn = pot_withdrawn_for_range(db, month_start, month_end_excl)
    custom_expense = custom_expense_for_range(db, month_start, month_end_excl)
    available = income - scheduled_total - manual_saved + withdrawn - custom_expense

    movements = get_pot_movements(db, limit=20)

    history = db.execute(
        """SELECT t.created_at AS created_at, t.amount AS amount,
                  c.name AS category_name, c.period AS period
           FROM transactions t
           JOIN categories c ON c.id = t.category_id
           WHERE t.kind = 'auto_save'
           ORDER BY t.created_at DESC
           LIMIT 20"""
    ).fetchall()

    return render_template(
        "savings.html",
        pots=pots,
        weekly_preview=weekly_preview,
        monthly_preview=monthly_preview,
        weekly_total=sum(p["leftover"] for p in weekly_preview),
        monthly_total=sum(p["leftover"] for p in monthly_preview),
        next_monday_label=next_monday(today).strftime("%d %b").upper(),
        next_month_first_label=next_month_first(today).strftime("%d %b").upper(),
        by_category=by_category,
        by_month=by_month,
        history=history,
        movements=movements,
        available=max(0, available),
        manual_saved=manual_saved,
        withdrawn=withdrawn,
        today=today.strftime("%d %b %Y").upper(),
    )


@app.route("/savings/deposit", methods=["POST"])
def add_pot_deposit():
    """Move money the user has on hand right now into a savings pot by hand,
    separately from the /close-day sweep."""
    db = get_db()
    pot_id = request.form.get("pot_id", type=int)
    amount = clamp_amount(request.form.get("amount", type=int))
    note = request.form.get("note", "").strip()
    client_id = request.form.get("client_id") or None

    pot = db.execute("SELECT id, name FROM pots WHERE id = ?", (pot_id,)).fetchone() if pot_id else None

    if not amount:
        flash("Enter a valid amount to set aside.", "error")
    elif not pot:
        flash("Pick a pot to set the money aside in.", "error")
    else:
        cur = db.execute(
            """INSERT INTO pot_deposits (pot_id, amount, note, created_at, client_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(client_id) WHERE client_id IS NOT NULL DO NOTHING""",
            (pot["id"], amount, note, datetime.now().isoformat(timespec="seconds"), client_id),
        )
        # Only credit the pot when the row was actually inserted — a replayed
        # offline submission hits the client_id conflict and must not move the
        # money a second time, since pots.balance is a stored running total.
        if cur.rowcount:
            db.execute(
                "UPDATE pots SET balance = balance + ? WHERE id = ?", (amount, pot["id"])
            )
            flash(f"Set aside {format_rp(amount)} into {pot['name']}.", "success")
        db.commit()

    return redirect(url_for("savings"))


@app.route("/savings/deposit/<int:deposit_id>/delete", methods=["POST"])
def delete_pot_deposit(deposit_id):
    """Undo a manual deposit: take the money back out of the pot's stored
    balance as well as dropping the row, or the two would drift apart."""
    db = get_db()
    row = db.execute(
        "SELECT pot_id, amount FROM pot_deposits WHERE id = ?", (deposit_id,)
    ).fetchone()

    if row:
        # Delete first; only debit the pot if this request removed the row, so a
        # concurrent/duplicate undo can't debit it twice.
        deleted = db.execute("DELETE FROM pot_deposits WHERE id = ?", (deposit_id,))
        if deleted.rowcount:
            db.execute(
                "UPDATE pots SET balance = balance - ? WHERE id = ?", (row["amount"], row["pot_id"])
            )
            db.commit()
            flash(f"Reverted {format_rp(row['amount'])} — money is back in available.", "success")

    return redirect(url_for("savings"))


@app.route("/savings/allocate", methods=["POST"])
def allocate_savings():
    """Move money between pots, or from a pot back to available/remaining
    balance. `to_pot_id` blank means 'available'. A transfer (pot -> pot) only
    reshuffles pots.balance; a withdrawal (pot -> available) is also added back
    to the dashboard's available/cash_left figures, since it is money returning
    to this month's unallocated cash."""
    db = get_db()
    from_pot_id = request.form.get("from_pot_id", type=int)
    # Blank destination means "back to available"; anything else must be a real
    # positive pot id. A malformed value is passed through as -1 (rejected by
    # apply_pot_movement) rather than being coerced into a withdrawal.
    to_raw = request.form.get("to_pot_id", "").strip()
    if to_raw == "":
        to_pot_id = None
    else:
        try:
            to_pot_id = int(to_raw)
        except ValueError:
            to_pot_id = -1
    amount = clamp_amount(request.form.get("amount", type=int))
    note = request.form.get("note", "").strip()
    client_id = request.form.get("client_id") or None

    result = apply_pot_movement(db, from_pot_id, to_pot_id, amount, note, client_id)
    amount_label = format_rp(amount) if amount else "that amount"

    if result == "ok":
        source_name = db.execute("SELECT name FROM pots WHERE id = ?", (from_pot_id,)).fetchone()["name"]
        if to_pot_id:
            dest_name = db.execute("SELECT name FROM pots WHERE id = ?", (to_pot_id,)).fetchone()["name"]
            flash(f"Transferred {amount_label} from {source_name} to {dest_name}.", "success")
        else:
            flash(f"Moved {amount_label} from {source_name} back to available.", "success")
    elif result == "insufficient":
        flash(f"Not enough in the source pot to move {amount_label}.", "error")
    elif result == "same_pot":
        flash("Source and destination pots must be different.", "error")
    elif result == "unknown_pot":
        flash("That pot no longer exists — pick another.", "error")
    elif result == "invalid":
        flash("Enter a valid amount and a destination pot.", "error")
    # "duplicate" is an idempotent replay (e.g. a retried offline submit): the
    # money already moved, so stay quiet rather than alarming the user.

    return redirect(url_for("savings"))


@app.route("/savings/movement/<int:movement_id>/delete", methods=["POST"])
def delete_pot_movement(movement_id):
    """Undo a transfer or withdrawal: put the money back where it came from
    (and take it off the destination pot, if there was one) as well as dropping
    the row, or the stored balances would drift apart."""
    db = get_db()
    row = db.execute(
        "SELECT from_pot_id, to_pot_id, amount FROM pot_movements WHERE id = ?",
        (movement_id,),
    ).fetchone()

    if row:
        # Delete first and only move money if this request actually removed the
        # row: a concurrent/duplicate undo would otherwise credit the pot twice.
        deleted = db.execute("DELETE FROM pot_movements WHERE id = ?", (movement_id,))
        if deleted.rowcount:
            db.execute(
                "UPDATE pots SET balance = balance + ? WHERE id = ?",
                (row["amount"], row["from_pot_id"]),
            )
            if row["to_pot_id"] is not None:
                db.execute(
                    "UPDATE pots SET balance = balance - ? WHERE id = ?",
                    (row["amount"], row["to_pot_id"]),
                )
            db.commit()
            flash(f"Reverted a {format_rp(row['amount'])} pot movement.", "success")

    return redirect(url_for("savings"))


@app.route("/trends")
def trends():
    db = get_db()
    cash_flow = cash_flow_chart(db)
    trajectory = savings_trajectory_chart(db)
    months = trend_months()
    trend_range_label = (
        f"{months[0]['full_label']} – {months[-1]['full_label']}" if months else ""
    )
    return render_template(
        "trends.html",
        cash_flow=cash_flow,
        trajectory=trajectory,
        trend_range_label=trend_range_label,
        today=date.today().strftime("%d %b %Y").upper(),
    )


@app.route("/income")
def income():
    db = get_db()
    start, end = month_bounds()

    sources = db.execute(
        "SELECT * FROM income_sources ORDER BY sort_order, id"
    ).fetchall()
    scheduled_total = total_scheduled_income(db)

    custom_entries = db.execute(
        """SELECT * FROM custom_incomes
           WHERE created_at >= ? AND created_at < ?
           ORDER BY created_at DESC""",
        (start, end),
    ).fetchall()
    custom_total = custom_income_for_range(db, start, end)

    return render_template(
        "income.html",
        sources=sources,
        scheduled_total=scheduled_total,
        custom_entries=custom_entries,
        custom_total=custom_total,
        total_income=scheduled_total + custom_total,
        today=date.today().strftime("%d %b %Y").upper(),
    )


@app.route("/income/source/add", methods=["POST"])
def add_income_source():
    db = get_db()
    name = request.form.get("name", "").strip()
    amount = clamp_amount(request.form.get("amount", type=int))
    due_day = request.form.get("due_day", type=int)  # optional, None if blank
    client_id = request.form.get("client_id") or None

    if name and amount and amount > 0:
        if due_day is not None and not (1 <= due_day <= 28):
            due_day = None  # ignore invalid day-of-month instead of erroring

        max_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM income_sources"
        ).fetchone()["m"]

        db.execute(
            """INSERT INTO income_sources (name, amount, due_day, sort_order, client_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(client_id) WHERE client_id IS NOT NULL DO NOTHING""",
            (name, amount, due_day, max_order + 1, client_id),
        )
        db.commit()

    return redirect(url_for("income"))


@app.route("/income/custom/add", methods=["POST"])
def add_custom_income():
    db = get_db()
    label = request.form.get("label", "").strip()
    amount = clamp_amount(request.form.get("amount", type=int))
    client_id = request.form.get("client_id") or None

    if amount and amount > 0:
        db.execute(
            """INSERT INTO custom_incomes (label, amount, created_at, client_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(client_id) WHERE client_id IS NOT NULL DO NOTHING""",
            (label or "Custom Income", amount, datetime.now().isoformat(timespec="seconds"), client_id),
        )
        db.commit()

    return redirect(url_for("income"))


if __name__ == "__main__":
    init_db()
    # debug=True binds a remote-code-execution console (the Werkzeug
    # debugger) on top of a server listening on 0.0.0.0 — opt in explicitly.
    debug = os.environ.get("LEDGER_DEBUG") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug, threaded=True)
else:
    # Run on every gunicorn/Fly.io boot, not just when ledger.db is missing:
    # init_db() is idempotent (CREATE TABLE IF NOT EXISTS, and the pots seed
    # only fires on an empty table), and it is the only place schema changes
    # are applied. Gating it on a missing file meant a redeploy that added a
    # table — pot_deposits, say — would leave an existing volume's database
    # without it and 500 on the first query.
    init_db()
