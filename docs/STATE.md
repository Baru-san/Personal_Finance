# STATE.md — current progress and next tasks

> Snapshot date: **2026-10-19**. Always reconcile this file with `git status`,
> `git log`, and a test run before trusting it.

## 1. Repository snapshot

- Branch: `feature/savings-allocation-and-catchup`
- Last commit: `f79d405 Add wishlist page, offline write guards, and project docs`
- Working tree: **clean**
- Test command: `python3 -m unittest discover -s tests -v`
- Last verified result: **53 tests passed** on 2026-10-19.
- Last documentation review: **2026-10-19** — project-memory read/verify
  protocol applied; route, schema, idempotency, trend-anchor, wishlist, and
  deployment claims re-checked against source. No drift found.

## 2. Implemented features

| Area | Status | Evidence |
|---|---|---|
| Flask app, SQLite schema, idempotent `init_db()` | Done | `app.py`; tests import against temp DB |
| Shared-password auth, setup token, lockout, session fingerprint | Done | `AuthTests` |
| Dashboard category cards, totals, statuses, day-by-day trend | Done | `CoreFlowTests`, `TrendChartTests` |
| Add/edit categories (weekly/monthly) | Done | Add covered; edit not directly tested |
| Spend + custom expense | Done | `CoreFlowTests` |
| Recurring + custom income | Done | Seed test populates; income route templated |
| Catch-up `/close-day` with 12-month pruning | Done | `CloseDayCatchUpTests` |
| Savings deposits, allocations, withdrawals, undo | Done | `SavingsAllocationTests` |
| Offline IndexedDB queue + replay | Done | Manual/browser only |
| PWA service worker caching | Done | Manual/browser only |
| Ledger + journal with month selection | Done | Templates; no dedicated route tests |
| Wishlist (priority, manual progress, archive, offline add) | Done | `WishlistTests` |
| Trends anchored to `2026-09` | Done | `TrendWindowTests` (date-gated) |
| Dummy seed script | Done | `DummySeedTests` |
| Fly.io, Docker, PythonAnywhere deployment files | Done | README + config files |

## 3. Recently completed (committed in `f79d405`)

The **Wishlist page** (`/wishlist`) is implemented:

1. `app.py` — additive `wishlist_items` table in `init_db()` (registered in
   `MIGRATED_TABLES` for `client_id` idempotency) plus add/edit/buy/restore/
   delete routes and validation helpers.
2. `templates/wishlist.html` — priority-ordered cards, progress bars, archived
   list, and an offline-queued add form. Wishlist nav link added to every page.
3. `static/style.css` + `static/offline.js` comment + `static/sw.js`
   `CACHE_VERSION` bumped to `v7`.
4. `tests/test_app.py` — 11 `WishlistTests` covering listing, replay dedup,
   edit/progress, archive/restore, delete, validation, URL safety, offline
   attribute, auth, and the non-monetary invariant.

The double-transaction fix from `to_be_implemented.md` is **implemented**:

1. `static/offline.js` — per-form in-flight submit guard (`WeakSet`); a second
   submit while one is pending is ignored; submit buttons are disabled and
   re-enabled after queueing.
2. `templates/savings.html` — the SET ASIDE and MOVE forms now carry
   `data-offline-queue="deposit"` / `"allocate"`, so they get per-submit
   `client_id`s and offline replay.
3. `app.py` — `apply_journal_edit()` performs a guarded
   `UPDATE ... WHERE id=? AND amount=?`, applies the auto-save pot delta only
   when the update won, and rolls back on a stale edit.
4. `tests/test_app.py` — added deposit-replay, savings-form-attribute, and
   stale auto-save edit tests.

## 4. Committed in `f79d405`

- Wishlist feature: `app.py`, `templates/wishlist.html`, all nav templates,
  `static/style.css`, `static/offline.js` (comment), `static/sw.js`,
  `tests/test_app.py`, and `docs/`.
- Double-transaction fix: `app.py`, `static/offline.js`,
  `templates/savings.html`, `tests/test_app.py`.
- Project memory: `AGENTS.md`, `docs/`, and
  `.opencode/skills/project-memory/SKILL.md`.
- Operator edits: `.dockerignore`, `.gitignore`, `CLAUDE.md`, `README.md`.
- `to_be_implemented.md` (its plan is implemented; kept as history).

## 5. Known issues and limitations

| Issue | Severity | Notes |
|---|---|---|
| Double-click fix not manually verified in a browser | Medium | Tests cover server idempotency and the guarded edit; the JS in-flight guard has no automated test. |
| Queued savings forms can swallow flash messages | Low/Medium | Background fetch follows the redirect and consumes the flash before navigation. Same class of trade-off as other queued forms. |
| No automated JS/browser tests | Medium | Offline queue, service worker, and in-flight guard rely on manual checks. |
| Wishlist offline add not manually verified | Low | Server idempotency is tested; the queue/replay path has no browser test. |
| `edit_category` has no dedicated test | Low | Behavior is simple but unverified by the suite. |
| Trends tests are date-gated | Low | Several skip before 2026-09-01; irrelevant after that date. |
| Single machine / single worker only | By design | SQLite write lock; do not scale horizontally. |
| External scheduler required for daily close | By design | `/close-day` or `close_day_task.py`. |
| `to_be_implemented.md` is stale | Low | Its plan is implemented; treat `STATE.md` as authoritative. |
| Documentation drift possible | Low | `docs/` is maintained; `CLAUDE.md` is legacy. |

## 6. Next tasks

Prioritized:

1. **Manually verify** the double-submit guard under a throttled connection:
   double-click SPEND / SET ASIDE / MOVE; confirm one row, one balance change,
   and that offline replay still applies exactly once.
2. **Manually verify** the wishlist add form with the network disabled:
   confirm it queues, replays once on reconnect, and that edit/buy/restore/
   delete stay online-only.
3. Consider a minimal browser/JS test harness or a documented manual test
   checklist for the offline queue and service worker.
4. Add a dedicated test for `edit_category`, including weekly due-day
   normalization.
5. Decide whether savings deposit/allocate should preserve flash feedback in
   the JS path (e.g. different success handling) or whether the current
   trade-off is acceptable.
6. Merge the feature branch after review.
7. Remove or archive the now-implemented `to_be_implemented.md` plan.

## 7. Verification checklist for the next session

```bash
git status --short
git log --oneline -10
python3 -m unittest discover -s tests -v
```

Manual checks (not automated):

- Log in, log out, and confirm the login redirect behavior.
- Add a category/spend/income while offline; reconnect and confirm replay.
- Double-click an offline-queued form and confirm one entry.
- Run Daily Close twice; confirm the second run reports nothing to sweep.
- Deposit, transfer, withdraw, and undo each; confirm pot balances and
  available/cash_left.
- Edit an `auto_save` journal entry; confirm the Auto-saved pot delta.
