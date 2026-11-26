import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash

from app import create_app
from models import User, db, utc_now


class AuthTest(unittest.TestCase):
    password = "a test-only long passphrase"

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": "sqlite:///auth-test.sqlite3",
                "SECRET_KEY": secrets.token_hex(32),
                "SESSION_COOKIE_SECURE": False,
                "LOGIN_RATE_LIMIT": 4,
            },
            instance_path=temp.name,
        )
        with self.app.app_context():
            db.create_all()
            self.addCleanup(db.engine.dispose)
        self.client = self.app.test_client()

    def csrf(self, client):
        return client.get("/api/me").get_json()["csrf_token"]

    def write(self, method, route, data=None, *, client=None, token=None, headers=None):
        client = client or self.client
        if token is None:
            token = self.csrf(client)
        return client.open("/api/" + route, method=method, json=data, headers={
            "X-CSRF-Token": token, **(headers or {}),
        })

    def register(self, *, client=None, **overrides):
        payload = {"full_name": "Example Traveler", "email": "traveler@example.test", "password": self.password}
        return self.write("POST", "register", payload | overrides, client=client)

    def login(self, *, client=None, **overrides):
        return self.write("POST", "login", {
            "email": "traveler@example.test", "password": self.password, **overrides,
        }, client=client)

    def assert_safe_user(self, response, *, profile=False):
        body = response.get_json()
        self.assertEqual(set(body["user"]), {
            "id", "full_name", "email", "city", "bio", "created_at",
        } | ({"phone"} if profile else set()))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(self.password, response.get_data(as_text=True))
        return body["user"]

    def test_registration_normalizes_email_and_stores_scrypt_hash(self):
        response = self.register(email="  Traveler@Example.Test  ", city=" Addis Ababa ", phone="+251 11 555 0101")
        self.assertEqual(response.status_code, 201)
        user = self.assert_safe_user(response)
        self.assertRegex(user["id"], r"^account:[1-9]\d*$")
        self.assertEqual(user["email"], "traveler@example.test")
        self.assertEqual(user["city"], "Addis Ababa")
        with self.app.app_context():
            stored = db.session.scalars(db.select(User)).one()
            self.assertTrue(stored.password_hash.startswith("scrypt:"))
            self.assertNotEqual(stored.password_hash, self.password)
            self.assertTrue(check_password_hash(stored.password_hash, self.password))
            self.assertNotIn(stored.password_hash, response.get_data(as_text=True))
        self.assert_safe_user(self.client.get("/api/me"))
        self.assertEqual(self.assert_safe_user(self.client.get("/api/profile"), profile=True)["phone"], "+251 11 555 0101")

    def test_duplicate_email_is_rejected(self):
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.register(email="TRAVELER@example.test").status_code, 409)
        with self.app.app_context():
            self.assertEqual(len(db.session.scalars(db.select(User)).all()), 1)

    def test_invalid_registration_input(self):
        for overrides in [
            {"email": "bad"}, {"email": "a..b@example.test"}, {"email": "a@-example.test"},
            {"password": "short"}, {"password": " " * 12}, {"password": "a" * 129},
            {"full_name": "  "}, {"full_name": 7}, {"city": "a" * 121}, {"phone": "call me"},
            {"bio": "\ud800"}, {"id": "account:10"},
        ]:
            with self.subTest(values=list(overrides), response_data=overrides):
                self.assertEqual(self.register(**overrides).status_code, 400)
        with self.app.app_context():
            self.assertIsNone(db.session.scalar(db.select(User.id)))

    def test_login_persists_and_clears_previous_session_state(self):
        self.assertEqual(self.register().status_code, 201)
        other_browser = self.app.test_client()
        failed = self.login(client=other_browser, password="incorrect")
        unknown = self.login(client=other_browser, email="nobody@example.test")
        self.assertEqual(failed.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(failed.get_json(), unknown.get_json())
        self.assertIsNone(other_browser.get("/api/me").get_json()["user"])
        with other_browser.session_transaction() as previous:
            previous["test_state"] = "discard me"
            old_token = previous["csrf_token"]
        result = self.login(client=other_browser, email="TRAVELER@example.test")
        self.assertEqual(result.status_code, 200)
        self.assert_safe_user(result)
        self.assertNotEqual(result.get_json()["csrf_token"], old_token)
        cookie = result.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("; Secure", cookie)
        self.assertIn("Expires=", cookie)
        with other_browser.session_transaction() as current:
            self.assertNotIn("test_state", current)
        self.assertEqual(other_browser.get("/api/me").get_json()["user"]["email"], "traveler@example.test")
        self.assertEqual(other_browser.get("/api/profile").status_code, 200)

    def test_unauthorized_profile_and_logout(self):
        self.assertIsNone(self.client.get("/api/me").get_json()["user"])
        self.assertEqual(self.client.get("/api/profile").status_code, 401)
        self.assertEqual(self.write("PATCH", "profile", {"city": "Berlin"}).status_code, 401)
        signed_in = self.register()
        self.assertEqual(signed_in.status_code, 201)
        old_token = signed_in.get_json()["csrf_token"]
        result = self.write("POST", "logout", token=old_token)
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.get_json()["user"])
        self.assertNotEqual(result.get_json()["csrf_token"], old_token)
        self.assertEqual(self.client.get("/api/profile").status_code, 401)
        self.assertIsNone(self.client.get("/api/me").get_json()["user"])

    def test_profile_update_is_scoped_to_authenticated_account(self):
        traveler = self.register().get_json()["user"]
        other_client = self.app.test_client()
        other_user = self.register(client=other_client, email="sender@example.test").get_json()["user"]
        for protected in [{"id": other_user["id"]}, {"user_id": other_user["id"]}, {"password_hash": "fake"},
                          {"email": "sender@example.test"}, {"deactivated_at": None}]:
            self.assertEqual(self.write("PATCH", "profile", {"city": "Changed", **protected}).status_code, 400)
        self.assertEqual(self.write("PATCH", "profile", {"full_name": ""}).status_code, 400)
        self.assertEqual(self.write("PATCH", "profile", {"bio": "X" * 2001}).status_code, 400)
        result = self.write("PATCH", "profile?user_id=" + other_user["id"], {
            "full_name": " Updated Traveler ", "city": "Berlin", "phone": "+49 123", "bio": "Hello",
        })
        self.assertEqual(result.status_code, 200)
        user = self.assert_safe_user(result, profile=True)
        self.assertEqual(user["id"], traveler["id"])
        self.assertEqual(user["full_name"], "Updated Traveler")
        self.assertEqual(user["phone"], "+49 123")
        self.assertEqual(user["bio"], "Hello")
        self.assertEqual(other_client.get("/api/profile").get_json()["user"], other_user | {"phone": None})
        self.assertEqual(self.write("PATCH", "profile", {"phone": None}).get_json()["user"]["phone"], None)

    def test_csrf_blocks_missing_mismatched_and_stale_tokens(self):
        for method, route in [("POST", "register"), ("POST", "login"), ("POST", "logout"), ("PATCH", "profile")]:
            self.assertEqual(self.client.open("/api/" + route, method=method, json={}).status_code, 403)
        token = self.csrf(self.client)
        other_client = self.app.test_client()
        other_token = self.csrf(other_client)
        self.assertEqual(self.write("POST", "login", {}, token=other_token).status_code, 403)
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.write("POST", "logout", token=token).status_code, 403)
        self.assertIsNotNone(self.client.get("/api/me").get_json()["user"])
        oversized = self.write("POST", "register", {"full_name": "x" * 17000})
        self.assertEqual(oversized.status_code, 413)

    def test_login_rate_limit_applies_across_sessions_and_ignores_forged_proxy_headers(self):
        self.assertEqual(self.register().status_code, 201)
        with patch("auth.time.monotonic", return_value=1000):
            for attempt in range(4):
                client = self.app.test_client()
                result = self.write("POST", "login", {
                    "email": f"unknown{attempt}@example.test", "password": self.password,
                }, client=client, headers={"X-Forwarded-For": f"192.0.2.{attempt + 1}"})
                self.assertEqual(result.status_code, 401)
            blocked = self.login(client=self.app.test_client())
            self.assertEqual(blocked.status_code, 429)
            self.assertGreater(int(blocked.headers["Retry-After"]), 0)
        with patch("auth.time.monotonic", return_value=1901):
            self.assertEqual(self.login(client=self.app.test_client()).status_code, 200)

    def test_deactivated_account_cannot_sign_in_or_use_existing_session(self):
        self.assertEqual(self.register().status_code, 201)
        with self.app.app_context():
            user = db.session.scalars(db.select(User)).one()
            user.deactivated_at = utc_now()
            db.session.commit()
        self.assertEqual(self.client.get("/api/profile").status_code, 401)
        self.assertIsNone(self.client.get("/api/me").get_json()["user"])
        denied = self.login(client=self.app.test_client())
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json(), {"error": "Invalid email or password."})

    def test_secure_cookie_can_be_enabled_and_missing_secret_fails_closed(self):
        self.app.config["SESSION_COOKIE_SECURE"] = True
        secure = self.client.get("/api/me", base_url="https://localhost")
        self.assertIn("; Secure", secure.headers["Set-Cookie"])
        self.app.config["SECRET_KEY"] = None
        unavailable = self.client.get("/api/me")
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("Set-Cookie", unavailable.headers)
        self.assertEqual(self.client.get("/").status_code, 200)
