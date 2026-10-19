# API.md — routes, authentication, and contracts

This is a server-rendered HTML application. There is **no JSON API**. "API"
here means HTTP routes, form fields, redirects, and idempotency guarantees.

## 1. Authentication model

- Single shared password, no accounts.
- Password source: `LEDGER_PASSWORD_HASH` env var, else `.password_hash` file.
- Session: signed cookie, one year, `HttpOnly`, `SameSite=Lax` (CSRF
  mitigation), `Secure` when `LEDGER_ENV=production`.
- Session stores `auth = sha256(password_hash)`, so rotating the password
  invalidates all existing sessions.
- `LEDGER_DISABLE_AUTH=1` disables the gate for local development only.
- `LEDGER_DEBUG=1` enables the Werkzeug debugger only in `__main__` mode.

### `require_login()` behavior

| Request | Result |
|---|---|
| Public endpoint (`login`, `setup`, `static`, `service_worker`) | allowed |
| `POST /close-day` with matching `X-Close-Day-Token` (when `CLOSE_DAY_TOKEN` is set) | allowed |
| No password configured, GET | `302` to `/setup` |
| No password configured, POST | `401 Setup required` |
| Authenticated session | allowed |
| Unauthenticated POST | `401 Unauthorized` (bare body, **not** a redirect) |
| Unauthenticated GET | `302` to `/login?next=<path>` |

The bare `401` on POST is load-bearing: `offline.js` treats a redirect to
`/login` as a failed write and keeps the queued record.

### Login lockout

- Five failed attempts trigger escalating lockout: 60s, 120s, 240s … capped at
  15 minutes.
- The counter is global (single-user app), not per-IP.
- A successful login resets it.

### First-run setup

- When no hash is configured, a random `SETUP_TOKEN` is printed to the console.
- `/setup` requires a matching `token` query/form value, else `403`.
- Password must be at least 8 characters and match confirmation.
- On success the hash is written to `.password_hash` (mode `0600`), the setup
  token is cleared permanently, the session is authenticated, and the user is
  redirected to `/`.

## 2. Common conventions

- All POST routes return a `302` redirect on success (except auth/setup cases).
- Amounts are parsed with `request.form.get(..., type=int)` and validated by
  `clamp_amount()`: integer, `1 ≤ amount ≤ 1_000_000_000_000`.
- Invalid or missing data generally causes a redirect with no write; savings
  and close-day routes flash a message.
- Money inputs in templates use `data-money-input`; the visible field is
  formatted with dots and a hidden field carries the plain integer.
- `client_id` is optional. When present, it is the idempotency key. It is only
  unique when non-NULL.
- `note`/`label` values are stripped.

## 3. Read routes

### `GET /`

Dashboard. Query params: none.

Renders categories, current-month totals, recent activity, and the day-by-day
spending trend.

### `GET /ledger`

Query: `month=YYYY-MM` (optional).

- Valid months: last 12 months from `available_months(12)`.
- Invalid/missing → current month.
- Renders merged entries (spends, auto-saves, custom income) plus totals.

### `GET /journal/<month_key>`

- `month_key` must be in the last 12 months, else redirect to `/ledger`.
- Renders editable amount/note forms for `transactions` rows only.

### `GET /savings`

Renders pots, current available/unallocated amount, weekly/monthly sweep
previews, saved-by-expense and saved-by-month charts, sweep history, and the
last 20 pot movements.

### `GET /trends`

Renders net cash flow and cumulative auto-saved charts from
`TREND_START_MONTH` (`2026-09`) through the current month.

### `GET /income`

Renders recurring income sources, this month's custom income, and totals.

### `GET /wishlist`

Renders active wishlist items (ordered high → medium → low priority, newest
first), their saved-so-far progress, and the archived/bought list. Totals are
active items only: estimated price, saved amount, and remaining. Progress is a
manual tracker and does not read or change pots.

### `GET /sw.js`

