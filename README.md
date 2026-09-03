# Ledger — Personal Finance Tracker (Flask)

Retro terminal-style dashboard for your personal budget: scheduled categories,
auto-save-on-unspent logic, and savings pots — backed by SQLite.

## Run locally

    pip install -r requirements.txt
    python app.py

Then open http://localhost:5000 — the database (`ledger.db`) is created
automatically on first run, starting empty. Add your own categories and
income sources from the dashboard and the Income tab.

On first run with no password configured, the app doesn't open up — it
prints a one-time setup link to the console instead:

    FIRST-TIME SETUP: no password configured yet.
    Open http://<this-host>:<port>/setup?token=<random-token>
    to choose a password before exposing this app to any network you don't trust.

Open that link (with the printed token — anyone without it gets a `403`,
which is what stops a remote visitor from claiming the password before you
do) and choose a password there. It's hashed and saved to `.password_hash`
(never the plaintext), and you're logged in immediately — no restart needed.
That file is what future runs read, so you only do this once per deploy.

For automated/scripted deploys where no one will click through a setup page,
set `LEDGER_PASSWORD_HASH` yourself instead and the app skips setup entirely:

    python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))"
    export LEDGER_PASSWORD_HASH='<paste the hash>'
    python app.py

### Skipping the password while developing

To run without any login at all — no setup wizard, no password prompt — start
it with the auth gate disabled:

    LEDGER_DISABLE_AUTH=1 python app.py

Everything is then reachable straight away, `/login` and `/setup` just bounce
to the dashboard, and the Logout button disappears. Local development only:
with this set, anyone who can reach the port can read *and* write your ledger,
so never set it on a deployed instance.

## How it works

- **Categories**: add your own budget categories from the "Add Scheduled
  Category" box on the dashboard (name, budget amount, optional due day).
- **Log an expense**: use the "SPEND" box on any category card on the dashboard.
- **Auto-save on unspent**: press "Run Daily Close" to sweep any leftover
  budget from categories whose `due_day` is today into the
  "Auto-saved (Unspent)" pot. In production, trigger this automatically once
  a day instead of by hand (see below).
- **Set aside current money**: the Savings tab's "Set Aside Current Money"
  box banks cash you already have into any pot right now, without waiting for
  a sweep. It prefills the amount that's still unallocated this month, takes
  an optional note, and lists every manual deposit underneath with an UNDO
  button that pulls the money back out of the pot. Money set aside this way is
  subtracted from "Available This Month" on the dashboard, so it stops being
  counted as spendable.
- **Income**: the Income tab lets you add recurring "scheduled income"
  sources (e.g. salary, counted every month) and one-off "custom income"
  entries (added anytime, counted only for that month).
- **Edit journal**: from the Ledger page, "Edit Journal" opens an editable
  view of that month's spend/auto-save entries — changing an amount acts as
  a refund (lower) or extra charge (higher).

## Automating the daily close (no manual button)

Since this is meant to run unattended, schedule a daily hit to `/close-day`
instead of clicking the button:

