# AGENTS.md — Ledger repository guide

This file is the mandatory entry point for any coding agent (or human) working
on this repository. Read it before touching code.

## 1. What this repository is

**Ledger** is a single-user, self-hosted, retro-terminal-style personal finance
tracker built with Flask + SQLite. It tracks scheduled budget categories,
one-off spending, recurring and one-off income, savings pots, automatic
"unspent budget to savings" sweeps, a month-by-month ledger, and trend charts.
It is installable as a PWA and has an offline write queue.

There are no per-user accounts. One shared password protects the whole instance.

## 2. Mandatory session startup

Do these in order, before making any change:

1. **Read the project docs** (at minimum):
   - `docs/STATE.md` — what is done, in progress, broken, or next.
   - `docs/PROJECT.md` — product intent, user flows, specifications.
   - `docs/RULES.md` — business rules, invariants, security constraints.
   - `docs/ARCHITECTURE.md` — components, data flow, file map.
   - Then `docs/DATABASE.md`, `docs/API.md`, and `docs/DECISIONS.md` as the
     task requires.
   - `CLAUDE.md` remains a detailed legacy deep-dive; it is useful but
     `docs/` is the maintained source of truth for project state.
2. **Inspect the working tree**:
   - `git status --short`
   - `git log --oneline -10`
   - Never assume the branch or uncommitted state from a previous session.
3. **Verify the baseline**:
   - `python3 -m unittest discover -s tests -v`
   - If tests fail before your change, record that in your summary and do not
     silently "fix" unrelated failures.
4. **Inspect the relevant source**, not just the docs. Documentation can drift;
   source and tests are the final authority. Use `docs/STATE.md` to tell
   implemented behavior from planned or unverified behavior.
5. **Check for secrets**: `.password_hash`, `.secret_key`, `ledger.db`,
   `dummy.db`, `*.log`, and real environment values must never be read into
   documentation, committed, or printed.


Use the project-memory skill when maintaining
project context and documentation.

## 3. Documentation map

| File | Read it when you need… |
|---|---|
| `docs/PROJECT.md` | Product goals, user flows, feature specs and status. |
| `docs/ARCHITECTURE.md` | Tech stack, directory layout, request/data flow. |
| `docs/DATABASE.md` | Tables, columns, indexes, relationships, migrations. |
| `docs/API.md` | Routes, form fields, auth, idempotency contracts. |
| `docs/RULES.md` | Business rules, coding conventions, security constraints. |
| `docs/STATE.md` | Current progress, known issues, next tasks. |
| `docs/DECISIONS.md` | Why the architecture is the way it is (ADRs). |
| `.opencode/skills/project-memory/SKILL.md` | The read → verify → update protocol for these docs. |

## 4. Non-negotiable rules

- **Do not change application behavior while doing documentation-only work.**
- **Never commit secrets.** Do not write passwords, password hashes, tokens,
  session keys, or `.env` values into any tracked file. The real
  `LEDGER_PASSWORD_HASH`, `SECRET_KEY`, and `CLOSE_DAY_TOKEN` belong in the
  deployment's environment/secrets manager only.
- **Never enable `LEDGER_DISABLE_AUTH=1` on a deployed/public host.** It opens
  every read and write route to anyone who can reach the port.
- **Never enable `LEDGER_DEBUG=1` on a deployed host.** The Werkzeug debugger is
  remote code execution.
- **Every runtime value in SQL must use a placeholder** (`?` or named). The only
  f-string SQL is the `client_id` migration over the hardcoded
  `MIGRATED_TABLES` tuple; never interpolate a runtime-derived table/column.
- **Money is always integer Rupiah.** No floats. Route inputs go through
  `clamp_amount()`.
- **Money that moves between concepts must move atomically.** Pot balances are
  stored running totals; update them in the same transaction as the audit row,
  and only when the insert actually happened (`cur.rowcount`).
- **Preserve idempotency.** Every offline-queued add form carries a per-submit
  `client_id`; the server dedups on it. `sweep_unspent()` is keyed by
  `(category_id, sweep_key)`; do not weaken either guard.
- **Keep the single-worker deployment assumption.** The app writes to one
  SQLite file. Do not change the gunicorn command to multiple worker processes
  or scale Fly beyond one machine.
- **Add or update tests** for behavior changes. Use the existing stdlib
  `unittest` suite; no new test dependency.
- **No new framework/build step** for a small UI change; this project is
  server-rendered Jinja + small vanilla-JS blocks by design.

## 5. Change workflow

1. Read the docs and source relevant to the task.
2. Write or update a failing test first when feasible.
3. Make the smallest change that satisfies the requirement.
4. Run the full suite:
   `python3 -m unittest discover -s tests -v`
5. Manually verify UI/offline behavior if the change touches templates,
   `static/offline.js`, `static/sw.js`, or auth.
6. Update documentation:
   - `docs/STATE.md` — always, if the change alters progress, known issues, or
     next tasks.
   - `docs/PROJECT.md` — if user-visible behavior/specification changes.
   - `docs/ARCHITECTURE.md` — if components, data flow, or files change.
   - `docs/DATABASE.md` — if schema, indexes, or migrations change.
   - `docs/API.md` — if routes, form fields, or contracts change.
   - `docs/RULES.md` — if a rule, convention, or security constraint changes.
   - `docs/DECISIONS.md` — if you make a non-obvious architectural choice.
7. Report what changed, what was verified, and what remains unverified.

## 6. Definition of done

A change is done only when:

- The behavior is implemented and covered by tests where practical.
- The full test suite passes (or pre-existing failures are documented).
- Relevant documentation in `docs/` is updated.
- No secret, real database, or generated local artifact is added to git.
- The final summary states the exact commands run and any manual checks not
  performed.

## 7. Task-specific gotchas

- The offline queue deliberately excludes edit/close-day forms. The
  savings deposit/allocate forms are queued and guarded; see
  `docs/ARCHITECTURE.md` and `docs/RULES.md`.
- `pots.balance` is **not** derived. Any new route that moves savings must keep
  the ledger row and the balance in sync, including undo/delete paths.
- Editing an `auto_save` transaction must adjust the Auto-saved pot by the
  delta, atomically (see `apply_journal_edit` in `app.py`).
- `init_db()` runs on every import/boot and is the only migration mechanism.
  All migrations must be additive and idempotent.
- `/close-day` is catch-up safe and must stay that way; it also prunes
  transactions older than 12 months.
- The Trends page is anchored to `TREND_START_MONTH = "2026-09"`; the Savings
  page's monthly chart uses a rolling 12 months.
- `README.md` says `python`; on this machine the interpreter is `python3`.
