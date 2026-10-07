"""
Sign-in tests: sign-up, sign-in, private data per account, Google sign-in redirect.
Runs offline on a temporary empty database with Google Calendar disabled.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "studyplanner_auth_test.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["STUDYPLANNER_DB"] = _DB
os.environ["STUDYPLANNER_CALENDAR_SYNC"] = "0"
os.environ["STUDYPLANNER_LOGIN"] = "1"
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret"

import datetime
import sqlite3
import unittest
from urllib.parse import urlparse, parse_qs

import app as flask_module


class AuthTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        flask_module.app.config["TESTING"] = True
        flask_module.app.config["LOGIN_REQUIRED"] = True
        # an "old" task created before accounts existed -> must go to the first account
        conn = sqlite3.connect(_DB)
        conn.execute("INSERT INTO tasks (subject, task, deadline, priority, status) VALUES ('Old', 'Legacy task', '2026-12-01', 'High', 'Pending')")
        conn.commit()
        conn.close()

    def client(self):
        return flask_module.app.test_client()

    def signup(self, c, name, email, password="password123", username=""):
        return c.post("/signup", data={"name": name, "email": email, "username": username,
                                       "password": password, "confirm": password})

    def test_1_pages_require_sign_in(self):
        c = self.client()
        r = c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        r = c.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Sign in to", r.data)
        self.assertIn(b"Continue with Google", r.data)
        self.assertIn(b"Forgot password?", r.data)
        self.assertEqual(c.get("/api/processing/x").status_code, 401)

    def test_2_signup_login_and_private_data(self):
        a = self.client()
        r = self.signup(a, "Binayak Mandal", "binayak@example.com", username="binayak")
        self.assertEqual(r.status_code, 302)
        # first account adopts data created before accounts existed
        self.assertIn(b"Legacy task", a.get("/tasks").data)
        a.post("/add-task", data={"subject": "PHP", "task": "Alpha private task",
                                  "deadline": "2026-11-01", "priority": "High"})

        b = self.client()
        self.assertEqual(self.signup(b, "Second Student", "second@example.com").status_code, 302)
        page = b.get("/tasks").data
        self.assertNotIn(b"Alpha private task", page)
        self.assertNotIn(b"Legacy task", page)
        b.post("/add-task", data={"subject": "Java", "task": "Bravo private task",
                                  "deadline": "2026-11-02", "priority": "Low"})
        self.assertNotIn(b"Bravo private task", a.get("/tasks").data)

        # B cannot delete A's task by guessing its id
        conn = sqlite3.connect(_DB)
        a_task = conn.execute("SELECT id FROM tasks WHERE task = 'Alpha private task'").fetchone()[0]
        conn.close()
        b.post(f"/delete/{a_task}")
        self.assertIn(b"Alpha private task", a.get("/tasks").data)

        # each account gets its own default study hours
        self.assertIn(b"17:00", b.get("/availability").data)

    def test_3_login_logout_and_errors(self):
        c = self.client()
        r = c.post("/login", data={"identifier": "binayak", "password": "wrong-pass"})
        self.assertEqual(r.status_code, 401)
        self.assertIn(b"Incorrect", r.data)
        r = c.post("/login", data={"identifier": "BINAYAK@example.com", "password": "password123"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/").status_code, 200)
        c.post("/logout")
        self.assertEqual(c.get("/").status_code, 302)

    def test_4_signup_validation(self):
        c = self.client()
        r = self.signup(c, "Dup", "binayak@example.com")
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"already exists", r.data)
        r = c.post("/signup", data={"name": "X", "email": "x@example.com", "password": "short", "confirm": "short"})
        self.assertIn(b"at least 8", r.data)

    def test_5_google_redirect(self):
        c = self.client()
        r = c.get("/auth/google")
        self.assertEqual(r.status_code, 302)
        target = urlparse(r.headers["Location"])
        self.assertEqual(target.netloc, "accounts.google.com")
        q = parse_qs(target.query)
        self.assertEqual(q["client_id"][0], "test-client-id.apps.googleusercontent.com")
        self.assertIn("/auth/google/callback", q["redirect_uri"][0])
        self.assertIn("openid", q["scope"][0])

    def test_7_google_callback_signs_in_and_links(self):
        original = flask_module.google_exchange_code
        try:
            # existing email/password account gets linked to Google
            flask_module.google_exchange_code = lambda code, cfg: {
                "sub": "google-123", "email": "Binayak@Example.com", "email_verified": True,
                "name": "Binayak Mandal", "picture": "https://example.com/p.png"}
            c = self.client()
            c.get("/auth/google")
            with c.session_transaction() as s:
                state = s["oauth_state"]
            r = c.get(f"/auth/google/callback?state={state}&code=abc")
            self.assertEqual(r.status_code, 302)
            self.assertIn(b"Alpha private task", c.get("/tasks").data)
            conn = sqlite3.connect(_DB)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE google_sub = 'google-123'").fetchone()[0], 1)
            conn.close()

            # wrong state is rejected
            c2 = self.client()
            c2.get("/auth/google")
            r = c2.get("/auth/google/callback?state=forged&code=abc")
            self.assertIn("/login", r.headers["Location"])
            self.assertEqual(c2.get("/").status_code, 302)

            # brand-new Google user gets a fresh, empty account
            flask_module.google_exchange_code = lambda code, cfg: {
                "sub": "google-999", "email": "new@gmail.com", "email_verified": True, "name": "New Person"}
            c3 = self.client()
            c3.get("/auth/google")
            with c3.session_transaction() as s:
                state = s["oauth_state"]
            c3.get(f"/auth/google/callback?state={state}&code=xyz")
            page = c3.get("/tasks").data
            self.assertEqual(c3.get("/").status_code, 200)
            self.assertNotIn(b"Alpha private task", page)
        finally:
            flask_module.google_exchange_code = original

    def test_6_calendar_only_for_owner(self):
        b = self.client()
        b.post("/login", data={"identifier": "second@example.com", "password": "password123"})
        with flask_module.app.test_request_context():
            conn = sqlite3.connect(_DB)
            conn.row_factory = sqlite3.Row
            second = conn.execute("SELECT * FROM users WHERE email = 'second@example.com'").fetchone()
            first = conn.execute("SELECT * FROM users WHERE email = 'binayak@example.com'").fetchone()
            conn.close()
            flask_module.g.user = second
            self.assertFalse(flask_module.is_owner())
            flask_module.g.user = first
            self.assertTrue(flask_module.is_owner())


if __name__ == "__main__":
    unittest.main()
