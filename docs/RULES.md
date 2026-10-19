# RULES.md — business rules, conventions, and constraints

## 1. Core financial rules

### 1.1 Money representation

- All money is **integer Rupiah**. Never use floats for storage or arithmetic.
- Route inputs are parsed as `int` and validated by `clamp_amount()`:
  `1 ≤ amount ≤ 1_000_000_000_000`.
- Formatting uses `format_rp()`: `Rp 1.234.567` (dot thousands separators).
- Offline.js collects the visible formatted value into a hidden plain-digit
  field, so the server receives an integer string.

### 1.2 Budget windows

- Monthly categories reset on the 1st of the calendar month.
- Weekly categories reset every **Monday**, and their window is Monday–Sunday.
- `due_day` (1–28) is only a reminder / "DUE TODAY" signal. It does **not**
  change the sweep schedule.
- Weekly categories ignore `due_day`; `normalize_category_period()` clears it.
- `total_scheduled` for a month multiplies weekly budgets by the number of
  Mondays in that month (`mondays_in_month`).

### 1.3 Category status

For the current window:

- `OVER BUDGET` — spent > budget.
- `COMPLETE` — spent == budget.
- `DUE TODAY` — spent < budget and `due_day == today.day`.
- `UNTOUCHED` — spent == 0 (and not due today).
- `ON TRACK` — anything else.

`pct = min(100, round(spent / budget * 100))`, or 0 if budget is 0.

### 1.4 Dashboard figures

- `available` (Available This Month):
  `income − total_scheduled − manual_saved + withdrawn − custom_expense`.
  This is **plan money**: it reserves the full scheduled budget whether or not
  it has been spent.
- `cash_left` (Money Left):
  `income − total_spent − manual_saved + withdrawn`.
  This is **actual money**: it subtracts only real category spending and
  one-off expenses (already inside `total_spent`), plus manual deposits, and
  adds withdrawals.
- Do not collapse these two figures into one. They intentionally differ.

### 1.5 Income

- `income_sources` are recurring and applied to every month at their current
  amount. There is no historical snapshot.
- `custom_incomes` count only in the month of their `created_at`.
- On Trends, this means scheduled income is a live rate applied retroactively;
  the page states this.

### 1.6 Auto-save / daily close

- `/close-day` banks each **completed** window's leftover:
  `budget − spend` (weekly: window budget; monthly: monthly budget).
- Catch-up behavior:
  - categories with `created_at` are swept for every missed completed window
    from `max(created_at, retention_floor)`.
  - legacy categories (`created_at IS NULL`) sweep only the most recent
    completed window, preserving old behavior.
- Idempotency: `transactions.sweep_key = window_start` plus the partial unique
  index on `(category_id, sweep_key)`. Only the winning insert credits the pot.
- The pre-check is a fast path; the unique index is the real race guard.
- `/close-day` also deletes `transactions` older than 12 months.
- Retention floor: catch-up never looks further back than retention keeps,
  because missing spend rows would overstate leftovers.

### 1.7 Savings pots

- `pots.balance` is a **stored running total**, never derived from rows.
- Seed pots: Emergency Fund, Sinking Fund, Auto-saved (Unspent).
- Money movements:
  - Deposit: available → pot. Insert `pot_deposits`, credit pot.
  - Transfer: pot → pot. Insert `pot_movements`, debit source, credit
    destination. Available unchanged.
  - Withdraw: pot → available. Insert `pot_movements` with `to_pot_id NULL`,
    debit source. Available/cash_left rise.
  - Undo: delete the audit row first, then reverse balances only if the delete
    actually removed a row.
- Only adjust a pot when the audit insert/delete returned `rowcount == 1`.
- Never allow a withdrawal/transfer to overdraw: re-check inside the guarded
  `UPDATE pots ... WHERE balance >= ?`.
- **Manual deposits/withdrawals** affect available; **transfers** do not.

### 1.8 Editing transactions

- Spend totals are computed live with `SUM`, so editing an amount is
  effectively a refund (lower) or extra charge (higher).
- `auto_save` edits must also adjust the Auto-saved pot by the delta.
- The edit must be atomic against concurrent edits: use
  `UPDATE ... WHERE id = ? AND amount = ?` with the originally-read amount,
  and roll back if `rowcount == 0`. A stale edit must not double-credit the pot.
- Journal editing only applies to `transactions` rows, not custom income or
  custom expense rows.

### 1.9 Idempotency

- Every write table has a nullable `client_id` and a partial unique index on
  non-NULL values.
- Offline-queued add forms generate a fresh UUID per submit.
- The server uses `ON CONFLICT(client_id) WHERE client_id IS NOT NULL DO NOTHING`.
- Only credit/move money when the insert actually happened.
- Do not replace the per-submit UUID with a static hidden field; legitimate
  repeat submissions would be silently dropped.

### 1.10 Wishlist

- Wishlist `saved_amount` is a **manual goal tracker**, not money. It must not
  read or write `pots`, `pot_deposits`, `pot_movements`, `available`, or
  `cash_left`.
- Goal progress is `remaining = max(0, price - saved_amount)` and
  `pct = min(100, round(saved_amount / price * 100))` (0 when price is 0).
