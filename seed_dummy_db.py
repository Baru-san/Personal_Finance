"""Create a throwaway, richly populated database for local testing.

Usage:
    python seed_dummy_db.py
    DB_PATH=dummy.db LEDGER_DISABLE_AUTH=1 python app.py

Writes to $DB_PATH (default: ./dummy.db), which .gitignore excludes — the real
ledger.db is never touched. Re-running rebuilds the dummy from scratch, so it
always matches the current schema. Data is generated relative to today, so the
dashboard/trends/ledger stay populated no matter when it runs.
"""
import os
import sqlite3
import sys
from datetime import date, datetime, time, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "dummy.db")

# This script deletes and rebuilds its target, so only ever point it at a
# dummy*.db file. Anything else (the real ledger.db, or a mis-set DB_PATH)
# needs an explicit opt-in, or a typo could wipe real data.
TARGET_BASENAME = os.path.basename(TARGET)
if not TARGET_BASENAME.startswith("dummy") and os.environ.get("LEDGER_SEED_FORCE") != "1":
    sys.exit(
        f"Refusing to seed {TARGET!r}: target must be a dummy*.db file "
        "(set LEDGER_SEED_FORCE=1 to override)."
    )


def ts(d, hour=9, minute=30):
    return datetime.combine(d, time(hour, minute)).isoformat(timespec="seconds")


def add_days(d, days):
    return d + timedelta(days=days)


def month_last_day(d):
    return (date(d.year + (d.month == 12), (d.month % 12) + 1, 1) - timedelta(days=1)).day


# Point the app at the dummy file *before* importing it: `import app` runs
# init_db(), which creates the schema on DB_PATH.
os.environ["DB_PATH"] = TARGET
os.environ["LEDGER_DISABLE_AUTH"] = "1"

# Deterministic rebuild: drop any previous dummy (and SQLite sidecars).
for suffix in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(TARGET + suffix)
    except FileNotFoundError:
        pass

import app as A  # noqa: E402  (import after DB_PATH is set, on purpose)

con = sqlite3.connect(TARGET)
con.row_factory = sqlite3.Row
con.execute("PRAGMA foreign_keys = ON")

today = date.today()
cur_start = date.fromisoformat(A.month_bounds(today)[0])
prev_start = date.fromisoformat(A.prev_month_bounds(today)[0])
prev_last = date.fromisoformat(A.prev_month_bounds(today)[1]) - timedelta(days=1)
week_start = date.fromisoformat(A.week_bounds(today)[0])
week_end = date.fromisoformat(A.week_bounds(today)[1])

# ---------- income ----------
con.executemany(
    "INSERT INTO income_sources (name, amount, due_day, sort_order) VALUES (?, ?, ?, ?)",
    [
        ("Salary", 8_000_000, 25, 1),
        ("Side Gig", 1_500_000, 10, 2),
    ],
)

con.executemany(
    "INSERT INTO custom_incomes (label, amount, created_at) VALUES (?, ?, ?)",
    [
        ("Freelance Project", 2_000_000, ts(add_days(today, -3))),
        ("Tax Refund", 750_000, ts(prev_start + timedelta(days=8))),
    ],
)

# ---------- categories ----------
category_specs = [
    ("Rent", 2_500_000, 1, "monthly"),
    ("Utilities", 400_000, 15, "monthly"),
    ("Internet", 350_000, 20, "monthly"),
    ("Dining", 600_000, None, "monthly"),
    ("Groceries", 600_000, None, "weekly"),
    ("Transport", 250_000, None, "weekly"),
]
for i, (name, budget, due, period) in enumerate(category_specs, start=1):
    con.execute(
        "INSERT INTO categories (name, budget_amount, due_day, sort_order, period, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name, budget, due, i, period, ts(prev_start)),
    )
cat_ids = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM categories")}


def add_spend(category, amount, when, note=""):
    con.execute(
        "INSERT INTO transactions (category_id, amount, kind, note, created_at) VALUES (?, ?, 'spend', ?, ?)",
        (cat_ids[category], amount, note, ts(when)),
    )


# ---------- last month: a full set of spends, so history/charts have data ----------
prev_spends = {
    "Rent": [(2_450_000, 2)],
    "Utilities": [(380_000, 14)],
    "Internet": [(350_000, 19)],
    "Dining": [(120_000, 5), (90_000, 12), (150_000, 22)],
    "Groceries": [(150_000, 3), (170_000, 10), (140_000, 17), (160_000, 24)],
    "Transport": [(55_000, 4), (60_000, 11), (50_000, 18), (65_000, 25)],
}
prev_last_day = month_last_day(prev_start)
for name, entries in prev_spends.items():
    for amount, day in entries:
        add_spend(name, amount, prev_start.replace(day=min(day, prev_last_day)), name.lower())