Serves `static/sw.js` with `Cache-Control: no-cache`. Served from the root so
the service-worker scope is `/`.

## 4. Auth routes

### `GET /login`, `POST /login`

Form fields:

| Field | Required | Notes |
|---|---|---|
| `password` | yes | Compared with the stored hash. |
| `next` | no | Same-site relative path only (`safe_next_path`). |

- Success: sets session, redirects to `next` (or `/`).
- Failure: re-renders login with an error (HTTP 200).
- Locked out: error with seconds remaining.

### `GET /setup`, `POST /setup`

Values: `token` (query or form), `password`, `confirm`.

- Redirects to `/login` if a password is already configured.
- `403` if the console token is missing/incorrect.
- Success writes the hash, authenticates, redirects `/`.

### `POST /logout`

Clears the session and redirects to `/login?logged_out=1`, which triggers a
client-side purge of cached PWA responses.

## 5. Write routes

### `POST /close-day`

Auth: logged-in session, or `X-Close-Day-Token` header matching
`CLOSE_DAY_TOKEN`.

No form fields. Effects:

1. `prune_old_transactions(retain_months=12)`.
2. `run_close_day(db, today)` sweeps completed, not-yet-swept windows.
3. Commits and flashes the total moved (or "nothing to sweep").
4. Redirects to `/`.

Idempotency: `UNIQUE(category_id, sweep_key) WHERE sweep_key IS NOT NULL`.

### `POST /journal/entry/<int:entry_id>/edit`

Form fields: `month` (return month key), `amount` (integer, required),
`note` (optional).

- Only updates `transactions` rows.
- Guarded update: `WHERE id = ? AND amount = ?` using the amount read at the
  start of the request. A concurrent edit causes no-op + rollback.
- If the row is `kind='auto_save'`, the Auto-saved pot is adjusted by
  `new_amount − old_amount` in the same transaction.
- Redirects to `/journal/<month>`.

### `POST /expense/add`

Form fields: `category_id` (int), `amount` (int), `note` (optional),
`client_id` (optional).

- Inserts `transactions(kind='spend')`.
- `ON CONFLICT(client_id) DO NOTHING` when `client_id` is set.
- Redirects to `/`.

### `POST /expense/custom/add`

Form fields: `label` (optional), `amount` (int), `client_id` (optional).

- Inserts `custom_expenses`; label defaults to `Custom Expense`.
- Redirects to `/`.

### `POST /category/add`

Form fields: `name`, `budget_amount` (int), `due_day` (optional int),
`period` (`weekly`/`monthly`), `client_id` (optional).

- Appends with `sort_order = max + 1`.
- `normalize_category_period()` forces `due_day=None` for weekly and drops
  invalid due days.
- Sets `created_at` for catch-up sweeping.
- Redirects to `/`.

### `POST /category/<int:category_id>/edit`

Form fields: `name`, `budget_amount`, `due_day`, `period`.

- Same validation/normalization as add.
- Not offline-queued and has no `client_id`.
- Redirects to `/`.

### `POST /savings/deposit`

Form fields: `pot_id` (int), `amount` (int), `note` (optional),
`client_id` (optional).

- Validates pot and amount; flashes an error otherwise.
- Inserts `pot_deposits`; credits the pot only if the insert happened.
- Redirects to `/savings`.

### `POST /savings/deposit/<int:deposit_id>/delete`

No form fields. Deletes the deposit row; if a row was deleted, debits the pot
by the same amount. Duplicate undo is a no-op. Redirects to `/savings`.

### `POST /savings/allocate`

Form fields: `from_pot_id` (int), `to_pot_id` (blank = available; otherwise
positive int), `amount` (int), `note` (optional), `client_id` (optional).

Delegates to `apply_pot_movement()`, which returns one of:

