FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. The volume is mounted at /data and must be writable
# by this user, so chown it at build time — Fly preserves the mount point's
# ownership across restarts.
RUN useradd --create-home --uid 10001 ledger \
    && mkdir -p /data \
    && chown -R ledger:ledger /app /data
USER ledger

EXPOSE 8080

# One worker, several threads: every write goes to a single SQLite file, and
# concurrent worker processes would contend for its write lock. Threads are
# enough for a single-user app, and each request still gets its own connection.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "60", "app:app"]
