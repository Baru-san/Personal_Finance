---
name: project-memory
description: Read, verify, and update this repository's project-memory docs (AGENTS.md and docs/) after inspecting source and tests. Use at the start of any session, before code changes, and whenever behavior, schema, routes, rules, or progress change.
---

# Project memory skill

This repository keeps its durable context in `AGENTS.md` and `docs/`. The goal
is that a fresh session can understand the app, its current state, and how to
continue safely without previous chat history.

## When to use

- At the start of every coding session.
- Before implementing or reviewing a change.
- After any change that affects behavior, schema, routes, rules, dependencies,
  deployment, or development progress.
- When you discover documentation that contradicts source or tests.

## Read protocol (start of session)

1. Read `AGENTS.md` completely.
2. Read, in this order:
   - `docs/STATE.md`
   - `docs/PROJECT.md`
   - `docs/RULES.md`
   - `docs/ARCHITECTURE.md`
3. Read `docs/DATABASE.md`, `docs/API.md`, and `docs/DECISIONS.md` as the task
   requires.
4. Inspect the working tree:
   - `git status --short`
   - `git log --oneline -10`
5. Run the baseline suite:
   - `python3 -m unittest discover -s tests -v`
6. Inspect the actual source for anything you will change. `docs/STATE.md`
   distinguishes implemented from planned/unverified behavior, but source and
   tests are the final authority.

Do not read `.password_hash`, `.secret_key`, `ledger.db`, `dummy.db`, logs, or
real environment values into any output or document.

## Verify protocol (before trusting or writing docs)

For every claim you intend to rely on or update:

1. Locate the implementation: `app.py`, the relevant template, `static/*.js`,
   `tests/test_app.py`, or a config file.
2. Confirm the behavior from source, not from memory or an older doc.
3. Check whether a test covers it. If not, treat it as **unverified** and say
   so rather than asserting it.
4. Reconcile contradictions explicitly:
   - If docs and code disagree, code wins.
   - Note the drift in the doc you update.
   - If the cause is ambiguous, ask instead of guessing.
5. For schema claims, inspect `init_db()` and `PRAGMA table_info` usage.
6. For route/contract claims, inspect the route decorator, form parsing, and
   redirect/flash behavior.

Prefer evidence with file references, e.g.
`app.py:1111 apply_journal_edit` or `tests/test_app.py:452`.

## Update protocol (after a change)

Update only the docs affected by the change:

| Change | Update |
|---|---|
| Progress, known issues, next tasks, uncommitted state | `docs/STATE.md` (always) |
| User-visible behavior, flows, specs, feature status | `docs/PROJECT.md` |
| Components, files, data flow, frontend/offline flow | `docs/ARCHITECTURE.md` |
| Tables, columns, indexes, relationships, migrations | `docs/DATABASE.md` |
| Routes, form fields, auth, idempotency contracts | `docs/API.md` |
| Business rules, conventions, security constraints | `docs/RULES.md` |
| A non-obvious architectural choice or trade-off | `docs/DECISIONS.md` |
| Agent workflow/doc maintenance itself | `AGENTS.md` and this skill |

Also update `docs/STATE.md` when:

- The test count or pass/fail state changes.
- A feature moves between implemented / unverified / planned.
- A branch, commit, or uncommitted working-tree state is relevant.
- A known issue is discovered or resolved.

### `docs/STATE.md` format

- Keep a dated snapshot at the top.
- Include branch, last commit, working-tree cleanliness, and the exact test
  command/result.
- Use status language consistently: **Done**, **In progress**,
  **Unverified**, **Planned**, **Known issue**, **By design**.
- Next tasks should be prioritized and actionable.
- Never claim manual browser checks were performed unless they actually were.

### `docs/DECISIONS.md` format

Add a new `ADR-NNN` entry with:

- **Status** — Accepted / Implemented / Superseded / Proposed.
- **Decision** — one paragraph.
- **Why** — the constraints that drove it.
- **Consequences** — what future changes must respect.

Do not rewrite history: supersede an ADR rather than silently changing it.

## Documentation quality rules

- Distinguish **implemented**, **unverified**, and **planned** behavior
  explicitly.
- Be concrete: name routes, functions, tables, fields, and files.
- Keep security constraints prominent and never weaken them.
- Do not paste secrets, real balances, hashes, tokens, or environment values.
- Do not document aspirational behavior as if it exists.
- Keep docs concise enough to be read at session start; move detail into the
  relevant topic file rather than inflating `STATE.md`.
- Preserve the existing retro/plain Markdown style; no generated HTML.

## Finish protocol

Before ending a session:

1. Run `python3 -m unittest discover -s tests -v` (or record why you could not).
2. Update the affected docs.
3. Update `docs/STATE.md` with the new verified state.
4. In your final message, report:
   - what changed,
   - the exact commands run and their result,
   - what remains unverified or requires manual checks,
   - any documentation drift you found but did not fix.
