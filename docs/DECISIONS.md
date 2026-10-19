# DECISIONS.md — architecture decision records

Each entry records a non-obvious choice and its rationale. Status is one of
**Accepted**, **Implemented**, **Superseded**, or **Proposed**.

---

## ADR-001 — Single-file Flask app with SQLite

**Status:** Accepted

**Decision:** Keep the backend in one `app.py` and persist to one SQLite file.

**Why:** This is a single-user, self-hosted app. A single file minimizes
deployment friction, keeps review easy, and avoids ORM/migration dependencies.
SQLite is sufficient for one writer and one machine, and PythonAnywhere's
persistent filesystem supports it for free.

**Consequences:** No horizontal scaling, no multi-machine writes. `init_db()`
must be idempotent because it runs on every boot. Keep one worker; use threads.

---

## ADR-002 — Raw SQL with placeholders

**Status:** Accepted

**Decision:** Use `sqlite3` directly with `?`/named parameters; no ORM.

**Why:** Queries are few and readable. `sqlite3.Row` gives name-based access.
Avoiding an ORM keeps the dependency surface tiny and makes money-movement SQL
(guarded updates, `rowcount`, `ON CONFLICT`) explicit.

**Consequences:** Every runtime value must be parameterized. The only f-string
SQL is the hardcoded `MIGRATED_TABLES` migration loop.

---

## ADR-003 — Integer Rupiah everywhere

**Status:** Accepted

**Decision:** Store and compute money as `INTEGER` Rupiah; never floats.

**Why:** Floating point rounding is unacceptable for balances and sums.
Integer arithmetic makes pot balances exact and chart math predictable.

**Consequences:** Inputs are parsed as `int` and clamped to a sane range.
Formatting is presentation-only (`format_rp`).

---

## ADR-004 — One shared password, no accounts

**Status:** Accepted

**Decision:** Protect the whole instance with one password hash and a signed
session cookie.

**Why:** The product is explicitly single-user. Accounts, roles, and password
reset flows add complexity without a user need.

**Consequences:** No per-user data isolation. The session stores a hash of the
password hash, so changing the password invalidates sessions. Login lockout is
global rather than per-IP because a single user behind a proxy is the threat
model.

---

## ADR-005 — Console-token first-run setup

**Status:** Accepted

**Decision:** With no password configured, require a high-entropy token printed
only to the process console before `/setup` accepts a new password.

**Why:** A freshly deployed public URL would otherwise be claimable by whoever
races to it first. The token is visible only to whoever controls the deployment.

**Consequences:** Automated deploys should set `LEDGER_PASSWORD_HASH` directly.
`/setup` closes permanently once a hash exists.

---

## ADR-006 — Unauthenticated POST returns 401, never a redirect

**Status:** Accepted

**Decision:** Reject unauthenticated POSTs with a bare `401`.

**Why:** `offline.js` follows redirects in `fetch`. A redirect to `/login`
resolves to a `200` and would look like a successful write, causing the queued
record to be deleted unsent.

**Consequences:** This is a load-bearing contract between auth and the offline
queue. Do not "improve" it into a redirect without updating the queue logic.

---

## ADR-007 — Per-submit `client_id` for idempotent writes

**Status:** Accepted

**Decision:** Each queued add form generates a UUID per submit; the server
dedups with a partial unique index on each write table.

**Why:** Replays are expected (offline retry, page reload before response,
background sync). A stable key would let replays be at-most-once without
suppressing legitimate repeat entries.

**Consequences:** `client_id` is nullable so no-JS submissions still work. Do
not replace the per-submit UUID with a static hidden field. Money-moving
routes must credit balances only when the insert won.

---

## ADR-008 — Per-window `sweep_key` for auto-save

**Status:** Accepted

**Decision:** Store the completed window's start date in
`transactions.sweep_key` and enforce a partial unique index on
`(category_id, sweep_key)`.

**Why:** `/close-day` may be called concurrently (dashboard button + cron +
retries). A pre-check alone is racy. The unique index ensures exactly one
auto-save row per category per window, and only the insert winner credits the
pot.

**Consequences:** Catch-up can iterate missed windows without double-banking.
Legacy rows with `created_at IS NULL` keep the old loose behavior.

---

## ADR-009 — Pot balances are stored running totals

**Status:** Accepted

**Decision:** `pots.balance` is authoritative and updated alongside every
audit row, rather than derived by summing deposits/movements.

**Why:** Pot balances also absorb auto-save sweeps and historical/legacy data;
deriving them from the merged logs would be fragile. A stored balance keeps
reads cheap and simple.

**Consequences:** Every money-moving write and undo must update the balance in
the same transaction, guarded by `rowcount`, or the two will drift. This is the
highest-risk invariant in the app.

---

## ADR-010 — Catch-up-safe close bounded by creation and retention

**Status:** Accepted

**Decision:** `run_close_day()` sweeps every missed completed window, starting
no earlier than a category's `created_at` and the 12-month retention floor.

