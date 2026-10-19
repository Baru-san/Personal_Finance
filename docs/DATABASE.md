# DATABASE.md — schema, relationships, and migrations

## 1. Overview

- Engine: SQLite (stdlib `sqlite3`).
- File: `DB_PATH`, default `<repo>/ledger.db`.
- Connections: one per Flask request (`get_db()`), `row_factory = sqlite3.Row`,
  `PRAGMA foreign_keys = ON`.
- Transactions: Python's default deferred transactions; write routes call
  `db.commit()`, and money-moving helpers roll back on a lost guard.
- Money: always **integer Rupiah**. Never floats.
- Timestamps: ISO-8601 strings (`YYYY-MM-DDTHH:MM:SS` or date-only where noted).
  Month ranges use half-open comparisons `created_at >= start AND < end`.

## 2. Tables

### `categories`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `name` | TEXT NOT NULL | Display name. |
| `budget_amount` | INTEGER NOT NULL DEFAULT 0 | Per-window budget (week or month). |
| `due_day` | INTEGER NULL | 1–28; reminder only; `NULL` for flexible or weekly. |
| `sort_order` | INTEGER DEFAULT 0 | Dashboard ordering. |
| `period` | TEXT NOT NULL DEFAULT `'monthly'` | `'weekly'` or `'monthly'`. |
| `client_id` | TEXT NULL | Offline idempotency key. |
| `created_at` | TEXT NULL | When added; bounds catch-up sweeps. `NULL` = legacy. |

Monthly-equivalent budget = `budget_amount` for monthly, or
`budget_amount × Mondays in month` for weekly.

### `transactions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `category_id` | INTEGER NOT NULL → `categories(id)` ON DELETE CASCADE | |
| `amount` | INTEGER NOT NULL | Always positive. |
| `kind` | TEXT NOT NULL | `'spend'` or `'auto_save'`. |
| `note` | TEXT NULL | |
| `created_at` | TEXT NOT NULL | Drives month/window placement. |
| `client_id` | TEXT NULL | Offline idempotency key (spends only). |
| `sweep_key` | TEXT NULL | Auto-save only: window start date. |

### `pots`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `name` | TEXT NOT NULL UNIQUE | |
| `balance` | INTEGER NOT NULL DEFAULT 0 | **Stored running total**, not derived. |

Seeded on first `init_db()` when the table is empty:

1. `Emergency Fund` (0)
2. `Sinking Fund` (0)
3. `Auto-saved (Unspent)` (0)

Templates assume index 2 / name `Auto-saved (Unspent)` is the auto-save pot.

### `income_sources`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `name` | TEXT NOT NULL | |
| `amount` | INTEGER NOT NULL | Recurring amount, counted every month. |
| `due_day` | INTEGER NULL | Arrival day 1–28, optional. |
| `sort_order` | INTEGER DEFAULT 0 | |
| `client_id` | TEXT NULL | Offline idempotency key. |

There is no income history: the current source list is applied to every month
on the Trends page.

### `custom_incomes`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `label` | TEXT NULL | Defaults to `Custom Income`. |
| `amount` | INTEGER NOT NULL | Positive, one-off. |
| `created_at` | TEXT NOT NULL | Counted only in its own month. |
| `client_id` | TEXT NULL | Offline idempotency key. |

### `custom_expenses`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `label` | TEXT NULL | Defaults to `Custom Expense`. |
| `amount` | INTEGER NOT NULL | Positive, one-off. |
| `created_at` | TEXT NOT NULL | Counted only in its own month. |
| `client_id` | TEXT NULL | Offline idempotency key. |

### `pot_deposits`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `pot_id` | INTEGER NOT NULL → `pots(id)` ON DELETE CASCADE | |
| `amount` | INTEGER NOT NULL | Positive; available → pot. |
| `note` | TEXT NULL | |
| `created_at` | TEXT NOT NULL | |
| `client_id` | TEXT NULL | Offline idempotency key. |

### `pot_movements`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `from_pot_id` | INTEGER NOT NULL → `pots(id)` ON DELETE CASCADE | |
| `to_pot_id` | INTEGER NULL → `pots(id)` ON DELETE CASCADE | `NULL` = back to available. |
| `amount` | INTEGER NOT NULL | Positive. |
| `note` | TEXT NULL | |
| `created_at` | TEXT NOT NULL | |
| `client_id` | TEXT NULL | Offline idempotency key. |

