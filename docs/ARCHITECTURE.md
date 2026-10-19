# ARCHITECTURE.md — components, layout, and data flow

## 1. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 (Docker image), stdlib `sqlite3` |
| Web framework | Flask 3.0.3 |
| Compression | Flask-Compress 1.24 |
| Production server | gunicorn 23.0.0: 1 worker, 4 threads |
| Database | SQLite, one file selected by `DB_PATH` |
| Templates | Jinja2, server-rendered |
| Frontend JS | Small vanilla-JS blocks; no framework/build step |
| Charts | Server-computed SVG geometry + `static/chart.js` hover behavior |
| Offline | Service worker (`static/sw.js`) + IndexedDB write queue (`static/offline.js`) |
| PWA | `static/manifest.json`, icons in `static/icons/` |
| Tests | Python stdlib `unittest` + Flask test client |

There is no Node toolchain, bundler, ORM, migration framework, or external
database.

## 2. Directory map

```
.
├── app.py                      # entire Flask app: config, auth, DB, domain logic, routes
├── close_day_task.py           # in-process daily-close runner (PythonAnywhere/cron)
├── seed_dummy_db.py            # rebuilds dummy.db with relative-to-today data
├── wsgi_pythonanywhere.py      # WSGI config template (never put real secrets in repo copy)
├── requirements.txt
├── Dockerfile                  # gunicorn image, non-root user, /data volume
├── fly.toml                    # Fly.io app, volume, env, single machine
├── .dockerignore / .gitignore
├── README.md                   # operator setup/deploy guide
├── CLAUDE.md                   # legacy detailed deep-dive (docs/ is maintained source)
├── AGENTS.md                   # mandatory agent entry point
├── docs/                       # maintained project memory
├── tests/
│   ├── __init__.py
│   └── test_app.py             # stdlib unittest suite
├── templates/
│   ├── dashboard.html          # /
│   ├── ledger.html             # /ledger
│   ├── journal.html            # /journal/<month>
│   ├── savings.html            # /savings
│   ├── trends.html             # /trends
│   ├── income.html             # /income
│   ├── wishlist.html           # /wishlist
│   ├── login.html              # /login
│   ├── setup.html              # /setup
│   └── _flash.html             # shared flash banner include
└── static/
    ├── style.css               # all styling (retro terminal theme)
    ├── offline.js              # SW registration, connectivity, IndexedDB queue, money input
    ├── sw.js                   # service worker (served at /sw.js)
    ├── chart.js                # bar fills + SVG line-chart crosshair/tooltip
    ├── manifest.json
    ├── offline.html
    ├── fonts/
    └── icons/
```

## 3. Application startup

- `app.py` reads configuration at import time:
  - `DB_PATH` (default `./ledger.db`)
  - `SECRET_KEY` (env, else persisted `.secret_key`, else generated and saved)
  - `LEDGER_PASSWORD_HASH` (env, else `.password_hash`)
  - `LEDGER_ENV` (`production` sets Secure cookie)
  - `LEDGER_DISABLE_AUTH` (development escape hatch only)
  - `CLOSE_DAY_TOKEN` (optional scheduler token)
  - `LEDGER_DEBUG` (only when run as `__main__`)
- `init_db()` runs on every import/boot, including under gunicorn. It creates
  missing tables and applies additive migrations. See `docs/DATABASE.md`.
- If no password is configured and auth is enabled, a one-time `SETUP_TOKEN`
  is printed to the console.

## 4. Request lifecycle

```
Browser
  │
  ▼
Flask before_request: require_login()
  │  - public endpoints: login, setup, static, service_worker
  │  - close_day may bypass with a matching X-Close-Day-Token
  │  - no password: redirect GETs to /setup, reject POSTs with 401
  │  - unauthenticated POST: bare 401 (never a redirect)
  │  - unauthenticated GET: redirect to /login?next=…
  ▼
Route handler
  │  get_db() → one sqlite3 connection per request on flask.g
  │  domain helpers run SELECT/INSERT/UPDATE with placeholders
  │  db.commit() for writes
  ▼
Jinja template render (or redirect)
  ▼
teardown_appcontext: close_db()
```

CSRF protection relies on `SESSION_COOKIE_SAMESITE="Lax"`, not tokens.

## 5. Domain logic (all in `app.py`)

