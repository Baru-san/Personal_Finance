# PROJECT.md — Ledger product specification

> Status legend: **Implemented** = present in source and covered by tests or
> manually verified; **Unverified** = present in source but not recently
> exercised; **Planned** = documented intention, not built.

## 1. Product summary

Ledger is a single-user, self-hosted personal finance tracker with a retro
terminal aesthetic. It is designed for one person tracking a household budget
on a personal server, a free PythonAnywhere account, or a small Fly.io machine.

Core promises:

- Scheduled **budget categories** (monthly or weekly) with a budget amount.
- **Spending** logged against a category, plus **one-off expenses** outside any
  category.
- **Recurring income** and **one-off income**.
- **Auto-save**: leftover budget from completed weeks/months is swept into the
  "Auto-saved (Unspent)" pot by `/close-day`.
- **Savings pots** with manual deposits, pot-to-pot transfers, withdrawals back
  to available cash, and undo.
- A month-by-month **ledger** and an editable **journal**.
- A **wishlist** of things to buy, with priority and manual savings progress.
- **Trends**: income vs spend per month and cumulative auto-saved.
- **Offline support**: installable PWA, cached app shell, and a queued write
  path for add forms.
- **Single shared password**; no accounts, roles, or multi-tenancy.

Non-goals:

- Multi-user or per-account data.
- Bank/API integrations or automatic transaction import.
- Investment/portfolio tracking.
- A built-in scheduler (an external cron/task triggers `/close-day`).
- A JSON/REST API for third-party clients.

## 2. Users and environment

- **Primary user**: the instance owner. One person, one shared password.
- **Deployment targets**:
  - Local development (`python3 app.py`, port 5000).
  - PythonAnywhere free tier via WSGI (`wsgi_pythonanywhere.py`).
  - Fly.io via Docker + gunicorn on port 8080 with a persistent volume.
- **Data sensitivity**: all financial data lives in one SQLite file. The app
  has no telemetry and makes no third-party requests.

## 3. User flows

### 3.1 First run and authentication

1. On boot with no password configured, the process prints a one-time
   `/setup?token=…` link to the console. Remote visitors without the token get
   `403`.
2. The owner opens the link, chooses a password (minimum 8 characters). It is
   stored as a one-way hash in `.password_hash` (or supplied via
   `LEDGER_PASSWORD_HASH`). The owner is logged in immediately.
3. Future visits require the password at `/login`.
4. Logout clears the session and asks the browser to purge the cached PWA
   shell so a shared device does not keep showing balances.

### 3.2 Set up income

1. Go to **Income**.
2. Add recurring sources (name, amount, optional arrival day 1–28). These count
   in every month.
3. Add one-off custom income (optional label, amount). It counts only in the
   month it was created.

### 3.3 Set up budget categories

1. On **Dashboard**, use **Add Scheduled Category**: name, budget, period
   (monthly/weekly), optional due day (1–28; ignored for weekly).
2. Each category card shows spend vs budget for its current window, a progress
   bar, status (`UNTOUCHED`, `ON TRACK`, `COMPLETE`, `OVER BUDGET`,
   `DUE TODAY`), and the window (month name or week range).
3. Categories can be edited inline. Changing to weekly clears the due day.

### 3.4 Log spending

- **Against a budget**: use the SPEND box on a category card.
- **One-off expense**: use **Log One-Off Expense** on the Dashboard.
- Both forms are offline-queued and carry a fresh `client_id` per submit.
- Dashboard figures update on the next page load (the offline path applies only
  a limited optimistic update to the affected category card and activity log).

### 3.5 Daily close / auto-save

1. `/close-day` is triggered by the dashboard button or an external scheduler.
2. It sweeps each completed, not-yet-swept weekly/monthly window into the
   Auto-saved (Unspent) pot.
3. It is **catch-up safe**: a missed Monday/1st is banked on the next run.
4. It is **idempotent**: each `(category, window_start)` is banked at most once
   via `transactions.sweep_key`.
5. It also prunes transactions older than 12 months.

### 3.6 Savings and pots

- Three pots are seeded: **Emergency Fund**, **Sinking Fund**, and
  **Auto-saved (Unspent)**.
- **Set aside unallocated cash**: move available money into any pot. Prefilled
  with the current month's unallocated amount.
- **Allocate a pot**: move money from a pot to another pot, or back to
  available/remaining balance.
- **Undo**: every deposit and movement row has an UNDO button that reverses
  both the audit row and the pot balance.
- **Important semantics**:
  - A deposit or withdrawal changes `available` and `cash_left`.
  - A pot-to-pot transfer changes only pot balances, not available.
  - `pots.balance` is a stored running total, not derived.

### 3.7 Ledger and journal editing

1. **Ledger** shows all entries (spends, auto-saves, custom income) for the
   selected month, newest first, with totals.
2. **Edit Journal** shows the same month with editable amount/note fields for
   `transactions` rows only. Custom income/expense rows are read-only there.
