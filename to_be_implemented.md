# Double-Transaction Fix Plan

> **Status: implemented** on 2026-10-19. See `docs/STATE.md` for the verified
> state and `docs/DECISIONS.md` (ADR-007, ADR-011, ADR-016) for the rationale.
> This file is kept as the original plan; it is no longer a pending task.

## Problem

The app's idempotency depends entirely on a per-submission `client_id` UUID. Several gaps allow double transactions:

1. **Rapid double-submit of a queued form** (`static/offline.js:250-294`). The handler calls
   `event.preventDefault()`, generates a **fresh** UUID (`offline.js:256`), and fires an async
   `fetch`. The button stays enabled. A double-click / double-tap / Enter-repeat fires a second
   `submit` with a **different** UUID. Server-side `ON CONFLICT(client_id)` only dedups the *same*
   UUID, so both insert -> duplicate spend.

2. **The two money-moving forms send no `client_id` at all.** `templates/savings.html:64`
   ("SET ASIDE", `add_pot_deposit`) and `:76` ("MOVE", `allocate_savings`) are deliberately *not*
   `data-offline-queue`, and no template contains a `client_id` field. Any double submit
   double-books a deposit or pot movement and double-updates `pots.balance` (`app.py:1559-1592`,
   `app.py:1618-1663`).

3. **Read-modify-write race in `edit_journal_entry`** (`app.py:1111-1132`). Two concurrent edits
   of an `auto_save` row can both read the old amount, each compute the same `delta`, and each add
   it to the pot balance (`app.py:1120-1129`) -> pot credited twice. There is no guarded UPDATE.

4. **No-JS resubmission** (browser refresh/back on a POST) has no key at all for the non-queued
   forms.

Existing tests (`tests/test_app.py:141`, `:296`) cover *same-key* replay but not distinct-key
double submits or the edit race, so this hole is untested.

## Fix Plan

### 1. `static/offline.js` — in-flight guard
- Add a module-level `var submittingForms = new WeakSet();` (or use `form.dataset.submitting`).
- At the top of the delegated submit handler (`offline.js:250`), after `if (!kind) return;`:
  - if the form is already in the set -> `event.preventDefault(); return;`
  - else add it to the set and disable its submit button(s).
- On the `.catch` queue path, leave the guard in place until the record is queued, so a second
  click during the failed attempt cannot slip through. The success path navigates away; no cleanup
  needed. (Optionally re-enable if `queueAdd` itself rejects.)
- This is the single choke point for all `data-offline-queue` forms, so spend/category/custom
  expense/income get the guard for free.

### 2. `templates/savings.html` — make deposit/allocate idempotent
- Add `data-offline-queue="deposit"` to the "SET ASIDE" form (`:64`) and
  `data-offline-queue="allocate"` to the "MOVE" form (`:76`).
- No optimistic-UI branch is needed for these `kind`s; `offline.js`'s queue path already handles
  unknown kinds (just queues + replays).
- Result: both forms get a fresh per-submit UUID, the in-flight guard, and offline replay. Server
  routes already honor `client_id` (`app.py:1576`, `app.py:1312`).
- Do **not** add a static hidden `client_id` in Jinja — reused on legitimate repeat submissions it
  would silently drop real deposits. The key must be regenerated per submit.

### 3. `app.py` — atomic auto_save edit (`edit_journal_entry:1111`)
- Replace the unconditional `UPDATE transactions` with a guarded one:
  `UPDATE transactions SET amount=?, note=? WHERE id=? AND amount=?`, passing the originally-read
  `row["amount"]`.
- Only if `cur.rowcount == 1` and `row["kind"] == "auto_save"` apply the pot delta, all inside one
  transaction; otherwise `db.rollback()`. This makes read-modify-write atomic against concurrent
  edits so the Auto-saved pot cannot be credited twice.

### 4. `tests/test_app.py` — coverage
- `test_deposit_replay_is_idempotent`: POST `/savings/deposit` twice with the same `client_id`;
  assert one `pot_deposits` row and pot credited once.
- `test_allocate_replay_is_idempotent` exists (`:296`) — confirm it still holds via the new form
  path; leave as-is.
- `test_edit_auto_save_is_concurrency_safe`: seed an `auto_save` txn + pot balance, invoke the
  guarded update path with a stale amount, assert the pot is not double-credited.
- Run `python -m unittest discover -s tests -v`.

## Verification
- Manual: double-click SPEND/SET ASIDE/MOVE under throttled network; confirm a single entry, and
  offline queue still replays exactly once.
- Full suite green.

## Files to touch
- `static/offline.js`
- `templates/savings.html`
- `app.py`
- `tests/test_app.py`