### `wishlist_items`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `name` | TEXT NOT NULL | Item description. |
| `price` | INTEGER NOT NULL | Estimated/target price, positive integer Rupiah. |
| `saved_amount` | INTEGER NOT NULL DEFAULT 0 | Manual goal tracker; **does not move money**. |
| `priority` | TEXT NOT NULL DEFAULT `'medium'` | `'low'`, `'medium'`, or `'high'`. |
| `note` | TEXT NULL | Optional note. |
| `url` | TEXT NULL | Optional product link; only `http://`/`https://` are stored. |
| `created_at` | TEXT NOT NULL | When added. |
| `bought_at` | TEXT NULL | `NULL` = active; timestamp = archived as bought. |
| `client_id` | TEXT NULL | Offline idempotency key. |

`wishlist_items` has no foreign keys and participates in no money movement.
`saved_amount` is display-only progress toward `price`; it is not derived from
pots and never affects `available`, `cash_left`, or any pot balance.

## 3. Relationships

```
categories 1───* transactions
pots       1───* pot_deposits
pots       1───* pot_movements (from_pot_id)
pots       0..1─* pot_movements (to_pot_id, NULL = available)

income_sources   (independent)
custom_incomes   (independent, month-scoped by created_at)
custom_expenses  (independent, month-scoped by created_at)
wishlist_items   (independent, non-monetary)
```

There is no foreign key from income/expense rows to categories.

## 4. Indexes

Created by `init_db()`:

| Index | Definition | Purpose |
|---|---|---|
| `idx_<table>_client_id` | `UNIQUE (client_id) WHERE client_id IS NOT NULL` on each of `categories`, `transactions`, `income_sources`, `custom_incomes`, `custom_expenses`, `pot_deposits`, `pot_movements`, `wishlist_items` | Idempotent replay of offline writes. `NULL` is allowed repeatedly, so plain no-JS submissions are unaffected. |
| `idx_transactions_sweep` | `UNIQUE (category_id, sweep_key) WHERE sweep_key IS NOT NULL` | One auto-save sweep per category per window; race guard for `/close-day`. |

## 5. Invariants

1. **Pot balance is authoritative and stored.** Every row that moves money must
   update `pots.balance` in the same transaction, and only when the audit row
   insert/delete actually happened (`cur.rowcount`).
2. **Auto-save edits adjust the pot by the delta.** `apply_journal_edit()` uses
   `UPDATE transactions ... WHERE id = ? AND amount = ?`; a stale edit fails
   and rolls back instead of applying a second delta.
3. **Deposits** credit the pot and reduce available via `pot_deposits`.
4. **Withdrawals** debit the pot and increase available via
   `pot_movements.to_pot_id IS NULL`.
5. **Transfers** debit source and credit destination; available is unchanged.
6. **Undo** deletes the audit row first and adjusts balances only if the delete
   removed a row.
7. **Sweeps** insert with `sweep_key`; only the insert winner credits the pot.
8. **Retention** deletes `transactions` older than 12 months, but not
   `pot_deposits`, `pot_movements`, or custom income/expense.
9. **No negative balances** are enforced by application logic, not a CHECK
   constraint.

## 6. Migration conventions

- `init_db()` is the **only** migration mechanism. It runs on every import and
  every process boot (`app.py` bottom).
- All migrations must be **additive and idempotent**:
  - `CREATE TABLE IF NOT EXISTS`.
  - `CREATE UNIQUE INDEX IF NOT EXISTS`.
  - `ALTER TABLE ... ADD COLUMN` only after checking `PRAGMA table_info`.
  - Never rename or drop a column/table in place.
- `MIGRATED_TABLES` is a hardcoded tuple used to add `client_id` and its partial
  unique index. It is the only place f-string SQL is allowed, and every name is
  a literal. Never add a runtime-derived table name.
- Older code can ignore newly added columns/tables, so rollback is generally
  safe; back up the database before deploying.
- Legacy rows may have `created_at = NULL`; migration must preserve the old
  single-window sweep behavior for them.

## 7. Backup and recovery

- SQLite file plus sidecars: `ledger.db`, `-wal`, `-shm`, `-journal`.
- Fly.io: back up to `/data/ledger.db.bak-*` (volume survives deploys).
- PythonAnywhere/local: `cp ledger.db ledger.db.bak-$(date +%F)`.
- Restore by copying the backup over the live file while the app is stopped or
  reloaded.
- Never commit `ledger.db`, `dummy.db`, backups, or sidecar files.

## 8. Test database

Tests set `DB_PATH` to a fresh temp directory **before** importing `app`, then
call `ledger.init_db()` in `setUp()`. The real `ledger.db` is never touched.
`seed_dummy_db.py` refuses to overwrite non-`dummy*.db` targets unless
`LEDGER_SEED_FORCE=1`.