**Why:** A missed Monday/1st should not lose a period. Sweeping before a
category existed would invent history. Sweeping beyond retention would bank the
full budget because old spend rows were pruned.

**Consequences:** Date windows are half-open ISO ranges. `prune_old_transactions`
must remain aligned with `_retention_floor`.

---

## ADR-011 — Offline queue only for add forms; guard in-flight submits

**Status:** Accepted (in-flight guard implemented in working tree)

**Decision:** Queue add forms, not edits/deletes/close-day. Intercept submit,
generate a UUID, and serialize submissions per form.

**Why:** Edits/deletes are either already idempotent or dangerous to replay
later (`/close-day` derives windows from the server's current date). A fresh
UUID per submit prevents same-key replay but not two rapid clicks with
different keys, hence the per-form in-flight guard.

**Consequences:** Successful JS writes navigate after a background fetch, so
flash messages from queued routes may be consumed before display. Manual
browser verification is required for changes here.

---

## ADR-012 — Trends anchored to `TREND_START_MONTH = "2026-09"`

**Status:** Accepted

**Decision:** Trend charts run from a fixed start month through the current
month, instead of a rolling 12-month window.

**Why:** A rolling window slides history off the left edge; the owner wanted
charts that grow one point per month. The Savings page keeps a separate rolling
12-month chart.

**Consequences:** The constant is hardcoded; tests before 2026-09 are skipped.
Changing it alters the meaning of cumulative history.

---

## ADR-013 — Service worker: network-first pages, cache-first assets

**Status:** Accepted

**Decision:** Navigations are network-first with a cached fallback; `/static/*`
is cache-first; non-GET requests are never intercepted.

**Why:** Balances must be fresh when online, but the app shell should load
instantly and work offline. Writes need to reach the network so the IndexedDB
queue can detect failure.

**Consequences:** Never cache redirects (the login page would become the
offline fallback for every page). Bump `CACHE_VERSION` when the precache list
or strategy changes.

---

## ADR-014 — Single gunicorn worker, multiple threads; one Fly machine

**Status:** Accepted

**Decision:** Deploy with `--workers 1 --threads 4` and one Fly machine with one
attached volume.

**Why:** SQLite allows one writer; multiple processes would contend and risk
`database is locked`. Threads give enough concurrency for a single user, and
each request gets its own connection.

**Consequences:** Do not add workers or scale Fly. This is a deliberate
availability/scalability trade-off.

---

## ADR-015 — PythonAnywhere close runs in-process

**Status:** Accepted

**Decision:** `close_day_task.py` imports the app and calls `/close-day` through
Flask's test client instead of making an outbound HTTP request.

**Why:** PythonAnywhere free accounts cannot make arbitrary outbound requests.
Driving the real route in-process avoids duplicating sweep logic.

**Consequences:** The task must run with the same `DB_PATH` as the web app.
It self-generates a one-off `CLOSE_DAY_TOKEN` before importing the app.

---

## ADR-016 — Guarded read-modify-write for auto-save journal edits

**Status:** Accepted (implemented in working tree)

**Decision:** Read the transaction, then update with
`WHERE id = ? AND amount = ?` (the originally-read amount). Apply the pot delta
only if `rowcount == 1`; otherwise roll back.

**Why:** Two concurrent edits could both read the old amount, compute the same
delta, and both credit the Auto-saved pot. An unconditional update has no way
to detect the lost race.

**Consequences:** A stale edit is silently discarded (no error page), which is
the desired behavior for this single-user app. Any future edit path that moves
money must use an equivalent guard.

---

## ADR-017 — Documentation as repository memory

**Status:** Accepted (this task)

**Decision:** Maintain `AGENTS.md` plus `docs/` as the source of truth for new
agent sessions, with `.opencode/skills/project-memory/SKILL.md` defining the
read → verify → update protocol.

**Why:** Chat history is not available to a fresh session. Code alone does not
capture intent, known issues, or why invariants exist. A dated `STATE.md`
prevents stale assumptions.

**Consequences:** Agents must read the docs at startup, verify claims against
source/tests, and update the affected docs after behavior changes. Docs must
never contain secrets and must distinguish implemented, planned, and unverified
behavior.

---

## ADR-018 — Wishlist progress is a non-monetary tracker

**Status:** Implemented

**Decision:** Store wishlist progress in `wishlist_items.saved_amount` as a
manual number. Do not link it to pots, deposits, or any cash figure. Marking an
item bought only sets `bought_at`.

**Why:** The user wants to plan purchases and see progress, not to move money.
Reusing pots would double-count savings or require a new money-movement path
with its own undo and race guards. A tracker keeps the highest-risk money
invariants untouched and is reversible if real earmarking is wanted later.

**Consequences:** Wishlist progress can drift from actual pot balances by
design. Never treat `saved_amount` as spendable, saved, or available cash. If
real earmarking is added later, it needs a new ADR and full money-movement
treatment.
