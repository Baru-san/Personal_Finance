"""Template for PythonAnywhere's WSGI configuration file.

PythonAnywhere doesn't run gunicorn or Docker — it imports a WSGI callable
named `application` from a file it owns, at:

    /var/www/<username>_pythonanywhere_com_wsgi.py

Copy everything below into that file through the Web tab's editor (NOT this
copy in the repo), replace the two placeholders, and reload the web app.

Keep the real password hash out of this repo copy: fill it in only in the
PythonAnywhere editor, where the file lives outside version control.
"""

import os
import sys

# --- 1. where the code lives -------------------------------------------------
PROJECT_DIR = "/home/<username>/Personal_Finance"
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# --- 2. configuration --------------------------------------------------------
# app.py reads all of this at import time, so it must be set before the import
# below. Generate the two values locally, on your own machine:
#
#   python3 -c "import secrets; print(secrets.token_hex(32))"
#   python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))"
#
os.environ["SECRET_KEY"] = "<paste the token_hex output>"
os.environ["LEDGER_PASSWORD_HASH"] = "<paste the generate_password_hash output>"

# PythonAnywhere serves the site over HTTPS, so the session cookie can be
# marked Secure.
os.environ["LEDGER_ENV"] = "production"

# The home directory is persistent here, so the database can simply live
# beside the code — no volume needed.
os.environ["DB_PATH"] = os.path.join(PROJECT_DIR, "ledger.db")

# Never set LEDGER_DISABLE_AUTH here. It turns off the login gate for
# everyone, and this site is on the public internet.

# --- 3. the app ---------------------------------------------------------------
from app import app as application  # noqa: E402,F401
