"""Run the daily close without going over the network.

`/close-day` is normally triggered by an external cron doing an HTTP POST with
the `X-Close-Day-Token` header. Free PythonAnywhere accounts can't make
arbitrary outbound requests, so their scheduled task runs this script instead:
it imports the app and drives the very same route through Flask's test client,
in-process. No HTTP server, no token to keep in sync, no duplicated sweep
logic that could drift from the real one.

Scheduled-task command on PythonAnywhere (one line, adjust the paths):

    /home/<user>/.virtualenvs/ledger/bin/python /home/<user>/Personal_Finance/close_day_task.py

Anywhere else, plain cron works the same way:

    5 0 * * * cd /path/to/Personal_Finance && python close_day_task.py
"""

import os
import secrets
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)  # so this runs from any working directory

# app.py reads its configuration at import time, so both of these have to be
# set first. The token never leaves this process — it exists only to satisfy
# require_login(), which lets /close-day through on a matching header.
os.environ["CLOSE_DAY_TOKEN"] = secrets.token_urlsafe(32)

# Must match whatever the web app uses, or the sweep would update a different
# database. Unset means app.py's own default, BASE_DIR/ledger.db.
if os.environ.get("DB_PATH"):
    print(f"[close-day] using DB_PATH={os.environ['DB_PATH']}")

from app import app  # noqa: E402  (import after the environment is prepared)

with app.test_client() as client:
    response = client.post(
        "/close-day", headers={"X-Close-Day-Token": os.environ["CLOSE_DAY_TOKEN"]}
    )

stamp = datetime.now().isoformat(timespec="seconds")

# The route redirects to the dashboard on success; a 401 would mean the token
# check didn't run, which is a bug rather than a transient failure.
if response.status_code in (200, 302):
    print(f"[close-day] {stamp} ok")
    sys.exit(0)

print(f"[close-day] {stamp} FAILED with HTTP {response.status_code}", file=sys.stderr)
sys.exit(1)