| Result | Meaning | User feedback |
|---|---|---|
| `ok` | Movement applied | success flash with source/destination |
| `invalid` | Bad amount or malformed destination | error flash |
| `unknown_pot` | Source/destination missing | error flash |
| `same_pot` | Same source and destination | error flash |
| `insufficient` | Source balance too low | error flash |
| `duplicate` | Replayed `client_id` | silent (already moved) |

Money effects:

- pot → pot: debit source, credit destination.
- pot → available: debit source; the withdrawal is counted by
  `pot_withdrawn_for_range`, raising available/cash_left.
- Redirects to `/savings`.

### `POST /savings/movement/<int:movement_id>/delete`

No form fields. Deletes the movement row; if a row was deleted, credits the
source pot back and debits the destination pot (if any). Duplicate undo is a
no-op. Redirects to `/savings`.

### `POST /income/source/add`

Form fields: `name`, `amount` (int), `due_day` (optional int),
`client_id` (optional).

- Appends with `sort_order = max + 1`; invalid due days become `NULL`.
- Redirects to `/income`.

### `POST /income/custom/add`

Form fields: `label` (optional), `amount` (int), `client_id` (optional).

- Inserts `custom_incomes`; label defaults to `Custom Income`.
- Redirects to `/income`.

### `POST /wishlist/add`

Form fields: `name`, `price` (int, required), `saved_amount` (int, optional,
default 0), `priority` (`high`/`medium`/`low`, default `medium`), `note`
(optional), `url` (optional), `client_id` (optional).

- `price` must pass `clamp_amount()`; `saved_amount` is clamped to
  `0..AMOUNT_MAX`.
- Unknown priorities fall back to `medium`.
- `url` is stored only when it begins with `http://` or `https://`.
- Inserts with `ON CONFLICT(client_id) DO NOTHING`; redirects to `/wishlist`.

### `POST /wishlist/<int:item_id>/edit`

Form fields: same as add (no `client_id`).

- Updates name, price, saved amount, priority, note, and URL.
- No money movement; redirects to `/wishlist`.

### `POST /wishlist/<int:item_id>/buy`

No form fields. Sets `bought_at` (archives the item). Redirects to `/wishlist`.

### `POST /wishlist/<int:item_id>/restore`

No form fields. Clears `bought_at`. Redirects to `/wishlist`.

### `POST /wishlist/<int:item_id>/delete`

No form fields. Deletes the item permanently. Redirects to `/wishlist`.

## 6. Offline queue contract

Forms with `data-offline-queue` are intercepted by `static/offline.js`:

1. `preventDefault()`.
2. If the form is already submitting, ignore the event.
3. Generate a fresh UUID `client_id`, disable submit buttons.
4. POST the form fields (including `client_id`).
5. On network/HTTP failure, store `{clientId, url, params, kind, timestamp}`
   in IndexedDB and reset the form.
6. On reconnect/page load, `replayQueue()` reposts each record; remove it only
   when the response is 2xx and not a redirect landing on `/login`.

Queued form kinds currently:

| Kind | Route |
|---|---|
| `expense` | `/expense/add` |
| `custom_expense` | `/expense/custom/add` |
| `category` | `/category/add` |
| `income_source` | `/income/source/add` |
| `custom_income` | `/income/custom/add` |
| `deposit` | `/savings/deposit` |
| `allocate` | `/savings/allocate` |
| `wishlist` | `/wishlist/add` |

Not queued: category edit, journal entry edit, wishlist edit/buy/restore/delete,
all deletes/undos, `/close-day`.

Known trade-off: queued savings forms navigate only after a background fetch,
which follows the route's redirect and consumes any flash message before the
browser navigates. Their success/error flashes may therefore not be visible in
the interactive JS path.

## 7. Error responses

| Status | When |
|---|---|
| `401` | Unauthenticated POST; setup required and POSTed. |
| `403` | `/setup` without the correct console token. |
| `302` | Successful writes and normal auth redirects. |
| `200` | Successful GET; failed login/setup re-render. |
| `404` | Unknown route (Flask default). |
| `405` | Wrong method (Flask default). |