- `price` goes through `clamp_amount()`; `saved_amount` is clamped to
  `0..AMOUNT_MAX`.
- Priority is one of `high`, `medium`, `low`; unknown values fall back to
  `medium`.
- Product links are stored/rendered only for `http://` or `https://` URLs.
- Marking bought archives the row (`bought_at`); it never creates an expense.
- Only the add form is offline-queued; edit/buy/restore/delete are online-only.

## 2. Coding conventions

- `app.py` is deliberately a single file; keep domain logic in functions above
  the routes.
- Raw SQL with `?`/named placeholders. **Never** interpolate runtime values.
  The only f-string SQL is the hardcoded `MIGRATED_TABLES` migration loop.
- Use `sqlite3.Row` access (`row["col"]`); do not rely on tuple positions.
- Routes should stay thin: validate → call a domain helper → commit → redirect.
- Templates are server-rendered Jinja. Prefer small inline vanilla JS over a
  new framework or build step.
- Use `url_for(...)` for links and form actions so static assets get the
  `?v=<mtime>` cache-buster.
- Timestamps: `datetime.now().isoformat(timespec="seconds")`.
- Date comparison: half-open ISO string ranges `>= start`, `< end`.
- Avoid adding dependencies unless absolutely necessary; the app intentionally
  ships only Flask, Flask-Compress, and gunicorn.
- `python` may not exist on this machine; use `python3`.

## 3. Scheme and migration rules

- `init_db()` is the only migration mechanism; it runs on every import/boot.
- Migrations must be additive and idempotent:
  - `CREATE TABLE IF NOT EXISTS`
  - `CREATE UNIQUE INDEX IF NOT EXISTS`
  - `ALTER TABLE ADD COLUMN` guarded by `PRAGMA table_info`
- Never rename/drop columns or tables in place.
- Never make `init_db()` depend on a file being missing; it must run on every
  boot so existing volumes get new objects.
- New write tables must be added to `MIGRATED_TABLES` **only** as a literal
  identifier, to get `client_id` plus the partial unique index.
- Keep legacy-null handling: rows with `created_at IS NULL` must not be swept
  retroactively.

## 4. Security constraints

- **Never commit secrets**: `.password_hash`, `.secret_key`, `ledger.db`,
  `dummy.db*`, logs, real `LEDGER_PASSWORD_HASH`, `SECRET_KEY`, and
  `CLOSE_DAY_TOKEN`.
- **Never set `LEDGER_DISABLE_AUTH=1` on a public/deployed host.** It disables
  every auth check and exposes all writes.
- **Never set `LEDGER_DEBUG=1` on a deployed host.** The Werkzeug debugger is
  remote code execution.
- `/setup` is protected by a console-printed high-entropy token so a remote
  visitor cannot claim the password first. It closes permanently once a hash
  exists.
- Sessions store a hash fingerprint, not a boolean; changing the password
  invalidates all sessions.
- `SESSION_COOKIE_SAMESITE="Lax"` is the CSRF mitigation. Do not weaken it.
- `SESSION_COOKIE_SECURE` must be on in production (`LEDGER_ENV=production`).
- `safe_next_path()` must reject protocol-relative and backslash paths to avoid
  open redirects.
- Unauthenticated POST must return a bare `401`, never a redirect. The offline
  queue would otherwise mistake a redirect-followed login page for success and
  delete the queued write.
- `X-Close-Day-Token` is compared with `hmac.compare_digest`.
- Do not add third-party analytics, CDNs, or telemetry.

## 5. Deployment constraints

- **One process / one machine.** SQLite has a single writer file. Gunicorn is
  configured as `--workers 1 --threads 4`; Fly is pinned to one machine/volume.
  Do not add multiple worker processes or scale Fly horizontally.
- `DB_PATH` must point at persistent storage on Fly (`/data/ledger.db`).
- Do not bake local data/secrets into the Docker image; `.dockerignore`
  excludes them.
- `close_day_task.py` exists for hosts that cannot make outbound HTTP requests
  (PythonAnywhere free tier). It must keep using Flask's test client and the
  same `/close-day` route so sweep logic cannot drift.
- Bump `CACHE_VERSION` in `static/sw.js` when changing the precache list or
  caching strategy.

## 6. Testing rules

- Use stdlib `unittest`; no new test dependency.
- The suite sets `DB_PATH` to a temp file before importing `app` and resets the
  database per test. Never touch the real `ledger.db`.
- Run the full suite before and after changes:
  `python3 -m unittest discover -s tests -v`
- Add tests for behavior changes, especially money movement, idempotency,
  migrations, auth, and date-window logic.
- There are no automated JS/browser tests. Manual verification is required for
  changes to `offline.js`, `sw.js`, templates, or auth; say so explicitly in
  the final report.

## 7. Git and change hygiene

- Do not commit unless explicitly asked.
- Before committing: inspect `git status`, `git diff`, and recent log; stage
  only intended files.
- Never commit generated databases, logs, `__pycache__`, or `.claude/`
  worktrees/settings.
- Update `docs/STATE.md` after any change that alters progress, known issues, or
  next tasks.
- Document non-obvious choices in `docs/DECISIONS.md`.
