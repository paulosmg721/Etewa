import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

from app import create_app
from models import DeliveryRequest, RequestStatusHistory, db, utc_now


class DeliveryApiTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.instance_path = temp.name
        self.config = {
            "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///requests-test.sqlite3",
            "SECRET_KEY": secrets.token_hex(32), "SESSION_COOKIE_SECURE": False,
        }
        self.app = self.make_app()
        with self.app.app_context():
            db.create_all()
        self.traveler, self.sender, self.other = [self.app.test_client() for _ in range(3)]
        for client, name in [(self.traveler, "Traveler"), (self.sender, "Sender"), (self.other, "Other")]:
            self.sign_up(client, name)
        self.trip = self.publish()
        self.path = f"/api/trips/{self.trip}/requests"

    def make_app(self):
        app = create_app(self.config, instance_path=self.instance_path)
        with app.app_context():
            self.addCleanup(db.engine.dispose)
        return app

    def send(self, client, path, body=None, method="POST"):
        token = client.get("/api/me").get_json()["csrf_token"]
        return client.open(path, method=method, json=body, headers={"X-CSRF-Token": token})

    def sign_up(self, client, name):
        response = self.send(client, "/api/register", {
            "email": f"{name.lower()}@example.test", "full_name": name,
            "password": "delivery test only passphrase", "city": "Ottawa", "phone": "+1 555 0100",
        })
        self.assertEqual(response.status_code, 201, response.get_json())

    def publish(self, **changes):
        result = self.send(self.traveler, "/api/trips", {
            "departure_country": "CA", "destination_country": "ET",
            "travel_date": (utc_now().date() + timedelta(days=5)).isoformat(), "capacity_grams": 2000,
            "pricing_method": "fixed", "price_minor": 1500,
        } | changes)
        self.assertEqual(result.status_code, 201, result.get_json())
        return result.get_json()["trip"]["id"]

    def package(self, **changes):
        return {"package_category": "documents", "package_description": "Sealed papers", "weight_grams": 1200} | changes

    def create(self, client=None, *, trip=None, **changes):
        path = self.path if trip is None else f"/api/trips/{trip}/requests"
        response = self.send(client or self.sender, path, self.package(**changes))
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["request"]

    def state(self, client, request_id, action):
        return self.send(client, f"/api/requests/{request_id}/{action}")

    def test_creation_visibility_and_history(self):
        item = self.create()
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["history"][0]["new_status"], "pending")
        self.assertEqual(item["history"][0]["actor"], "requester")
        self.assertEqual(item["trip"]["price_minor"], 1500)
        incoming = self.traveler.get("/api/my/requests").get_json()
        self.assertEqual(incoming["incoming"][0]["id"], item["id"])
        self.assertEqual(incoming["incoming"][0]["allowed_actions"], ["accept", "decline"])
        self.assertEqual(self.sender.get("/api/my/requests").get_json()["outgoing"][0]["id"], item["id"])
        self.assertEqual(self.other.get("/api/my/requests").get_json(), {"incoming": [], "outgoing": []})
        detail = self.traveler.get(f"/api/requests/{item['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.headers["Cache-Control"], "no-store")
        for private in ("sender@example.test", "traveler@example.test", "+1 555 0100", "password_hash"):
            self.assertNotIn(private, detail.get_data(as_text=True))
        self.assertEqual(self.other.get(f"/api/requests/{item['id']}").status_code, 404)

    def test_validation_duplicate_and_own_trip(self):
        for payload in [
            {"weight_grams": 0}, {"weight_grams": -1}, {"weight_grams": True}, {"weight_grams": 1.5},
            {"weight_grams": "1200"}, {"package_description": " "}, {"package_description": "x" * 2001},
            {"package_category": "bad"}, {"package_category": "other"}, {"requester_id": 2},
        ]:
            with self.subTest(payload=list(payload)):
                self.assertEqual(self.send(self.sender, self.path, self.package(**payload)).status_code, 400)
        self.assertEqual(self.send(self.sender, self.path, self.package(weight_grams=2001)).status_code, 409)
        self.assertEqual(self.send(self.traveler, self.path, self.package()).status_code, 403)
        self.create(package_category="other", category_details="Books")
        self.assertEqual(self.send(self.sender, self.path, self.package()).status_code, 409)

    def test_accept_cancel_and_release_capacity(self):
        item = self.create()
        other = self.create(self.other)
        accepted = self.state(self.traveler, item["id"], "accept")
        self.assertEqual(accepted.get_json()["request"]["status"], "accepted")
        self.assertEqual(self.traveler.get(f"/api/trips/{self.trip}").get_json()["trip"]["available_capacity_grams"], 800)
        self.assertEqual(self.state(self.traveler, other["id"], "accept").status_code, 409)
        blocked = self.traveler.get(f"/api/requests/{other['id']}").get_json()["request"]
        self.assertEqual(blocked["allowed_actions"], ["decline"])
        self.assertIn("capacity", blocked["accept_notice"])
        self.assertEqual(self.state(self.traveler, item["id"], "decline").status_code, 409)
        cancelled = self.state(self.sender, item["id"], "cancel").get_json()["request"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual([event["new_status"] for event in cancelled["history"]], ["pending", "accepted", "cancelled"])
        self.assertEqual(self.state(self.traveler, other["id"], "accept").status_code, 200)

    def test_decline_and_pricing_modes_survive_restart(self):
        for method, price in [("negotiable", 1700), ("agreement", None)]:
            trip = self.publish(pricing_method=method, price_minor=price)
            item = self.create(trip=trip)
            self.assertEqual((item["trip"]["pricing_method"], item["trip"]["price_minor"]), (method, price))
            result = self.state(self.traveler, item["id"], "decline").get_json()["request"]
            self.assertEqual(result["status"], "declined")
            self.assertEqual(result["history"][-1]["actor"], "traveler")
            self.assertEqual(self.state(self.sender, item["id"], "cancel").status_code, 409)
        client = self.make_app().test_client()
        login = self.send(client, "/api/login", {"email": "sender@example.test", "password": "delivery test only passphrase"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual([item["status"] for item in client.get("/api/my/requests").get_json()["outgoing"]], ["declined", "declined"])

    def test_authorization_and_csrf(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/my/requests").status_code, 401)
        self.assertEqual(self.send(anon, self.path, self.package()).status_code, 401)
        self.assertEqual(self.sender.post(self.path, json=self.package()).status_code, 403)
        item = self.create()
        self.assertEqual(self.traveler.post(f"/api/requests/{item['id']}/accept").status_code, 403)
        self.assertEqual(self.state(self.sender, item["id"], "accept").status_code, 403)
        self.assertEqual(self.state(self.traveler, item["id"], "cancel").status_code, 403)
        self.assertEqual(self.state(self.other, item["id"], "decline").status_code, 404)

    def test_simultaneous_acceptances_cannot_overbook(self):
        items = [self.create(), self.create(self.other)]
        clients = [self.app.test_client(), self.app.test_client()]
        for client in clients:
            self.assertEqual(self.send(client, "/api/login", {
                "email": "traveler@example.test", "password": "delivery test only passphrase",
            }).status_code, 200)
        tokens = [client.get("/api/me").get_json()["csrf_token"] for client in clients]
        barrier = Barrier(2, timeout=5)

        def accept(index):
            barrier.wait()
            return clients[index].post(f"/api/requests/{items[index]['id']}/accept",
                                       headers={"X-CSRF-Token": tokens[index]}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(accept, range(2))), [200, 409])
        with self.app.app_context():
            requests = db.session.scalars(db.select(DeliveryRequest)).all()
            self.assertEqual(sorted(item.status for item in requests), ["accepted", "requested"])
            self.assertEqual(sum(item.weight_grams for item in requests if item.status == "accepted"), 1200)
            self.assertEqual(len(db.session.scalars(db.select(RequestStatusHistory)).all()), 3)