# ---------- this month: a rotating spread so the day-by-day trend is populated ----------
rotate = ["Rent", "Utilities", "Internet", "Dining", "Groceries", "Transport"]
rotate_amounts = [2_450_000, 120_000, 350_000, 85_000, 150_000, 55_000]
for i in range(12):
    offset = i % max(1, today.day)
    add_spend(rotate[i % len(rotate)], rotate_amounts[i % len(rotate_amounts)], add_days(today, -offset))

# This week's weekly-category activity (Monday -> today), so weekly cards show spend.
d = week_start
while d <= today and d < week_end:
    add_spend("Groceries", 150_000 + (d.day % 3) * 10_000, d, "weekly groceries")
    add_spend("Transport", 50_000, d, "commute")
    d = add_days(d, 1)

# ---------- one-off expenses ----------
con.executemany(
    "INSERT INTO custom_expenses (label, amount, created_at) VALUES (?, ?, ?)",
    [
        ("New Headphones", 450_000, ts(add_days(today, -2))),
        ("Gift", 150_000, ts(prev_start + timedelta(days=20))),
    ],
)

# ---------- auto-saved leftovers (last month) + their pot balance ----------
auto_saves = [
    ("Rent", 50_000),
    ("Utilities", 20_000),
    ("Dining", 240_000),
    ("Groceries", 150_000),
]
for name, amount in auto_saves:
    con.execute(
        "INSERT INTO transactions (category_id, amount, kind, note, created_at) VALUES (?, ?, 'auto_save', ?, ?)",
        (cat_ids[name], amount, "Unspent scheduled amount", ts(prev_last)),
    )
auto_saved_total = sum(amount for _, amount in auto_saves)

# ---------- pots: deposits + transfers/withdrawals, keeping balances in sync ----------
pots = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM pots")}
AUTO = pots["Auto-saved (Unspent)"]
EMERGENCY = pots["Emergency Fund"]
SINKING = pots["Sinking Fund"]


def credit(pot_id, amount):
    con.execute("UPDATE pots SET balance = balance + ? WHERE id = ?", (amount, pot_id))


credit(AUTO, auto_saved_total)

con.execute(
    "INSERT INTO pot_deposits (pot_id, amount, note, created_at) VALUES (?, ?, ?, ?)",
    (EMERGENCY, 500_000, "monthly top-up", ts(add_days(today, -2))),
)
credit(EMERGENCY, 500_000)

con.execute(
    "INSERT INTO pot_deposits (pot_id, amount, note, created_at) VALUES (?, ?, ?, ?)",
    (SINKING, 200_000, "car fund", ts(prev_start + timedelta(days=15))),
)
credit(SINKING, 200_000)

# Auto-saved -> Emergency Fund (transfer: only pot balances move).
con.execute(
    "INSERT INTO pot_movements (from_pot_id, to_pot_id, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
    (AUTO, EMERGENCY, 300_000, "sweep into emergency", ts(add_days(today, -1))),
)
con.execute("UPDATE pots SET balance = balance - ? WHERE id = ?", (300_000, AUTO))
credit(EMERGENCY, 300_000)

# Auto-saved -> available (withdrawal: also raises available/cash_left).
con.execute(
    "INSERT INTO pot_movements (from_pot_id, to_pot_id, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
    (AUTO, None, 100_000, "pulled back out", ts(today)),
)
con.execute("UPDATE pots SET balance = balance - ? WHERE id = ?", (100_000, AUTO))

con.commit()

summary = {
    table: con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    for table in (
        "categories", "transactions", "income_sources", "custom_incomes",
        "custom_expenses", "pot_deposits", "pot_movements",
    )
}
balances = con.execute("SELECT name, balance FROM pots ORDER BY id").fetchall()
con.close()

print(f"Seeded dummy database: {TARGET}")
for table, count in summary.items():
    print(f"  {table:<16} {count}")
for row in balances:
    print(f"  pot {row['name']}: {row['balance']}")
print()
print("Run it with:")
print(f"  DB_PATH={os.path.relpath(TARGET, BASE_DIR)} LEDGER_DISABLE_AUTH=1 python app.py")