| Group | Functions |
|---|---|
| Date windows | `month_bounds`, `week_bounds`, `prev_week_bounds`, `prev_month_bounds`, `period_bounds`, `next_monday`, `next_month_first`, `_retention_floor`, `_category_created_date`, `_week_windows`, `_month_windows` |
| Budget math | `mondays_in_month`, `monthly_equivalent`, `normalize_category_period` |
| Aggregation | `total_scheduled_income`, `custom_income_for_range`, `custom_expense_for_range`, `pot_deposit_for_range`, `pot_withdrawn_for_range`, `auto_save_for_range`, `spent_for_category`, `build_dashboard_data` |
| Charts | `_nice_ceil`, `line_chart_geometry`, `spending_trend_chart`, `cash_flow_chart`, `savings_trajectory_chart`, `trend_months`, `months_between`, `trailing_months`, `available_months`, `month_key_bounds` |
| Ledger | `ENTRY_UNION_SQL`, `ALL_ENTRIES_SQL`, `LEDGER_ENTRIES_SQL`, `get_ledger_for_month`, `POT_MOVEMENT_LOG_SQL`, `get_pot_movements` |
| Money movement | `sweep_unspent`, `apply_pot_movement`, `apply_journal_edit`, `run_close_day`, `prune_old_transactions` |
| Wishlist | `WISHLIST_PRIORITIES`, `_wishlist_priority`, `_wishlist_url`, `_wishlist_saved_amount`, `_wishlist_item_view`, `_wishlist_form_values` |
| Formatting | `format_rp`, `clamp_amount` |
| Validation | `safe_next_path`, `login_locked_out`, `register_failed_login`, `register_successful_login` |

## 6. Routes and templates

| Route | Template / result |
|---|---|
| `GET /` | `dashboard.html` |
| `GET /ledger` | `ledger.html` for selected/current month |
| `GET /journal/<month_key>` | `journal.html` for a valid retained month |
| `GET /savings` | `savings.html` |
| `GET /trends` | `trends.html` |
| `GET /income` | `income.html` |
| `GET /wishlist` | `wishlist.html` |
| `GET/POST /login` | `login.html` |
| `GET/POST /setup` | `setup.html` (requires console token) |
| `POST /logout` | redirect to `/login?logged_out=1` |
| `GET /sw.js` | `static/sw.js` with `Cache-Control: no-cache` |
| `POST /close-day` | dashboard redirect + flash |
| `POST /journal/entry/<id>/edit` | journal redirect |
| `POST /expense/add` | dashboard redirect |
| `POST /expense/custom/add` | dashboard redirect |
| `POST /category/add`, `/category/<id>/edit` | dashboard redirect |
| `POST /savings/deposit`, `/savings/allocate` | savings redirect |
| `POST /savings/deposit/<id>/delete`, `/savings/movement/<id>/delete` | savings redirect |
| `POST /income/source/add`, `/income/custom/add` | income redirect |
| `POST /wishlist/add` | wishlist redirect (offline-queued; `client_id` dedup) |
| `POST /wishlist/<id>/edit`, `/buy`, `/restore`, `/delete` | wishlist redirect |

Full contracts are in `docs/API.md`.

## 7. Data flow: dashboard

1. `categories` are read ordered by `sort_order, id`.
2. For each category, the current window is chosen by `period`:
   - monthly: current calendar month.
   - weekly: Monday–Sunday containing today.
3. `spent_for_category()` sums `transactions` where `kind = 'spend'`.
4. `total_scheduled` sums monthly-equivalent budgets; weekly budgets multiply
   by the number of Mondays in the month.
5. Income, deposits, withdrawals, and custom expenses are summed for the
   current month to derive `available` and `cash_left`.
6. Recent activity is a UNION of transactions, custom income, and custom
   expense, sorted by `created_at DESC`, limited to 8.
7. The trend chart is computed as SVG geometry and rendered server-side.

## 8. Data flow: `/close-day`

```
POST /close-day
  → prune_old_transactions(retain_months=12)
  → run_close_day(db, today)
      for each weekly category:
          if created_at is NULL (legacy): sweep previous week (loose check)
          else: sweep every missed week from max(created_at, retention floor)
                through the previous week (strict sweep_key)
      for each monthly category:
          same, using completed calendar months
      each sweep:
          skip if already banked
          leftover = budget − spent; skip if <= 0
          INSERT auto_save row with sweep_key = window_start
          only if rowcount: credit Auto-saved pot
  → commit
  → flash total moved
```

The `(category_id, sweep_key)` partial unique index is the race guard.

## 9. Data flow: savings money movement