- **Fly.io**: use [Fly Machines scheduled runs](https://fly.io/docs/machines/flyctl/fly-machine-run/)
  or a small `flyctl` cron script that curls `POST /close-day` once a day
  (e.g. just after midnight local time), passing the close-day token:
  `curl -X POST -H "X-Close-Day-Token: $CLOSE_DAY_TOKEN" https://<app>.fly.dev/close-day`
- **Simple alternative**: a system cron job wherever this is hosted:
  `0 0 * * * curl -X POST -H "X-Close-Day-Token: $CLOSE_DAY_TOKEN" http://localhost:5000/close-day`

If `CLOSE_DAY_TOKEN` isn't set, `/close-day` falls back to requiring a
logged-in session, so a scheduler with no way to authenticate can't call it —
set the token for any unattended (cron) deployment.

## Hosting free on PythonAnywhere (no credit card)

Fly.io requires a payment method. PythonAnywhere's free "Beginner" account
doesn't, and — unlike most free tiers — its filesystem persists, which is what
a SQLite-backed app needs. It runs the app through WSGI rather than Docker, so
`Dockerfile`/`fly.toml` go unused; two other files in this repo cover it
instead:

- `wsgi_pythonanywhere.py` — a template for the WSGI configuration file
  PythonAnywhere owns (at `/var/www/<username>_pythonanywhere_com_wsgi.py`).
  Paste it into their Web-tab editor and fill in the username, `SECRET_KEY`
  and `LEDGER_PASSWORD_HASH` **there**, never in this repo copy.
- `close_day_task.py` — the daily close, for their Tasks tab. Free accounts
  can't make arbitrary outbound HTTP requests, so instead of curling
  `/close-day` it imports the app and drives the same route in-process via
  Flask's test client. Safe to run more than once a day; `sweep_unspent`
  refuses to sweep a window twice.

Outline: sign up → `git clone` in a Bash console →
`mkvirtualenv --python=$(which python3.11) ledger` (use the interpreter
`which` reports — a `/usr/bin/python3.x` path builds the virtualenv against a
mismatched base and even `pip` then dies with `No module named
'_posixsubprocess'`) → `pip install -r requirements.txt` → Web tab → *Manual configuration* (not the
Flask option) → set source dir, virtualenv, and a `/static/` mapping to
`<project>/static/` → paste the WSGI template → Reload → add the daily task
`~/.virtualenvs/ledger/bin/python ~/Personal_Finance/close_day_task.py`
(scheduled in **UTC**; 17:05 UTC is 00:05 WIB).

Caveats: a CPU-seconds quota, a renewal button to click every three months,
and no custom domain on the free plan.

## Deploying to Fly.io

The repo ships everything Fly needs: a `Dockerfile` (gunicorn on port 8080,
running as a non-root user), a `.dockerignore` that keeps `ledger.db` and
`.secret_key` out of the image, and a `fly.toml` with the volume mount and
non-secret env already wired up.

Fly's default filesystem is ephemeral — anything written to disk (including
the `.secret_key` and `.password_hash` auto-generated by a bare `python
app.py`) is wiped on every redeploy and most restarts. Two separate things
need to survive that, handled two different ways.

**0. Add a payment method** to your Fly organization first (Billing, at
fly.io/dashboard). Without one, Fly can't place a machine and `fly launch`
fails with the unhelpful `failed to determine region: failed to get
placements: requested machine count exceeds organization limit`.

**1. Rename the app** in `fly.toml` — the name has to be unique across Fly
and may contain **only lowercase letters, numbers and dashes**; capitals or
underscores are rejected outright. Set `primary_region` to your nearest
region if `sin` (Singapore) isn't it.

**2. Create the app and its volume:**

    fly launch --no-deploy       # reuses the existing fly.toml/Dockerfile
    fly volumes create ledger_data --size 1 --region sin

`fly.toml` already mounts that volume at `/data` and sets
`DB_PATH=/data/ledger.db`, so the database lives on the volume and survives
redeploys. A volume attaches to exactly one machine — never scale this app
past a single instance.

**3. Set the secrets** (auth never depends on the filesystem this way):

    fly secrets set \
      SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") \
      LEDGER_PASSWORD_HASH=$(python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))") \
      CLOSE_DAY_TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")

With `LEDGER_PASSWORD_HASH` set this way the app skips the `/setup` wizard
entirely — there's nothing to persist, so no volume is needed for auth.
`LEDGER_ENV=production` (already in `fly.toml`) marks the session cookie
`Secure`. **Never set `LEDGER_DISABLE_AUTH` on Fly** — it turns the login
gate off for everyone.

**4. Deploy and open it:**

    fly deploy
    fly open

**5. Schedule the daily close** — see the section above; the app has no
in-app scheduler, so `/close-day` needs an external trigger carrying the
`X-Close-Day-Token` header.

Schema changes are applied on every boot: `init_db()` runs at import time
under gunicorn and is idempotent, so a redeploy that adds a table picks it up
on the existing volume.
