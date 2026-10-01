"""Tests for the current Ledger web iteration.

Run with:
    python -m unittest discover -s tests -v

No test dependencies: the suite uses the stdlib `unittest` runner and Flask's
own test client. It imports app.py against a throwaway temp database (set via
DB_PATH *before* the import, since importing app runs init_db()) and never
touches the real ledger.db.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.path.join(tempfile.mkdtemp(prefix="ledger-tests-"), "ledger_test.db")

# Configure the app *before* importing it: auth is enabled with a known
# password, so the login/setup gate itself is under test too.
os.environ["DB_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["LEDGER_PASSWORD_HASH"] = generate_password_hash("test-password")
os.environ.pop("LEDGER_DISABLE_AUTH", None)

sys.path.insert(0, BASE_DIR)
import app as ledger  # noqa: E402


class LedgerTestCase(unittest.TestCase):
    """Fresh database per test; helpers for auth, DB reads, and dashboard data."""

    def setUp(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(TEST_DB + suffix)
            except FileNotFoundError:
                pass
        ledger.init_db()
        # The login lockout is a process-global counter; reset it so one test's
        # failed login can't lock the next test out.
        ledger.LOGIN_ATTEMPTS.update(count=0, locked_until=0.0)

    def make_client(self, authenticated=True):
        client = ledger.app.test_client()
        if authenticated:
            resp = client.post("/login", data={"password": "test-password"})
            self.assertEqual(resp.status_code, 302, "test login should succeed")
        return client

    def pot_balances(self):
        con = sqlite3.connect(TEST_DB)
        con.row_factory = sqlite3.Row
        try:
            return {r["name"]: r["balance"] for r in con.execute("SELECT name, balance FROM pots")}
        finally:
            con.close()

    def pot_id(self, name):
        con = sqlite3.connect(TEST_DB)
        try:
            return con.execute("SELECT id FROM pots WHERE name = ?", (name,)).fetchone()[0]
        finally:
            con.close()

    def movement_rows(self):
        con = sqlite3.connect(TEST_DB)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute("SELECT * FROM pot_movements ORDER BY id")]
        finally:
            con.close()

    def dashboard(self):
        with ledger.app.test_request_context("/"):
            return ledger.build_dashboard_data()


class AuthTests(LedgerTestCase):
    def test_protected_get_redirects_to_login(self):
        resp = self.make_client(authenticated=False).get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_protected_post_returns_401_not_redirect(self):
        # Load-bearing for static/offline.js: an unauthenticated write must be a
        # bare 401, never a redirect fetch would follow and mistake for success.
        resp = self.make_client(authenticated=False).post(
            "/expense/add", data={"category_id": "1", "amount": "1000"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_setup_redirects_to_login_when_password_configured(self):
        resp = self.make_client(authenticated=False).get("/setup")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_wrong_password_rejected(self):
        resp = self.make_client(authenticated=False).post("/login", data={"password": "nope"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Incorrect password", resp.get_data(as_text=True))

    def test_login_success_reaches_dashboard(self):
        client = self.make_client()
        self.assertEqual(client.get("/").status_code, 200)


class CoreFlowTests(LedgerTestCase):
    def _add_category(self, client, name="Food", budget=500_000, period="monthly", due_day=""):
        client.post(
            "/category/add",
            data={"name": name, "budget_amount": str(budget), "period": period, "due_day": due_day},
        )
        con = sqlite3.connect(TEST_DB)
        try:
            return con.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]
        finally:
            con.close()

    def test_add_category_and_expense_reflected_on_dashboard(self):
        client = self.make_client()
        category_id = self._add_category(client, budget=500_000)

        client.post("/expense/add", data={"category_id": str(category_id), "amount": "125000"})

        with ledger.app.test_request_context("/"):
            row = ledger.get_db().execute(
                "SELECT SUM(amount) AS t FROM transactions WHERE category_id = ? AND kind = 'spend'",
                (category_id,),
            ).fetchone()
        self.assertEqual(row["t"], 125_000)

        data = self.dashboard()
        self.assertEqual(data["total_spent"], 125_000)
        self.assertEqual(data["cash_left"], -125_000)  # no income, so spending leaves cash negative

    def test_offline_replay_does_not_double_insert_expense(self):
        client = self.make_client()
        category_id = self._add_category(client)
        payload = {"category_id": str(category_id), "amount": "10000", "client_id": "cid-expense-1"}
        client.post("/expense/add", data=payload)
        client.post("/expense/add", data=payload)  # replayed submission

        con = sqlite3.connect(TEST_DB)
        try:
            count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 1)

    def test_sweep_unspent_is_idempotent(self):
        client = self.make_client()
        category_id = self._add_category(client, budget=300_000, due_day="5")
        client.post("/expense/add", data={"category_id": str(category_id), "amount": "100000"})

        start = date.today().replace(day=1).isoformat()
        end = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1).isoformat()
        category = {"id": category_id, "budget_amount": 300_000}

        # sweep_unspent doesn't commit itself (the /close-day route does), so
        # commit here and re-open a fresh context to prove the second call sees
        # the first sweep's row and refuses to sweep the window again.
        with ledger.app.test_request_context("/"):
            db = ledger.get_db()
            first = ledger.sweep_unspent(db, category, start, end)
            db.commit()
        with ledger.app.test_request_context("/"):
            db = ledger.get_db()
            second = ledger.sweep_unspent(db, category, start, end)
            db.commit()

        self.assertEqual(first, 200_000)  # 300k budget - 100k spent
        self.assertEqual(second, 0, "a swept window must not sweep again")
        self.assertEqual(self.pot_balances()["Auto-saved (Unspent)"], 200_000)

        con = sqlite3.connect(TEST_DB)
        try:
            row = con.execute(
                "SELECT sweep_key FROM transactions WHERE category_id = ? AND kind = 'auto_save'",
                (category_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], start, "an auto_save row should carry its window key")

    def test_sweep_unique_key_blocks_a_racing_sweep(self):
        # A competing /close-day committed for the same window while our request
        # was in flight. Model it with a row whose created_at predates the window
        # so the loose pre-check can't see it — the unique index must be the guard.
        client = self.make_client()
        category_id = self._add_category(client, budget=300_000, due_day="5")
        client.post("/expense/add", data={"category_id": str(category_id), "amount": "100000"})

        start = date.today().replace(day=1).isoformat()
        end = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1).isoformat()
        category = {"id": category_id, "budget_amount": 300_000}

        con = sqlite3.connect(TEST_DB)
        con.execute(
            """INSERT INTO transactions (category_id, amount, kind, note, created_at, sweep_key)
               VALUES (?, ?, 'auto_save', '', ?, ?)""",
            (category_id, 200_000, "2000-01-01T00:00:00", start),
        )
        con.commit()
        con.close()

        with ledger.app.test_request_context("/"):
            db = ledger.get_db()
            moved = ledger.sweep_unspent(db, category, start, end)
            db.commit()

        self.assertEqual(moved, 0, "the unique (category, window) key must reject the race")
        self.assertEqual(self.pot_balances()["Auto-saved (Unspent)"], 0)


class SavingsAllocationTests(LedgerTestCase):
    AUTO = "Auto-saved (Unspent)"
    EMERGENCY = "Emergency Fund"

    def seed_pot(self, client, pot_name, amount, client_id=None):
        data = {"pot_id": str(self.pot_id(pot_name)), "amount": str(amount)}
        if client_id:
            data["client_id"] = client_id
        client.post("/savings/deposit", data=data)

    def test_deposit_credits_pot_and_reduces_available(self):
        client = self.make_client()
        before = self.dashboard()["available"]
        self.seed_pot(client, self.AUTO, 100_000)
        self.assertEqual(self.pot_balances()[self.AUTO], 100_000)
        self.assertEqual(self.dashboard()["available"], before - 100_000)

    def test_deposit_undo_reverses_it(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 100_000)
        con = sqlite3.connect(TEST_DB)
        deposit_id = con.execute("SELECT id FROM pot_deposits ORDER BY id DESC LIMIT 1").fetchone()[0]
        con.close()

        client.post(f"/savings/deposit/{deposit_id}/delete")
        self.assertEqual(self.pot_balances()[self.AUTO], 0)
        self.assertEqual(self.dashboard()["manual_saved"], 0)

    def test_double_undo_of_deposit_is_a_noop(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 100_000)
        con = sqlite3.connect(TEST_DB)
        deposit_id = con.execute("SELECT id FROM pot_deposits ORDER BY id DESC LIMIT 1").fetchone()[0]
        con.close()

        client.post(f"/savings/deposit/{deposit_id}/delete")
        client.post(f"/savings/deposit/{deposit_id}/delete")  # retried undo
        self.assertEqual(self.pot_balances()[self.AUTO], 0)

    def test_transfer_moves_between_pots_without_touching_available(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 100_000)
        available_before = self.dashboard()["available"]

        client.post(
            "/savings/allocate",
            data={
                "from_pot_id": str(self.pot_id(self.AUTO)),
                "to_pot_id": str(self.pot_id(self.EMERGENCY)),
                "amount": "40000",
                "client_id": "transfer-1",
            },
        )

        balances = self.pot_balances()
        self.assertEqual(balances[self.AUTO], 60_000)
        self.assertEqual(balances[self.EMERGENCY], 40_000)
        self.assertEqual(self.dashboard()["available"], available_before)
        self.assertEqual(self.dashboard()["withdrawn"], 0)

    def test_withdraw_to_available_raises_available_and_cash_left(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 100_000)
        before = self.dashboard()

        client.post(
            "/savings/allocate",
            data={"from_pot_id": str(self.pot_id(self.AUTO)), "to_pot_id": "", "amount": "30000", "client_id": "wd-1"},
        )

        after = self.dashboard()
        self.assertEqual(self.pot_balances()[self.AUTO], 70_000)
        self.assertEqual(after["withdrawn"], 30_000)
        self.assertEqual(after["available"], before["available"] + 30_000)
        self.assertEqual(after["cash_left"], before["cash_left"] + 30_000)

    def test_allocate_replay_is_idempotent(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 100_000)
        payload = {
            "from_pot_id": str(self.pot_id(self.AUTO)),
            "to_pot_id": str(self.pot_id(self.EMERGENCY)),
            "amount": "40000",
            "client_id": "transfer-replay",
        }
        client.post("/savings/allocate", data=payload)
        client.post("/savings/allocate", data=payload)

        self.assertEqual(self.pot_balances()[self.AUTO], 60_000)
        self.assertEqual(self.pot_balances()[self.EMERGENCY], 40_000)
        self.assertEqual(len(self.movement_rows()), 1)

    def test_overdraw_is_rejected(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 50_000)
        client.post(
            "/savings/allocate",
            data={"from_pot_id": str(self.pot_id(self.AUTO)), "to_pot_id": "", "amount": "999999", "client_id": "big"},
        )
        self.assertEqual(self.pot_balances()[self.AUTO], 50_000)
        self.assertEqual(self.movement_rows(), [])

    def test_transfer_to_same_pot_is_rejected(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 50_000)
        auto_id = str(self.pot_id(self.AUTO))
        client.post(
            "/savings/allocate",
            data={"from_pot_id": auto_id, "to_pot_id": auto_id, "amount": "1000", "client_id": "self"},
        )
        self.assertEqual(self.pot_balances()[self.AUTO], 50_000)
        self.assertEqual(self.movement_rows(), [])

    def test_malformed_destination_is_rejected_not_treated_as_withdrawal(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 50_000)
        for bad in ("abc", "0", "-3"):
            client.post(
                "/savings/allocate",
                data={"from_pot_id": str(self.pot_id(self.AUTO)), "to_pot_id": bad, "amount": "1000", "client_id": f"bad-{bad}"},
            )
        self.assertEqual(self.pot_balances()[self.AUTO], 50_000)
        self.assertEqual(self.movement_rows(), [])

    def test_transfer_to_unknown_pot_is_rejected(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 50_000)
        client.post(
            "/savings/allocate",
            data={"from_pot_id": str(self.pot_id(self.AUTO)), "to_pot_id": "99999", "amount": "1000", "client_id": "ghost"},
        )
        self.assertEqual(self.pot_balances()[self.AUTO], 50_000)
        self.assertEqual(self.movement_rows(), [])

    def test_double_undo_does_not_double_credit(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 100_000)
        client.post(
            "/savings/allocate",
            data={"from_pot_id": str(self.pot_id(self.AUTO)), "to_pot_id": "", "amount": "30000", "client_id": "wd-double"},
        )
        movement_id = self.movement_rows()[0]["id"]
        client.post(f"/savings/movement/{movement_id}/delete")
        client.post(f"/savings/movement/{movement_id}/delete")  # duplicate/retried undo
        self.assertEqual(self.pot_balances()[self.AUTO], 100_000)
        self.assertEqual(self.movement_rows(), [])

    def test_feedback_is_rendered_after_a_move(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 50_000)

        insufficient = client.post(
            "/savings/allocate",
            data={"from_pot_id": str(self.pot_id(self.AUTO)), "to_pot_id": "", "amount": "999999"},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("flash-error", insufficient)
        self.assertIn("Not enough", insufficient)

        success = client.post(
            "/savings/allocate",
            data={"from_pot_id": str(self.pot_id(self.AUTO)), "to_pot_id": "", "amount": "10000"},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("flash-success", success)
        self.assertIn("back to available", success)

    def test_feedback_is_rendered_after_a_deposit(self):
        client = self.make_client()
        html = client.post(
            "/savings/deposit",
            data={"pot_id": str(self.pot_id(self.AUTO)), "amount": "25000"},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("flash-success", html)
        self.assertIn("Set aside", html)

    def test_feedback_is_shown_once(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 50_000)
        client.post(
            "/savings/allocate",
            data={"from_pot_id": str(self.pot_id(self.AUTO)), "to_pot_id": "", "amount": "10000"},
            follow_redirects=True,
        )
        # A second render must not repeat the already-consumed message.
        again = client.get("/savings").get_data(as_text=True)
        self.assertNotIn("back to available", again)

    def test_movement_undo_restores_both_pots(self):
        client = self.make_client()
        self.seed_pot(client, self.AUTO, 100_000)
        client.post(
            "/savings/allocate",
            data={
                "from_pot_id": str(self.pot_id(self.AUTO)),
                "to_pot_id": str(self.pot_id(self.EMERGENCY)),
                "amount": "40000",
                "client_id": "transfer-undo",
            },
        )
        movement_id = self.movement_rows()[0]["id"]

        client.post(f"/savings/movement/{movement_id}/delete")

        balances = self.pot_balances()
        self.assertEqual(balances[self.AUTO], 100_000)
        self.assertEqual(balances[self.EMERGENCY], 0)
        self.assertEqual(self.movement_rows(), [])


class TrendChartTests(LedgerTestCase):
    def test_axis_spans_full_calendar_month(self):
        today = date.today()
        start, end = ledger.month_bounds(today)
        with ledger.app.test_request_context("/"):
            geo = ledger.spending_trend_chart(ledger.get_db(), today, start, end)

        days_in_month = (
            date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
            - date(today.year, today.month, 1)
        ).days
        last_day = date(today.year, today.month, days_in_month)

        self.assertAlmostEqual(geo["points"][0]["px"], geo["plot_left"], places=2)
        expected_last_px = geo["plot_left"] + (geo["plot_right"] - geo["plot_left"]) * (today.day - 1) / (days_in_month - 1)
        self.assertAlmostEqual(geo["points"][-1]["px"], expected_last_px, places=2)
        self.assertEqual(geo["x_last_label"], last_day.strftime("%d %b"))

    def test_first_of_month_line_starts_at_left_edge(self):
        with ledger.app.test_request_context("/"):
            geo = ledger.line_chart_geometry(
                [{"day": 1, "cumulative": 0}], x_span=31, x_key="day"
            )
        self.assertEqual(geo["points"][0]["px"], ledger.CHART_PAD["l"])

    def test_trajectory_chart_stays_index_spaced(self):
        with ledger.app.test_request_context("/"):
            traj = ledger.savings_trajectory_chart(ledger.get_db())
        self.assertAlmostEqual(traj["points"][0]["px"], traj["plot_left"], places=2)
        self.assertAlmostEqual(traj["points"][-1]["px"], traj["plot_right"], places=2)


class CloseDayCatchUpTests(LedgerTestCase):
    """run_close_day chases missed windows instead of only firing on Mon/1st."""

    def _add_category(self, name, budget, period, created):
        con = sqlite3.connect(TEST_DB)
        con.execute(
            "INSERT INTO categories (name, budget_amount, period, created_at) VALUES (?, ?, ?, ?)",
            (name, budget, period, created.isoformat() + "T08:00:00"),
        )
        con.commit()
        category_id = con.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]
        con.close()
        return category_id

    def _add_spend(self, category_id, amount, when):
        con = sqlite3.connect(TEST_DB)
        con.execute(
            "INSERT INTO transactions (category_id, amount, kind, created_at) VALUES (?, ?, 'spend', ?)",
            (category_id, amount, when.isoformat() + "T10:00:00"),
        )
        con.commit()
        con.close()

    def _run(self, today):
        with ledger.app.test_request_context("/"):
            db = ledger.get_db()
            moved = ledger.run_close_day(db, today)
            db.commit()
        return moved

    def test_catches_up_multiple_missed_months(self):
        today = date(2026, 10, 15)          # mid-month, not the 1st
        category_id = self._add_category("Rent", 300_000, "monthly", date(2026, 8, 1))
        self._add_spend(category_id, 100_000, date(2026, 8, 10))
        self._add_spend(category_id, 250_000, date(2026, 9, 10))

        moved = self._run(today)

        self.assertEqual(moved, 250_000)  # Aug leftover 200k + Sep leftover 50k
        self.assertEqual(self.pot_balances()["Auto-saved (Unspent)"], 250_000)
        self.assertEqual(self._run(today), 0, "catch-up must be idempotent")

    def test_does_not_sweep_before_the_category_existed(self):
        today = date(2026, 10, 15)
        self._add_category("New", 100_000, "monthly", date(2026, 10, 10))
        self.assertEqual(self._run(today), 0)
        self.assertEqual(self.pot_balances()["Auto-saved (Unspent)"], 0)

    def test_catches_up_missed_weeks(self):
        today = date(2026, 10, 15)  # Thursday
        category_id = self._add_category("Groceries", 150_000, "weekly", date(2026, 9, 28))
        self._add_spend(category_id, 10_000, date(2026, 9, 30))  # week of Sep 28
        self._add_spend(category_id, 20_000, date(2026, 10, 7))  # week of Oct 5

        moved = self._run(today)

        self.assertEqual(moved, 270_000)  # 140k + 130k leftovers

    def test_legacy_category_sweeps_only_the_latest_window(self):
        # No created_at -> must not retroactively bank older months.
        today = date(2026, 10, 15)
        con = sqlite3.connect(TEST_DB)
        con.execute("INSERT INTO categories (name, budget_amount, period) VALUES ('Old', 300000, 'monthly')")
        con.commit()
        category_id = con.execute("SELECT id FROM categories WHERE name = 'Old'").fetchone()[0]
        con.close()
        self._add_spend(category_id, 100_000, date(2026, 8, 10))  # older, must be ignored

        moved = self._run(today)

        self.assertEqual(moved, 300_000)  # only September (prev month)
        con = sqlite3.connect(TEST_DB)
        try:
            keys = [r[0] for r in con.execute("SELECT sweep_key FROM transactions WHERE kind='auto_save'")]
        finally:
            con.close()
        self.assertEqual(keys, ["2026-09-01"])


class TrendWindowTests(LedgerTestCase):
    """The Trends charts are anchored to TREND_START_MONTH, not a rolling 12 months."""

    @unittest.skipIf(date.today() < date(2026, 9, 1), "run before the trend anchor month")
    def test_months_between_is_inclusive_and_ordered(self):
        months = ledger.months_between("2026-09", "2026-11")
        self.assertEqual([m["key"] for m in months], ["2026-09", "2026-10", "2026-11"])
        self.assertEqual(months[0]["full_label"], "September 2026")

    def test_months_between_falls_back_when_start_is_after_end(self):
        months = ledger.months_between("2030-01", "2026-10")
        self.assertEqual([m["key"] for m in months], ["2026-10"])

    @unittest.skipIf(date.today() < date(2026, 9, 1), "run before the trend anchor month")
    def test_trend_months_start_at_the_constant_and_end_today(self):
        months = ledger.trend_months()
        self.assertEqual(months[0]["key"], ledger.TREND_START_MONTH)
        self.assertEqual(months[0]["full_label"], "September 2026")
        today = date.today()
        self.assertEqual(months[-1]["key"], f"{today.year:04d}-{today.month:02d}")

    @unittest.skipIf(date.today() < date(2026, 9, 1), "run before the trend anchor month")
    def test_cash_flow_chart_starts_at_september_2026(self):
        with ledger.app.test_request_context("/"):
            chart = ledger.cash_flow_chart(ledger.get_db())
        self.assertEqual(chart["rows"][0]["full_label"], "September 2026")
        today = date.today()
        self.assertEqual(
            chart["rows"][-1]["full_label"], date(today.year, today.month, 1).strftime("%B %Y")
        )

    @unittest.skipIf(date.today() < date(2026, 9, 1), "run before the trend anchor month")
    def test_trajectory_chart_starts_at_september_2026(self):
        with ledger.app.test_request_context("/"):
            trajectory = ledger.savings_trajectory_chart(ledger.get_db())
        self.assertEqual(trajectory["points"][0]["label"], "September 2026")

    def test_trends_page_renders_the_window_label(self):
        client = self.make_client()
        html = client.get("/trends").get_data(as_text=True)
        self.assertIn("NET CASH FLOW", html)
        self.assertNotIn("LAST 12 MONTHS", html)


class DummySeedTests(LedgerTestCase):
    def test_seed_script_populates_a_dummy_database(self):
        seed_db = os.path.join(tempfile.mkdtemp(prefix="ledger-seed-"), "dummy.db")
        env = dict(os.environ, DB_PATH=seed_db)
        result = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "seed_dummy_db.py")],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # It must never write to the shared test DB or the real ledger.db.
        self.assertNotEqual(os.path.abspath(seed_db), os.path.abspath(TEST_DB))

        con = sqlite3.connect(seed_db)
        try:
            for table in ("categories", "transactions", "income_sources", "pot_movements"):
                count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertGreater(count, 0, f"{table} should be populated")
        finally:
            con.close()

    def test_seed_script_refuses_to_target_ledger_db(self):
        result = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "seed_dummy_db.py")],
            env=dict(os.environ, DB_PATH=os.path.join(BASE_DIR, "ledger.db")),
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