3. Editing a spend changes it directly; lowering acts as a refund, raising as
   an extra charge.
4. Editing an `auto_save` row also adjusts the Auto-saved pot by the delta.
   The update is guarded against stale concurrent edits.

### 3.8 Trends

- Net cash flow: income vs spend per month, from `TREND_START_MONTH`
  (`2026-09`) through the current month.
- Cumulative auto-saved: running total of `auto_save` transactions over the
  same window.
- Scheduled income is a live snapshot applied to every month; only custom
  income varies by month. The page labels this explicitly.
- The dashboard's day-by-day spending trend is laid across the whole current
  calendar month, so early-month data does not stretch edge to edge.

### 3.9 Offline use

1. The service worker precaches the app shell and serves navigations
   network-first with a cached fallback.
2. Add forms intercepted by `offline.js` POST immediately. On failure, the
   submission is stored in IndexedDB with its `client_id` and replayed when
   connectivity returns.
3. A banner/topbar indicator shows queued count and session-expired state.
4. Edit, delete, undo, and close-day forms are **not** queued.

### 3.10 Wishlist

1. Go to **Wishlist**.
2. Add an item: name, estimated price, priority (high/medium/low), optional note
   and product link, and optionally how much is already saved.
3. Each active card shows the price, saved amount, progress bar, remaining
   amount, and whether it is **READY** (saved ≥ price).
4. Use **EDIT** to correct the price or update saved progress, **MARK BOUGHT**
   to archive an item, or **DELETE** to remove it.
5. Archived items appear under **Bought / Archived** and can be restored.
6. The add form is offline-queued and idempotent via `client_id`; edits,
   buy/restore, and delete are online-only.
7. **Progress is a manual tracker.** It does not move money, change pots, or
   affect available cash / money left.

## 4. Financial definitions

All amounts are integer Rupiah.

| Figure | Formula / meaning |
|---|---|
| `scheduled_income` | Sum of all `income_sources.amount` (same every month). |
| `custom_income` | Sum of `custom_incomes` created in the month. |
| `income` | `scheduled_income + custom_income`. |
| `total_scheduled` | Sum of category budgets for the month; weekly budgets are multiplied by the number of Mondays in the month. |
| `manual_saved` | Sum of `pot_deposits` in the month. |
| `withdrawn` | Sum of `pot_movements` to available (`to_pot_id IS NULL`) in the month. |
| `custom_expense` | Sum of `custom_expenses` in the month. |
| `available` | `income − total_scheduled − manual_saved + withdrawn − custom_expense`. Reserves every category's full budget whether spent or not. Shown as **Available This Month**. |
| `total_spent` | Category spends in the month + `custom_expense`. |
| `cash_left` | `income − total_spent − manual_saved + withdrawn`. Actual money left; shown as **Money Left**. |
| Category `pct` | `min(100, round(spent / budget * 100))`, or 0 when budget is 0. |
| Category status | `OVER BUDGET` if spent > budget; `COMPLETE` if equal; `DUE TODAY` if due day matches today and not complete; `UNTOUCHED` if spent 0; else `ON TRACK`. |
| Wishlist `saved_amount` | Manual tracker only. Not part of any financial figure and does not affect pots or cash. |

## 5. Feature status

| Feature | Status | Notes |
|---|---|---|
| Shared-password auth, setup wizard, lockout | Implemented | Verified by `AuthTests`. |
| Dashboard: categories, income, cash flow, trend | Implemented | Tests cover totals and chart geometry. |
| Add/edit categories, weekly/monthly | Implemented | Tests cover add; edit has no dedicated test. |
| Add spend / custom expense | Implemented | Offline replay covered. |
| Income sources / custom income | Implemented | Indirectly exercised by seed tests. |
| Wishlist with priority + manual progress | Implemented | `WishlistTests`. |
| Catch-up safe `/close-day` + 12-month pruning | Implemented | `CloseDayCatchUpTests`. |
| Savings deposits / allocations / undo | Implemented | `SavingsAllocationTests`. |
| Idempotent savings add forms + in-flight guard | Implemented | Implemented in working tree; JS guard not unit-tested. |
| Atomic auto-save journal edit | Implemented | `JournalEditIdempotencyTests`. |
| Ledger / journal month selection | Implemented | Templated; no dedicated route tests. |
| Trends anchored to 2026-09 | Implemented | `TrendWindowTests`; date-dependent skips. |
| PWA + service worker caching | Implemented | Not covered by automated tests. |
| Offline IndexedDB queue + replay | Implemented | Not covered by automated JS tests. |
| Dummy seed script | Implemented | `DummySeedTests`. |
| In-app scheduler | Planned / non-goal | Use external cron or `close_day_task.py`. |
| Automated browser/JS tests | Planned | Manual verification only today. |

## 6. Current open work

See `docs/STATE.md` for the authoritative, dated snapshot of progress, known
issues, and next tasks.