### Deposit (`/savings/deposit`)

```
validate pot and amount
INSERT pot_deposits (client_id optional, ON CONFLICT DO NOTHING)
if rowcount:
    UPDATE pots SET balance = balance + amount
commit
```

### Allocate (`/savings/allocate` → `apply_pot_movement`)

```
validate amount, pot ids
if source.balance < amount: reject
INSERT pot_movements (ON CONFLICT client_id DO NOTHING)
if rowcount == 0: duplicate replay, stop
guarded UPDATE pots SET balance = balance - amount WHERE id=? AND balance >= amount
if rowcount == 0: rollback (insufficient race)
if destination pot: UPDATE destination balance += amount
commit
```

### Undo deposit / movement

Delete the row first; only adjust balances if the delete removed a row
(`rowcount`). This makes repeated undo a no-op.

### Wishlist (non-monetary)

```
GET /wishlist
  → active rows (bought_at IS NULL) ordered by priority then newest
  → archived rows (bought_at IS NOT NULL) newest first
  → per item: remaining = max(0, price - saved_amount),
             pct = min(100, round(saved_amount / price * 100))
  → totals = sums over active items only
```

`wishlist_items.saved_amount` is a manual goal tracker only. Nothing in this
flow reads or writes `pots`, `pot_deposits`, `pot_movements`, or `available`.

## 10. Data flow: offline write queue

```
form[data-offline-queue] submit
  → preventDefault
  → if form already submitting: return
  → disable submit buttons
  → client_id = uuid()
  → FormData + client_id → URLSearchParams
  → fetch(POST)
      success: navigate to response URL
      failure: queueAdd({clientId, url, params, kind, timestamp})
               optional optimistic category-card / activity-log update
               reset form, clear submitting state
  → on reconnect or page load: replayQueue()
      POST each record
      2xx and not a /login bounce → queueRemove
      401 or /login bounce → keep queued, mark session expired
      network failure → keep queued
```

Server routes dedup a replayed record with `client_id` partial unique indexes.

## 11. Frontend assets

- `static/offline.js`
  - Registers `/sw.js`.
  - Connectivity check via same-origin `HEAD /`.
  - IndexedDB queue (`ledger-offline-queue` / `writes`).
  - In-flight submit guard per form.
  - Money input formatting: visible input loses its `name`; a paired hidden
    field carries the plain digit string so the server always receives an
    integer.
- `static/sw.js`
  - `CACHE_VERSION` invalidates precache; bump when precache list/strategy
    changes.
  - Navigations: network-first, cache fallback, then `offline.html`.
  - `/static/*`: cache-first.
  - Non-GET requests are never intercepted.
  - Redirects are never cached (prevents caching the login page as a page).
- `static/chart.js`
  - `initPctBars()` applies `data-pct` to CSS custom properties.
  - `initLineChart()` reads `data-points` JSON from an SVG and drives the
    crosshair, dot, and tooltip.
- `static/style.css` — the complete theme and responsive layout.

## 12. Deployment topology

### Local

```
python3 app.py
  → init_db()
  → Flask dev server on 0.0.0.0:5000 (threaded)
```

### Fly.io / Docker

```
Dockerfile
  → python:3.12-slim
  → non-root user uid 10001
  → /data volume owned by that user
  → gunicorn -b 0.0.0.0:8080 --workers 1 --threads 4 app:app
fly.toml
  → DB_PATH=/data/ledger.db
  → LEDGER_ENV=production
  → force_https
  → one machine (volume limit)
Secrets set with `fly secrets set`:
  SECRET_KEY, LEDGER_PASSWORD_HASH, CLOSE_DAY_TOKEN
```

### PythonAnywhere

```
WSGI file (from wsgi_pythonanywhere.py)
  → sets env (SECRET_KEY, LEDGER_PASSWORD_HASH, LEDGER_ENV, DB_PATH)
  → imports app as `application`
Tasks tab
  → close_day_task.py imports the app and drives /close-day in-process
```

## 13. Error and redirect conventions

- Unauthenticated POST → `401 Unauthorized` body, never a redirect. The offline
  queue depends on this.
- Unauthenticated GET → redirect to `/login?next=…`.
- No password configured → GET redirects to `/setup`; POST returns `401`.
- Successful writes → `302` back to the relevant page; some routes flash a
  message.
- Invalid form input is usually ignored with a redirect (except savings and
  journal flows, which flash specific errors or silently no-op).
